"""Operational semantics against explicit observations, without a running Factory."""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from district import health  # noqa: E402


AT = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-09-05T12:00:00Z"
SLUG = "acme/widgets"


def source(data, *, identity="runtime", timestamp=STAMP, cadence=5, error=None):
    return {"id": identity, "observed_at": timestamp, "cadence_seconds": cadence,
            "data": data, "error": error}


def scheduled():
    return {"dispatcher": {"service_active": False, "timer_active": True}, "executions": []}


def check(**changes):
    return {"condition": "dependency.unavailable", "resource": "model-endpoint",
            "status": "failed", "detail": "Configured endpoint refused this factory's connection.",
            "impact": "New triage cannot use its configured endpoint.", **changes}


class OperationsTest(unittest.TestCase):
    def classify(self, data, *, table=None, at=AT):
        return health.classify(SLUG, [source(data)], table or {}, at)

    def test_scheduling_intent_and_authoritative_stop_are_distinct(self):
        cases = [
            (scheduled(), "scheduled waiting", "normal", []),
            ({"dispatcher": {"pause": {"recorded_at": STAMP, "reason": "maintenance"}}, "executions": []},
             "deliberately paused", "normal", []),
            ({"dispatcher": {"capped": True, "cap_reason": "failure cap"}, "executions": []},
             "capped", "attention", ["scheduling.capped"]),
            ({"dispatcher": {"service_active": False, "timer_active": False, "expected_enabled": True}, "executions": []},
             "unexpectedly stopped", "attention", ["scheduling.unexpected_stop"]),
            ({"dispatcher": {"service_active": False, "timer_active": False}, "executions": []},
             "unknown", "unknown", []),
        ]
        for data, state, assessment, conditions in cases:
            with self.subTest(state=state):
                result = self.classify(data)
                self.assertEqual((result["operating_state"], result["assessment"]), (state, assessment))
                self.assertEqual([f["condition_code"] for f in result["findings"]], conditions)

    def test_unrecorded_or_future_pause_does_not_establish_intent(self):
        for pause in (True, {}, {"reason": "maintenance"}, {"recorded_at": STAMP},
                      {"recorded_at": "2026-09-06T12:00:00Z", "reason": "future maintenance"}):
            with self.subTest(pause=pause):
                result = self.classify({"dispatcher": {"pause": pause, "service_active": False, "timer_active": False}, "executions": []})
                self.assertEqual((result["operating_state"], result["assessment"], result["findings"]), ("unknown", "unknown", []))

    def test_registry_disable_means_cap_not_pause_even_without_snapshot(self):
        result = health.classify(SLUG, health.snapshot_sources(None, "unreachable"),
                                 {"disabled_at": STAMP, "disabled_reason": "10 failed passes"}, AT)
        self.assertEqual((result["operating_state"], result["execution_state"], result["assessment"]), ("capped", "unknown", "attention"))
        finding = result["findings"][0]
        self.assertEqual((finding["condition_code"], finding["observed_at"]), ("scheduling.capped", STAMP))
        self.assertIsNone(result["sources"][-1]["observed_at"])
        self.assertNotIn("first_observed_at", finding)

    def test_concurrent_stages_survive_and_newer_terminal_record_replaces_active(self):
        data = {"dispatcher": {"service_active": True}, "executions": [
            {"id": "pass1/worker", "state": "stage-active", "stage": "worker"},
            {"id": "pass1/review", "state": "stage-active", "stage": "review"},
        ]}
        result = self.classify(data)
        self.assertEqual((result["operating_state"], result["execution_state"], result["assessment"]), ("running", "stage-active", "normal"))
        self.assertEqual({e["id"]: e["stage"] for e in result["executions"]}, {"pass1/worker": "worker", "pass1/review": "review"})
        terminal = source({"executions": [{"id": "pass1/worker", "state": "interrupted", "stage": "worker"}]}, identity="liveness", timestamp="2026-09-05T12:00:01Z")
        result = health.classify(SLUG, [terminal, source(data)], {}, AT + timedelta(seconds=1))
        self.assertEqual({e["id"]: e["state"] for e in result["executions"]}, {"pass1/worker": "interrupted", "pass1/review": "stage-active"})

    def test_only_reported_phases_and_reasons_are_known(self):
        for execution in ({"id": "x", "state": "stage-active"}, {"id": "x", "state": "known wait"}):
            result = self.classify({"dispatcher": {"service_active": True}, "executions": [execution]})
            self.assertEqual((result["execution_state"], result["assessment"]), ("unknown", "unknown"))
            self.assertNotIn("stage", result["executions"][0])

    def test_execution_terminal_and_wait_states_do_not_manufacture_incidents(self):
        records = [
            {"id": "x", "state": "known wait", "reason": "capacity", "entered_at": "2025-01-01T00:00:00Z"},
            {"id": "x", "state": "known wait", "reason": "CI"},
            {"id": "x", "state": "blocked", "reason": "upstream project needs revision", "outcome_kind": "product"},
            {"id": "x", "state": "completed", "stage": "merge"},
            {"id": "x", "state": "interrupted", "stage": "worker"},
        ]
        for execution in records:
            with self.subTest(execution=execution):
                result = self.classify({"dispatcher": {"service_active": True}, "executions": [execution]})
                self.assertEqual((result["execution_state"], result["assessment"], result["findings"]), (execution["state"], "normal", []))

    def test_negative_product_verdict_differs_from_unable_mechanism_and_unknown_cause(self):
        execution = {"id": "pass2/gate", "state": "failed", "stage": "gate", "outcome_kind": "product", "reason": "tests failed"}
        data = {"dispatcher": {"service_active": True}, "executions": [execution],
                "tickets": [{"labels": ["ready-for-human"], "phase": "gate"}],
                "metrics": {"bounce_rate": 1}, "upstream": {"blocker": {"number": 7}}}
        result = self.classify(data)
        self.assertEqual((result["execution_state"], result["assessment"], result["findings"]), ("failed", "normal", []))
        execution.update(outcome_kind="mechanism", reason="configured executable missing")
        result = self.classify(data)
        finding = result["findings"][0]
        self.assertEqual((result["assessment"], finding["condition_code"]), ("attention", "runtime.mechanism_unavailable"))
        self.assertIn("configured executable missing", finding["evidence"][0]["detail"])
        self.assertIsNone(finding["cause"])
        execution.pop("outcome_kind")
        result = self.classify(data)
        self.assertEqual((result["execution_state"], result["observation"], result["assessment"], result["findings"]),
                         ("failed", "fresh", "unknown", []))
        self.assertEqual((result["sources"][0]["observed_at"], result["sources"][0]["age_seconds"]), (STAMP, 0))

    def test_source_age_and_staleness_do_not_reset_on_reread(self):
        sources = [source(scheduled())]
        initial = health.classify(SLUG, sources, {}, AT + timedelta(seconds=10))
        reread = health.classify(SLUG, sources, {}, AT + timedelta(seconds=11))
        self.assertEqual((initial["observation"], initial["sources"][0]["age_seconds"]), ("fresh", 10))
        self.assertEqual((reread["observation"], reread["sources"][0]["age_seconds"], reread["assessment"]), ("stale", 11, "unknown"))
        self.assertEqual(reread["operating_state"], "scheduled waiting")
        self.assertEqual(sources[0]["observed_at"], STAMP)
        for timestamp, cadence in ((None, 5), ("bad timestamp", 5), ("2026-09-05T12:01:00Z", 5), (STAMP, None), (STAMP, 0)):
            with self.subTest(timestamp=timestamp, cadence=cadence):
                result = health.classify(SLUG, [source(scheduled(), timestamp=timestamp, cadence=cadence)], {}, AT)
                self.assertEqual((result["observation"], result["assessment"]), ("partial", "unknown"))

    def test_missing_or_partial_sources_do_not_prove_dispatcher_stopped(self):
        unavailable = health.classify(SLUG, health.snapshot_sources(None, "timeout"), {}, AT)
        self.assertEqual((unavailable["operating_state"], unavailable["observation"], unavailable["assessment"], unavailable["findings"]), ("unknown", "unavailable", "unknown", []))
        sources = [source({"dispatcher": {"service_active": True}, "executions": [{"id": "x", "state": "stage-active", "stage": "worker"}]}),
                   source(None, identity="github", error="rate limited")]
        result = health.classify(SLUG, sources, {}, AT)
        self.assertEqual((result["operating_state"], result["execution_state"], result["observation"], result["assessment"]), ("running", "stage-active", "partial", "unknown"))
        self.assertEqual(result["sources"][0]["observation"], "fresh")

    def test_empty_or_malformed_present_data_is_partial_not_unavailable(self):
        for data in ({}, []):
            with self.subTest(data=data):
                result = self.classify(data)
                self.assertEqual((result["observation"], result["assessment"]), ("partial", "unknown"))
                self.assertEqual((result["sources"][0]["observation"], result["sources"][0]["observed_at"]), ("partial", STAMP))

    def test_malformed_records_remain_partial_with_valid_execution_telemetry(self):
        for extra in ({"resources": {}}, {"resources": [None]}, {"executions": [None]},
                      {"checks": {}}, {"dispatcher": []},
                      {"executions": [{"id": "x", "state": "invalid"}]}):
            with self.subTest(extra=extra):
                result = self.classify({**scheduled(), **extra})
                self.assertEqual((result["observation"], result["assessment"]), ("partial", "unknown"))
                self.assertEqual(result["sources"][0]["observation"], "partial")

    def test_reported_unknown_check_is_fresh_but_assessment_unknown(self):
        result = self.classify({**scheduled(), "checks": [check(status="unknown")]})
        self.assertEqual((result["observation"], result["assessment"], result["findings"]), ("fresh", "unknown", []))
        result = self.classify({**scheduled(), "executions": [{"id": "x", "state": "unknown"}]})
        self.assertEqual((result["observation"], result["execution_state"], result["assessment"]), ("fresh", "unknown", "unknown"))

    def test_legacy_github_errors_preserve_local_facts_without_fabricating_execution(self):
        snap = {"generated_at": STAMP, "errors": ["github: unavailable"],
                "dispatcher": {"service_active": True, "timer": {"active": True}, "runs": [], "consecutive_failures": 0},
                "tickets": [{"phase": "gate", "labels": ["ready-for-human"]}],
                "metrics": {"bounce_rate": 1}, "upstream": {"blocker": {"number": 8}}, "gpu_lock_held": True}
        for error in ("github: unavailable", "factory dashboard exited 1: ?; github: unavailable"):
            result = health.classify(SLUG, health.snapshot_sources(snap, error), {}, AT)
            self.assertEqual((result["operating_state"], result["execution_state"], result["observation"], result["assessment"]), ("running", "unknown", "partial", "unknown"))
            self.assertEqual((result["findings"], result["executions"]), ([], []))
            self.assertEqual(result["sources"][0]["observed_at"], STAMP)
            self.assertEqual(result["resources"][0]["ownership"], "unknown")
        snap["dispatcher"]["service_active"] = False
        result = health.classify(SLUG, health.snapshot_sources(snap), {}, AT)
        self.assertEqual(result["operating_state"], "scheduled waiting")
        snap["dispatcher"]["timer"]["active"] = False
        result = health.classify(SLUG, health.snapshot_sources(snap), {}, AT)
        self.assertEqual(result["operating_state"], "unknown")
        self.assertEqual(result["findings"], [])

    def test_legacy_runtime_and_dependency_failures_are_scoped_not_product_feedback(self):
        snap = {"generated_at": STAMP, "dispatcher": {"timer": {"active": True}, "runs": [{"result": "failed", "finished": STAMP}], "consecutive_failures": 1},
                "config": {"triage": {"url": "http://model:11434", "online": False}}}
        result = health.classify(SLUG, health.snapshot_sources(snap), {}, AT)
        self.assertEqual({f["condition_code"] for f in result["findings"]}, {"runtime.dispatcher_failed", "dependency.unavailable"})
        self.assertEqual(result["assessment"], "attention")
        for finding in result["findings"]:
            self.assertEqual(finding["scope"], {"kind": "factory", "id": SLUG})
            self.assertIsNone(finding["cause"])

    def test_held_resource_does_not_establish_owner_or_failure_and_terminal_owner_is_cleared(self):
        data = scheduled()
        data["resources"] = [{"id": "gpu-lock", "held": True}]
        result = self.classify(data)
        self.assertEqual((result["resources"][0]["owner"], result["resources"][0]["ownership"]), (None, "unknown"))
        self.assertEqual((result["assessment"], result["findings"]), ("normal", []))
        data["resources"][0]["owner"] = {"factory": SLUG, "execution_id": "x"}
        data["executions"] = [{"id": "x", "state": "stage-active", "stage": "gate"}]
        self.assertEqual(self.classify(data)["resources"][0]["ownership"], "known")
        data["executions"][0]["state"] = "interrupted"
        result = self.classify(data)
        self.assertEqual((result["resources"][0]["owner"], result["resources"][0]["ownership"]), (None, "unknown"))

    def test_finding_identity_survives_evidence_change_and_grouping_preserves_factory_evidence(self):
        data = scheduled()
        data["checks"] = [check()]
        original = self.classify(data)["findings"][0]
        data["checks"][0].update(detail="Connection timed out on the next observation.", observed_at="2026-09-05T12:00:01Z")
        changed = self.classify(data, at=AT + timedelta(seconds=1))["findings"][0]
        self.assertEqual((original["id"], original["scope"]), (changed["id"], changed["scope"]))
        self.assertNotEqual(original["evidence"], changed["evidence"])
        data["checks"][0]["scope"] = {"kind": "shared", "id": "host/model"}
        entries = {slug: health.classify(slug, [source(data)], {}, AT + timedelta(seconds=1)) for slug in (SLUG, "acme/other")}
        groups = health.group_findings(entries)
        self.assertEqual(len(groups), 1)
        self.assertEqual({f["factory"] for f in groups[0]["observations"]}, {SLUG, "acme/other"})
        self.assertEqual({e["factory"] for f in groups[0]["observations"] for e in f["evidence"]}, {SLUG, "acme/other"})
        data["checks"][0].pop("scope")
        local = {slug: health.classify(slug, [source(data)], {}, AT + timedelta(seconds=1)) for slug in entries}
        self.assertEqual(len(health.group_findings(local)), 2)

    def test_first_and_last_observations_require_a_declared_bounded_window(self):
        data = scheduled()
        data["checks"] = [check(first_observed_at="2026-09-05T11:59:50Z", last_observed_at=STAMP)]
        self.assertNotIn("first_observed_at", self.classify(data)["findings"][0])
        data["history"] = {"start": "2026-09-05T11:59:00Z", "end": STAMP, "complete": False, "truncated": True}
        result = self.classify(data)["findings"][0]
        self.assertEqual(result["first_observed_at"], "2026-09-05T11:59:50Z")
        self.assertEqual(result["history"], data["history"])
        data["checks"][0]["first_observed_at"] = "2026-09-04T11:59:50Z"
        self.assertNotIn("first_observed_at", self.classify(data)["findings"][0])

    def test_host_findings_need_impact_and_do_not_treat_intentional_pins_as_drift(self):
        for condition in ("host.configuration_drift", "host.configuration_invalid", "host.policy_violation"):
            with self.subTest(condition=condition):
                data = scheduled()
                data["checks"] = [check(condition=condition, resource="factory-config", detail="Required setting is absent.", impact="Configured gate cannot run.")]
                finding = self.classify(data)["findings"][0]
                self.assertEqual(finding["condition_code"], condition)
                self.assertEqual(finding["impact"], "Configured gate cannot run.")
        data["checks"] = [check(condition="host.configuration_drift", intentional=True)]
        self.assertEqual((self.classify(data)["assessment"], self.classify(data)["findings"]), ("normal", []))
        data["checks"] = [check(impact="")]
        self.assertEqual((self.classify(data)["assessment"], self.classify(data)["findings"]), ("unknown", []))
        data["checks"] = [check(condition="unknown.cause")]
        self.assertEqual(self.classify(data)["assessment"], "unknown")

    def test_newer_source_can_clear_cap_and_inputs_are_not_mutated(self):
        older = source({"dispatcher": {"capped": True}, "executions": []}, identity="old", timestamp="2026-09-05T11:59:59Z")
        sources = [source(scheduled()), older]
        before = copy.deepcopy(sources)
        result = health.classify(SLUG, sources, {}, AT)
        self.assertEqual((result["operating_state"], result["findings"]), ("scheduled waiting", []))
        self.assertEqual(sources, before)

    def test_newer_check_verdict_replaces_failed_evidence(self):
        data = scheduled()
        data["checks"] = [
            check(status="passed", observed_at=STAMP),
            check(observed_at="2026-09-05T11:59:59Z"),
        ]
        result = self.classify(data)
        self.assertEqual((result["assessment"], result["findings"]), ("normal", []))

    def test_malformed_checks_are_unknown_not_normal_or_exceptions(self):
        for checks in ([None], [check(condition=[])], [check(scope={"kind": "shared", "id": ""})]):
            with self.subTest(checks=checks):
                data = scheduled()
                data["checks"] = checks
                result = self.classify(data)
                self.assertEqual((result["observation"], result["assessment"], result["findings"]), ("partial", "unknown", []))


if __name__ == "__main__":
    unittest.main()
