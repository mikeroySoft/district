"""CLI, LAN projection and Atlas agree without trusting raw Factory diagnostics."""

from __future__ import annotations

import contextlib
import copy
import http.client
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from district import atlas, cli, dashboard, health, host, metrics, status  # noqa: E402
from district.dashboard_read import safe_fleet  # noqa: E402

AT = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-09-05T12:00:00Z"
SLUG = "acme/widgets"
STATES = ("schema_version", "assessment", "operating_state", "execution_state", "observation")
COLLECTIONS = ("findings", "sources", "executions", "resources", "unknowns")


def source(data, *, identity="runtime", stamp=STAMP, error=None):
    return {"id": identity, "observed_at": stamp, "cadence_seconds": 5,
            "data": data, "error": error}


def scheduled(**extra):
    return {"dispatcher": {"service_active": False, "timer_active": True},
            "executions": [], **extra}


def check(**extra):
    return {"condition": "dependency.unavailable", "resource": "model-endpoint",
            "status": "failed", "detail": "Configured endpoint refused the connection.",
            "impact": "The configured triage dependency cannot serve this factory.", **extra}


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
        }))
        self.repo = self.root / "private-checkout"
        self.repo.mkdir()
        self.table = {"path": str(self.repo), "dashboard": {"port": 8765}}
        host.save({"repo": {SLUG: self.table}})

    def observe(self, sources=None, *, snap=None, error=None, at=AT):
        """Only transport/time are controlled; CLI dispatch, registry and semantics run."""
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return at.astimezone(tz) if tz else at.replace(tzinfo=None)

        before = copy.deepcopy(sources)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(health, "datetime", Clock))
            stack.enter_context(mock.patch.object(status, "datetime", Clock))
            if sources is not None:
                stack.enter_context(mock.patch.object(status, "snapshot", side_effect=lambda slug, table: {
                    "slug": slug, "sources": copy.deepcopy(sources),
                    "snap": copy.deepcopy(snap), "error": error,
                }))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                json_code = cli.main(["status", "--json"])
            fleet = json.loads(output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                text_code = cli.main(["status"])
        self.assertEqual(sources, before)
        self.assertEqual(text_code, json_code)
        raw = fleet[SLUG]
        headings, values = [re.split(r" {2,}", line.strip()) for line in output.getvalue().splitlines()]
        row = dict(zip(headings, values))
        self.assertEqual(row["repo"], SLUG)
        for column, key in (("assessment", "assessment"), ("operating", "operating_state"),
                            ("execution", "execution_state"), ("observation", "observation")):
            self.assertEqual(row[column], raw[key])
        public = safe_fleet(fleet)[SLUG]
        # The unreconciled D07 projection makes this real view raise KeyError.
        block = atlas.factory_block(0, SLUG, public, 0)
        for key in STATES:
            self.assertEqual(public[key], raw[key], key)
            self.assertEqual(block[key], public[key], key)
        for key in (*COLLECTIONS, "projection"):
            self.assertEqual(block[key], public[key], key)
        for entry in (raw, public, block):
            self.assertNotIn("health", entry)
            self.assertNotIn("reasons", entry)
        return raw, public, block, json_code

    def assert_state(self, result, expected):
        raw, _, _, code = result
        self.assertEqual((code, *(raw[key] for key in STATES[1:])), expected)

    def test_scheduled_idle_cli_public_atlas_agree(self):
        result = self.observe([source(scheduled())])
        self.assert_state(result, (0, "normal", "scheduled waiting", "known wait", "fresh"))
        raw, public, block, _ = result
        self.assertEqual(public["findings"], [])
        self.assertEqual(public["sources"], raw["sources"])
        self.assertFalse(public["projection"]["truncated"])
        self.assertFalse(any(public["projection"]["omitted"].values()))
        self.assertEqual(public["table"]["dashboard"]["port"], 8765)
        self.assertIn("8765", dict(block["kpis"])["dashboard"])

    def test_observation_quality_keeps_runtime_and_source_age(self):
        running = {"dispatcher": {"service_active": True}, "executions": [
            {"id": "pass1/worker", "state": "stage-active", "stage": "worker"}]}
        cases = [
            ("fresh", [source(scheduled())], AT + timedelta(seconds=10),
             (0, "normal", "scheduled waiting", "known wait", "fresh")),
            ("stale", [source(scheduled())], AT + timedelta(seconds=11),
             (2, "unknown", "scheduled waiting", "known wait", "stale")),
            ("github missing", [source(running), source(None, identity="github", error="rate limited")], AT,
             (2, "unknown", "running", "stage-active", "partial")),
            ("unavailable", [source(None, error="transport timeout")], AT,
             (2, "unknown", "unknown", "unknown", "unavailable")),
            ("semantic unknown", [source(scheduled(checks=[check(status="unknown")]))], AT,
             (2, "unknown", "scheduled waiting", "known wait", "fresh")),
        ]
        for name, sources, at, expected in cases:
            with self.subTest(case=name):
                result = self.observe(sources, at=at)
                self.assert_state(result, expected)
                raw, public, _, _ = result
                self.assertEqual(public["findings"], [])
                for original, projected in zip(raw["sources"], public["sources"]):
                    for key in ("id", "observed_at", "age_seconds", "cadence_seconds", "observation"):
                        self.assertEqual(projected[key], original[key], key)
                self.assertEqual(public["sources"][0]["observed_at"], STAMP)
                self.assertEqual(public["sources"][0]["age_seconds"], (at - AT).total_seconds())
                if name == "github missing":
                    self.assertEqual([s["observation"] for s in public["sources"]], ["fresh", "unavailable"])
                    self.assertEqual(public["executions"][0]["stage"], "worker")

    def test_pause_cap_and_authoritative_stop_remain_distinct(self):
        cases = [
            ({"pause": {"recorded_at": STAMP, "reason": "operator maintenance"}},
             (0, "normal", "deliberately paused", "known wait", "fresh"), []),
            ({"capped": True, "cap_reason": "recorded failure cap"},
             (1, "attention", "capped", "known wait", "fresh"), ["scheduling.capped"]),
            ({"service_active": False, "timer_active": False, "expected_enabled": True},
             (1, "attention", "unexpectedly stopped", "unknown", "fresh"), ["scheduling.unexpected_stop"]),
            ({"service_active": False, "timer_active": False, "pause": {"reason": "unrecorded"}},
             (2, "unknown", "unknown", "unknown", "fresh"), []),
        ]
        for dispatcher, expected, conditions in cases:
            with self.subTest(dispatcher=dispatcher):
                result = self.observe([source({"dispatcher": dispatcher, "executions": []})])
                self.assert_state(result, expected)
                self.assertEqual([f["condition_code"] for f in result[1]["findings"]], conditions)

    def test_project_feedback_and_waits_are_not_mechanism_failure(self):
        project = {"tickets": [{"number": 7, "stage": "escalated", "labels": ["ready-for-human"]}],
                   "metrics": {"first_pass": 0, "bounce_rate": 1, "escalations": 12},
                   "config": {"upstream": "parent"},
                   "upstream": {"repo": "acme/parent", "behind": 4, "blocker": {"number": 8}}}
        cases = [
            ("gate", "failed", "product", "ordinary product tests failed", "normal", 0),
            ("review", "failed", "product", "review requires a revision", "normal", 0),
            ("upstream", "blocked", "product", "upstream project requires a revision", "normal", 0),
            ("worker", "known wait", None, "capacity", "normal", 0),
            ("merge", "known wait", None, "CI", "normal", 0),
            ("gate", "failed", "mechanism", "configured gate executable is missing", "attention", 1),
            ("review", "failed", None, "unreported failure cause", "unknown", 2),
        ]
        for stage, state, kind, reason, assessment, code in cases:
            with self.subTest(stage=stage, kind=kind):
                execution = {"id": "pass1/stage", "stage": stage, "state": state, "reason": reason,
                             "entered_at": "2025-01-01T00:00:00Z"}
                if kind:
                    execution["outcome_kind"] = kind
                data = {**project, "dispatcher": {"service_active": True}, "executions": [execution]}
                result = self.observe([source(data)], snap=project)
                self.assert_state(result, (code, assessment, "running", state, "fresh"))
                public = result[1]
                self.assertEqual([f["condition_code"] for f in public["findings"]],
                                 ["runtime.mechanism_unavailable"] if kind == "mechanism" else [])
                self.assertEqual(public["executions"][0]["entered_at"], execution["entered_at"])
                self.assertEqual(public["snap"]["tickets"][0]["labels"], ["ready-for-human"])
                self.assertEqual(public["snap"]["metrics"]["bounce_rate"], 1)
                self.assertEqual(public["snap"]["upstream"]["blocker"]["number"], 8)

    def test_legacy_transport_keeps_runtime_when_github_fails(self):
        snap = {"generated_at": STAMP, "version": "0.2.0", "errors": ["github: unavailable"],
                "dispatcher": {"service_active": True, "timer": {"active": True},
                               "consecutive_failures": 0, "runs": []},
                "tickets": [{"phase": "gate", "stage": "escalated", "labels": ["ready-for-human"]}],
                "metrics": {"bounce_rate": 1}, "gpu_lock_held": True,
                "config": {"upstream": "parent"}, "upstream": {"blocker": {"number": 8}}}
        # The legacy case crosses the actual subprocess/JSON adapter, not the normalized seam.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "snapshot.json").write_text(json.dumps(snap))
        executable = bin_dir / "factory"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import sys\nfrom pathlib import Path\n"
            "if sys.argv[1:] != ['dashboard', '--json']: raise SystemExit(99)\n"
            "print(Path(__file__).with_name('snapshot.json').read_text())\n"
            "raise SystemExit(1)\n")
        executable.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
            result = self.observe()
        self.assert_state(result, (2, "unknown", "running", "unknown", "partial"))
        public = result[1]
        self.assertEqual(public["findings"], [])
        self.assertEqual(public["executions"], [])
        self.assertEqual([s["id"] for s in public["sources"]], ["factory.snapshot", "factory.github"])
        self.assertEqual([s["observed_at"] for s in public["sources"]], [STAMP, STAMP])
        self.assertEqual(public["resources"][0]["owner"], None)
        self.assertEqual(public["resources"][0]["ownership"], "unknown")

    def test_held_resource_does_not_invent_owner_or_incident(self):
        result = self.observe([source(scheduled(resources=[{"id": "gpu-lock", "held": True}]))])
        self.assert_state(result, (0, "normal", "scheduled waiting", "known wait", "fresh"))
        resource = result[1]["resources"][0]
        self.assertEqual((resource["id"], resource["held"], resource["owner"], resource["ownership"]),
                         ("gpu-lock", True, None, "unknown"))
        self.assertEqual((resource["source_id"], resource["observed_at"], resource["observation"]),
                         ("runtime", STAMP, "fresh"))
        self.assertEqual(result[1]["findings"], [])

    def test_finding_identity_scope_history_and_evidence_survive_rereads(self):
        history = {"start": "2026-09-05T11:59:00Z", "end": STAMP, "complete": False, "truncated": True}
        findings = [
            check(first_observed_at="2026-09-05T11:59:50Z", last_observed_at=STAMP),
            check(condition="host.configuration_drift", resource="gate-policy",
                  scope={"kind": "shared", "id": "host/gate-policy"},
                  detail="Required gate policy differs from the recorded host policy.",
                  impact="The configured gate cannot enforce the required host policy.",
                  first_observed_at="2026-09-05T11:59:50Z", last_observed_at=STAMP),
        ]
        sources = [source(scheduled(checks=findings, history=history))]
        initial = self.observe(sources)
        self.assert_state(initial, (1, "attention", "scheduled waiting", "known wait", "fresh"))
        findings[0].update(detail="Configured endpoint timed out on the next observation.",
                           observed_at="2026-09-05T12:00:01Z")
        changed = self.observe(sources, at=AT + timedelta(seconds=1))
        stale = self.observe(sources, at=AT + timedelta(seconds=12))
        self.assert_state(stale, (1, "attention", "scheduled waiting", "known wait", "stale"))
        self.assertEqual([f["id"] for f in initial[1]["findings"]],
                         [f["id"] for f in changed[1]["findings"]])
        self.assertNotEqual(initial[1]["findings"][0]["evidence"], changed[1]["findings"][0]["evidence"])
        self.assertEqual([f["scope"] for f in changed[1]["findings"]],
                         [{"kind": "factory", "id": SLUG}, {"kind": "shared", "id": "host/gate-policy"}])
        self.assertEqual([f["severity"] for f in changed[1]["findings"]], ["error", "warning"])
        for raw, public, block, _ in (initial, changed, stale):
            rendered = unescape(json.dumps(block["kpis"]))
            for original, projected in zip(raw["findings"], public["findings"]):
                for key in ("id", "factory", "scope", "condition_code", "resource", "severity",
                            "observed_at", "impact", "cause", "evidence", "history",
                            "first_observed_at", "last_observed_at"):
                    self.assertEqual(projected[key], original[key], key)
                self.assertEqual(projected["history"], history)
                self.assertIn(projected["impact"], rendered)
                self.assertIn(projected["observed_at"], rendered)
                self.assertIn(projected["first_observed_at"], rendered)
        self.assertEqual(stale[1]["sources"][0]["observed_at"], STAMP)
        self.assertEqual(stale[1]["sources"][0]["age_seconds"], 12)
        self.assertEqual(stale[1]["findings"][0]["evidence"][0]["observation"], "stale")

    def test_missing_classification_is_unknown_not_legacy_failure(self):
        raw = self.observe([source(scheduled())])[0]
        for key in (*STATES[1:], *COLLECTIONS):
            raw.pop(key)
        raw.update(health="failing", reasons=["timer inactive", "12 open ready-for-human (#7)"])
        public = safe_fleet({SLUG: raw})[SLUG]
        block = atlas.factory_block(0, SLUG, public, 0)
        self.assertEqual(tuple(public[key] for key in STATES),
                         (1, "unknown", "unknown", "unknown", "unavailable"))
        self.assertEqual(public["findings"], [])
        for entry in (public, block):
            self.assertNotIn("health", entry)
            self.assertNotIn("reasons", entry)
            self.assertEqual(entry["assessment"], "unknown")

    def test_private_evidence_is_redacted_without_losing_identity_or_assessment(self):
        token = "ghp_PROJECTION_CREDENTIAL"
        diagnostic = "DIAGNOSTIC_CANARY"
        config_value = "CONFIGURATION_CANARY"
        source_data = "RAW_SOURCE_CANARY"
        log = "PRIVATE_LOG_CANARY"
        secret_url = f"https://operator:{token}@private.example/model"
        execution_id = f"/private/{token}/execution"
        unknown_id = f"/private/{token}/unknown"
        scope_id = f"/private/{config_value}/policy"
        self.table.update(triage={"token": token}, factory_source=f"/private/{config_value}")
        host.save({"repo": {SLUG: self.table}})
        cache = metrics.cache_path(SLUG)
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"loc": 123, "files": 4, "head": token, "logs": log,
                                     "languages": {"Python": 123, token: 999},
                                     "open_by_label": {"ready-for-human": 1, token: 999}}))
        snap = {
            "version": "0.2.0", "generated_at": STAMP, "errors": [f"password={diagnostic}"],
            "config": {"triage": {"url": secret_url}, "secret": config_value,
                       "gate_checks": ["tests", token]},
            "dispatcher": {"service_active": True, "runs": [
                {"result": "done", "lines": [log], "stderr": token}]},
            "tickets": [{"number": 7, "title": log, "labels": ["ready-for-human", token]}],
            "logs": [log],
        }
        data = {
            "dispatcher": {"service_active": True},
            "executions": [
                {"id": execution_id, "state": "stage-active", "stage": "worker",
                 "reason": f"Authorization: Bearer {token}", "reference": secret_url},
                {"id": unknown_id, "state": "unknown"},
            ],
            "resources": [{"id": secret_url, "held": True,
                           "owner": {"factory": SLUG, "execution_id": execution_id}}],
            "checks": [check(resource=secret_url, scope={"kind": "shared", "id": scope_id},
                             detail=f"Endpoint failed: password={diagnostic}",
                             impact=f"Cannot load {secret_url}", cause=f"api_key={token}",
                             reference=secret_url)],
            "raw": source_data, "logs": [log], "configuration": config_value,
        }
        sources = [source(data, identity=secret_url,
                          error=json.dumps({"password": diagnostic, "api_key": diagnostic}))]
        result = self.observe(sources, snap=snap, error=f"password={diagnostic}")
        self.assert_state(result, (1, "attention", "running", "stage-active", "partial"))
        raw, public, block, _ = result
        self.assertIn(token, json.dumps(raw))  # The hostile fixture actually reaches the private CLI.
        self.assertEqual(public["metrics"]["loc"], 123)
        self.assertEqual(public["snap"]["config"]["gate_checks"], ["tests"])
        finding = public["findings"][0]
        for identity in (public["sources"][0]["id"], public["executions"][0]["id"],
                         public["resources"][0]["id"], finding["resource"], finding["scope"]["id"]):
            self.assertRegex(identity, r"^[A-Za-z:._-]*[0-9a-f]{64}$")
        self.assertEqual(finding["scope"]["kind"], "shared")
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["condition_code"], "dependency.unavailable")
        self.assertEqual(finding["evidence"][0]["observed_at"], STAMP)
        self.assertEqual(finding["evidence"][0]["observation"], "partial")
        self.assertEqual(finding["evidence"][0]["source_id"], public["sources"][0]["id"])
        self.assertEqual(public["resources"][0]["ownership"], "known")
        self.assertEqual(public["resources"][0]["owner"]["execution_id"], public["executions"][0]["id"])
        self.assertTrue(all("data" not in item for item in public["sources"]))
        data["checks"][0]["detail"] = f"Next endpoint observation: password={diagnostic}"
        reread = self.observe(sources, snap=snap, error=f"password={diagnostic}", at=AT + timedelta(seconds=1))[1]
        self.assertEqual(finding["id"], reread["findings"][0]["id"])
        self.assertEqual(finding["scope"], reread["findings"][0]["scope"])
        self.assertEqual(public["sources"][0]["id"], reread["sources"][0]["id"])
        self.assertEqual(public["resources"][0]["id"], reread["resources"][0]["id"])

        # Exercise the actual public routes too: their HTML and embedded DATA must not bypass the projection.
        collector = mock.Mock(
            read=mock.Mock(return_value=(1, {SLUG: raw})),
            runtime_interval=5, full_interval=30, timeout=10, concurrency=4,
        )
        bodies = [json.dumps(public), json.dumps(block), atlas.page({SLUG: public}, AT)]
        with mock.patch.object(status, "fleet", side_effect=AssertionError("request collected fleet")):
            with ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler) as server:
                server.collector = collector
                worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
                worker.start()
                try:
                    for route in ("/api/fleet", "/"):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                        try:
                            connection.request("GET", route)
                            reply = connection.getresponse()
                            body = reply.read().decode()
                            self.assertEqual(reply.status, 200, body)
                            if route == "/api/fleet":
                                self.assertEqual(json.loads(body)["fleet"][SLUG], public)
                            bodies.append(body)
                        finally:
                            connection.close()
                finally:
                    server.shutdown()
                    worker.join(timeout=5)
        for body in bodies:
            for private in (token, diagnostic, config_value, source_data, log, str(self.repo), secret_url, scope_id):
                self.assertNotIn(private, body)

    def test_event_projection_preserves_safe_sequence_within_event_limit(self):
        raw = {"activity": {"events": [
            {"event_id": f"event-{i}", "sequence": i} for i in range(514)
        ]}}
        events = safe_fleet({SLUG: raw})[SLUG]["activity"]["events"]
        self.assertEqual([(event["event_id"], event["sequence"]) for event in events],
                         [(f"event-{i}", i) for i in range(512)])
        for value, expected in ((2**53 - 1, 2**53 - 1), (2**53, None), (-1, None),
                                (True, None), ("42", None), (None, None),
                                (float("inf"), None), (float("nan"), None)):
            with self.subTest(sequence=value):
                raw["activity"]["events"] = [{"event_id": "event-1", "sequence": value}]
                event = safe_fleet({SLUG: raw})[SLUG]["activity"]["events"][0]
                self.assertEqual(event["sequence"], expected)

    def test_collection_bounds_are_disclosed_without_reclassifying(self):
        count = 35
        checks = [check(resource=f"dependency-{i}") for i in range(count)]
        data = scheduled(checks=checks,
                         executions=[{"id": f"execution-{i}", "state": "unknown"} for i in range(count)],
                         resources=[{"id": f"resource-{i}", "held": True} for i in range(count)])
        sources = [source(data, identity="runtime-0")]
        sources += [source(scheduled(checks=[checks[0]]), identity=f"runtime-{i}") for i in range(1, count - 1)]
        sources.append(source(None, identity="github", error="unavailable"))
        raw, public, block, code = self.observe(sources)
        self.assertEqual(code, 1)
        self.assertEqual((raw["assessment"], raw["observation"]), ("attention", "partial"))
        # The omitted unavailable source still governs the shared observation, not a view's surviving records.
        self.assertTrue(all(item["observation"] == "fresh" for item in public["sources"]))
        self.assertEqual((public["assessment"], public["observation"]), ("attention", "partial"))
        self.assertTrue(public["projection"]["truncated"])
        for key in COLLECTIONS:
            self.assertEqual(len(raw[key]), count, key)
            self.assertEqual(len(public[key]), 32, key)
            self.assertEqual(public["projection"]["omitted"][key], count - 32, key)
        original, finding = raw["findings"][0], public["findings"][0]
        self.assertEqual(len(original["evidence"]), count - 1)
        self.assertEqual(len(finding["evidence"]), 8)
        omitted = count - 1 - 8
        self.assertEqual(public["projection"]["omitted"]["evidence"], omitted)
        self.assertEqual(finding["projection"], {"truncated": True, "omitted": {"evidence": omitted}})
        self.assertEqual(block["findings"][0]["projection"], finding["projection"])
        self.assertEqual(finding["id"], original["id"])

    def test_prose_and_fleet_limits_preserve_supported_assessment(self):
        prose = "Configured endpoint did not answer the latest supported observation. " * 40
        result = self.observe([source(scheduled(checks=[check(detail=prose, impact=prose, cause=prose)]))])
        self.assert_state(result, (1, "attention", "scheduled waiting", "known wait", "fresh"))
        finding = result[1]["findings"][0]
        for text in (finding["impact"], finding["cause"], finding["evidence"][0]["detail"]):
            self.assertLessEqual(len(text), 512)
            self.assertNotEqual(text, prose)
        raw = self.observe([source(scheduled())])[0]
        fleet = {f"acme/factory-{i}": raw for i in range(66)}
        public = safe_fleet(fleet)
        self.assertEqual(len(public), 64)
        for entry in public.values():
            self.assertEqual((entry["assessment"], entry["observation"]), ("normal", "fresh"))
            self.assertTrue(entry["projection"]["truncated"])
            self.assertEqual(entry["projection"]["omitted"]["factories"], 2)


if __name__ == "__main__":
    unittest.main()
