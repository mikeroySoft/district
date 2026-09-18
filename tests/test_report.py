"""Consumer-level checks for the read-only Markdown fleet report."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from datetime import datetime, timezone
from unittest import mock

from district import report
from district import host, metrics
from test_collector import runtime
from test_district import DistrictCase, dashboard


AT = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def entry(*, assessment="normal", observed="2026-09-17T11:59:00Z", cache=True,
          snapshot=True, quality="fresh") -> dict:
    snap = None
    if snapshot:
        snap = {
            "generated_at": observed,
            "tickets": [{"stage": "escalated"}, {"stage": "worker"}],
            "metrics": {
                "first_pass": 0.75,
                "first_pass_numerator": 3,
                "first_pass_denominator": 4,
                "bounce_rate": 0.25,
                "bounce_numerator": 1,
                "bounce_denominator": 4,
                "window": {"start": "2026-09-10T00:00:00Z", "end": "2026-09-17T00:00:00Z"},
                "escalations": 8,
                "escalations_7d": 2,
                "human_resolutions": {"count": 3, "attribution": "shared login; actor unknown"},
            },
        }
    metrics = None
    if cache:
        metrics = {
            "collected_at": "2026-09-17T11:30:00Z", "merged_prs_30d": 5,
            "agent_prs_30d": 2, "open_prs": 1,
            "open_by_label": {"ready-for-human": 4},
        }
    return {
        "assessment": assessment, "observation": quality, "snap": snap, "metrics": metrics,
        "error": None if snapshot else "factory dashboard unavailable",
        "sources": [{"id": "factory.snapshot", "observed_at": observed if snapshot else None,
                     "observation": quality if snapshot else "unavailable",
                     "error": None if snapshot else "factory dashboard unavailable"}],
    }


class ReportTest(unittest.TestCase):
    def test_complete_report_is_deterministic_and_pools_compatible_rates(self) -> None:
        entries = {"z/repo": entry(), "a/repo": entry(observed="2026-09-17T11:58:00Z")}
        output = report.render(entries, AT)
        self.assertEqual(output, report.render(entries, AT))
        self.assertLess(output.index("## a/repo"), output.index("## z/repo"))
        self.assertIn("Generated: 2026-09-17T12:00:00Z", output)
        self.assertIn("Registered repositories: 2", output)
        self.assertIn("Merged PRs (trailing 30 days, bounded at 500): 5", output)
        self.assertIn("Merged PRs from `agent/` branches (trailing 30 days): 2", output)
        self.assertNotIn("autonomously delivered", output.lower())
        self.assertIn("Current escalated tickets: 1", output)
        self.assertIn("Trailing-seven-day escalations: 2", output)
        self.assertIn("Human resolutions: count 3; attribution shared login; actor unknown", output)
        self.assertIn("First-gate pass aggregate: 75% (6/8)", output)
        self.assertIn("Review-bounce aggregate: 25% (2/8)", output)
        self.assertIn("Observation-time range: 2026-09-17T11:58:00Z to 2026-09-17T11:59:00Z", output)
        self.assertIn("GitHub query limits: merged PRs 500; open PRs 1000; caches have no completeness flag.", output)

    def test_partial_sources_and_incompatible_rates_remain_explicit(self) -> None:
        incompatible = entry()
        incompatible["snap"]["metrics"]["window"]["start"] = "2026-09-01T00:00:00Z"
        missing = entry(assessment="unknown", cache=False, snapshot=False, quality="unavailable")
        stale = entry(observed="2026-09-10T00:00:00Z")
        stale["metrics"]["collected_at"] = "2026-09-10T00:00:00Z"
        output = report.render({"a/complete": entry(), "b/different": incompatible,
                                "c/missing": missing, "d/stale": stale}, AT)
        self.assertIn("## c/missing", output)
        self.assertIn("Cached GitHub metrics: unavailable", output)
        self.assertIn("Factory snapshot: unavailable", output)
        self.assertIn("Cached GitHub metrics: stale", output)
        self.assertIn("Comparable count coverage: 3/4 repositories", output)
        self.assertIn("Totals below are partial", output)
        self.assertIn("First-gate pass aggregate: unavailable (incompatible or unspecified producer windows/cohorts)", output)
        self.assertIn("Review-bounce aggregate: unavailable (incompatible or unspecified producer windows/cohorts)", output)

    def test_missing_and_zero_rate_denominators_are_not_derived(self) -> None:
        zero = entry()
        zero["snap"]["metrics"].update(first_pass=0, first_pass_numerator=0,
                                         first_pass_denominator=0)
        zero["snap"]["metrics"].pop("bounce_numerator")
        zero["snap"]["metrics"].pop("bounce_denominator")
        output = report.render({"a/zero": zero}, AT)
        self.assertIn("First-gate pass: unavailable (zero denominator)", output)
        self.assertIn("Review-bounce rate: 25% (denominator unavailable; producer window", output)
        self.assertIn("First-gate pass aggregate: unavailable", output)
        self.assertIn("Review-bounce aggregate: unavailable", output)

    def test_empty_fleet_and_operational_exit_semantics(self) -> None:
        self.assertIn("Registered repositories: 0", report.render({}, AT))
        cases = [({}, 0), ({"a/repo": entry(assessment="unknown")}, 2),
                 ({"a/repo": entry(assessment="attention"),
                   "b/repo": entry(assessment="unknown")}, 1)]
        for fleet, expected in cases:
            out = io.StringIO()
            with self.subTest(expected=expected), \
                 mock.patch.object(report.host, "load", return_value={"repo": {}}), \
                 mock.patch.object(report.status, "fleet", return_value=fleet), \
                 mock.patch.object(report, "_now", return_value=AT), \
                 contextlib.redirect_stdout(out):
                self.assertEqual(report.main([]), expected)
            self.assertTrue(out.getvalue().startswith("# District fleet review"))

    def test_invalid_invocation_uses_argparse(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            report.main(["--week"])
        self.assertEqual(raised.exception.code, 2)


class ReportCLITest(DistrictCase):
    def test_report_reads_disposable_fleet_without_mutation_or_collection(self) -> None:
        repo = self.repo(toml="")
        self.units("acme/widgets")
        for case in ("complete", "partial", "empty"):
            with self.subTest(case=case):
                host.save({"repo": {} if case == "empty" else {
                    "acme/widgets": {"path": str(repo)}}})
                snap = dashboard()
                cache = metrics.cache_path("acme/widgets")
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({
                    "collected_at": snap["generated_at"],
                    "merged_prs_30d": 3, "agent_prs_30d": 1, "open_prs": 2,
                }))
                self.stub("factory",
                          ("dashboard --runtime-json", json.dumps(runtime(
                              "acme/widgets", stamp=snap["generated_at"]))),
                          ("dashboard --json", "{not json" if case == "partial"
                           else json.dumps(snap), 1 if case == "partial" else 0),
                          ("*", "unexpected Factory command", 99))
                for name in ("gh", "systemctl", "uv"):
                    self.stub(name, ("*", "unexpected management/collector command", 99))
                for log in self.bin.glob("*.log"):
                    log.unlink()
                roots = [Path(os.environ["XDG_CONFIG_HOME"]),
                         Path(os.environ["XDG_CACHE_HOME"]), repo]
                before = {p: p.read_bytes() for root in roots
                          for p in root.rglob("*") if p.is_file()}
                registry = host.load()
                code, out = self.district("report")
                self.assertEqual(code, 2 if case == "partial" else 0, out)
                self.assertIn(f"Registered repositories: {0 if case == 'empty' else 1}", out)
                if case != "empty":
                    self.assertIn("## acme/widgets", out)
                    self.assertIn("Merged PRs (trailing 30 days, bounded at 500): 3", out)
                    self.assertIn("Factory snapshot: " + (
                        "unavailable" if case == "partial" else "available"), out)
                self.assertCountEqual(self.calls("factory"), [] if case == "empty" else [
                    "dashboard --runtime-json", "dashboard --json"])
                for name in ("gh", "systemctl", "uv"):
                    self.assertEqual(self.calls(name), [])
                self.assertEqual(host.load(), registry)
                self.assertEqual({p: p.read_bytes() for root in roots
                                  for p in root.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
