"""Systemd job-health edge regressions through the public status command."""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from district import health, host
from tests.test_district import DistrictCase


UNIT_FAILED = "be02cf6855d2428ba40df7e9d022f03d"


class TestSystemdJobHealth(DistrictCase):
    def configure(self, records, service, timer):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        self.stub("journalctl", ("*", "\n".join(map(json.dumps, records))))
        self.stub(
            "systemctl",
            ("list-units *", "factory-widgets.service loaded inactive dead\n"),
            ("list-unit-files *", "factory-widgets.service enabled\n"),
            ("show factory-widgets.service *", service),
            ("show factory-widgets.timer *", timer),
        )
        _, output = self.district("status", "--json")
        return json.loads(output)["acme/widgets"]

    def test_multiline_journal_message_reports_first_error_line(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        records = []
        for index in range(3):
            at = now - timedelta(minutes=10 * (2 - index))
            records.extend([
                {
                    "__REALTIME_TIMESTAMP": str(int(at.timestamp() * 1_000_000) - 1),
                    "_SYSTEMD_USER_UNIT": "factory-widgets.service",
                    "MESSAGE": "ERROR: first failure line\nsecondary context",
                },
                {
                    "__REALTIME_TIMESTAMP": str(int(at.timestamp() * 1_000_000)),
                    "USER_UNIT": "factory-widgets.service",
                    "ExecMainStatus": "1",
                    "RESULT": "exit-code",
                    "MESSAGE_ID": UNIT_FAILED,
                    "MESSAGE": "Failed",
                },
            ])
        entry = self.configure(
            records,
            f"ExecMainStatus=1\nResult=exit-code\nExecMainStartTimestamp={now.isoformat()}\n"
            f"ExecMainExitTimestamp={now.isoformat()}\nActiveState=inactive\n",
            f"UnitFileState=enabled\nActiveState=active\nLastTriggerUSec={now.isoformat()}\n",
        )

        alert = next(item for item in entry["failure_streaks"] if item["rule"] == "failure_streak")
        self.assertEqual(alert["first_error"], "ERROR: first failure line")

    def test_current_exec_failure_keeps_pending_journal_error(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        error = "RuntimeError: dispatch crashed"
        entry = self.configure(
            [{
                "__REALTIME_TIMESTAMP": str(int(now.timestamp() * 1_000_000) - 1),
                "_SYSTEMD_USER_UNIT": "factory-widgets.service",
                "MESSAGE": error,
            }],
            f"ExecMainStatus=1\nResult=exit-code\nExecMainStartTimestamp={now.isoformat()}\n"
            f"ExecMainExitTimestamp={now.isoformat()}\nActiveState=inactive\n",
            f"UnitFileState=enabled\nActiveState=active\nLastTriggerUSec={now.isoformat()}\n",
        )

        alert = next(item for item in entry["failure_streaks"] if item["rule"] == "failure_rate")
        self.assertEqual(alert["first_error"], error)

    def test_never_run_enabled_runtime_timer_stalls(self):
        activated = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
        entry = self.configure(
            [],
            "ExecMainStatus=0\nResult=success\nExecMainStartTimestamp=\n"
            "ExecMainExitTimestamp=\nActiveState=inactive\n",
            "UnitFileState=enabled-runtime\nActiveState=active\nLastTriggerUSec=\n"
            f"ActiveEnterTimestamp={activated.isoformat()}\n",
        )

        stalled = next(item for item in entry["failure_streaks"] if item["rule"] == "stalled")
        self.assertIsNone(stalled["last_run_at"])

    def test_local_timezone_abbreviations_still_alert(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        now = datetime.now(timezone.utc).replace(microsecond=0)
        ran = now - timedelta(hours=3)
        def instant(ts):
            return datetime.fromisoformat(ts).astimezone(timezone.utc)
        results = {}
        for abbr, offset in (("UTC", 0), ("CET", 1), ("JST", 9)):
            wall = (ran + timedelta(hours=offset)).strftime("%Y-%m-%d %H:%M:%S")
            self.stub("journalctl", ("*", ""))
            self.stub(
                "systemctl",
                ("list-units *", "factory-widgets.service loaded inactive dead\n"),
                ("list-unit-files *", "factory-widgets.service enabled\n"),
                ("show factory-widgets.service *",
                 f"ExecMainStatus=1\nResult=exit-code\nExecMainStartTimestamp=Fri {wall} {abbr}\n"
                 f"ExecMainExitTimestamp=Fri {wall} {abbr}\nActiveState=inactive\n"),
                ("show factory-widgets.timer *",
                 f"UnitFileState=enabled\nActiveState=active\nLastTriggerUSec=Fri {wall} {abbr}\n"),
            )
            _, output = self.district("status", "--json")
            streaks = json.loads(output)["acme/widgets"]["failure_streaks"]
            rate = next(a for a in streaks if a["rule"] == "failure_rate")
            stalled = next(a for a in streaks if a["rule"] == "stalled")
            results[abbr] = (rate["count"], rate["total"], instant(rate["last_run_at"]))
            self.assertEqual(instant(stalled["last_run_at"]), ran)
        self.assertEqual(results["CET"], results["UTC"])
        self.assertEqual(results["JST"], results["UTC"])
        self.assertEqual(results["UTC"], (1, 1, ran))

    def test_unknown_timezone_abbreviation_surfaces_gap(self):
        ran = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        wall = ran.strftime("%Y-%m-%d %H:%M:%S")
        entry = self.configure(
            [],
            f"ExecMainStatus=1\nResult=exit-code\nExecMainStartTimestamp=Fri {wall} XYZ\n"
            f"ExecMainExitTimestamp=Fri {wall} XYZ\nActiveState=inactive\n",
            f"UnitFileState=enabled\nActiveState=active\nLastTriggerUSec=Fri {wall} XYZ\n",
        )
        self.assertTrue(any(g["code"] == "timestamp_unparseable"
                            for g in entry["pass_history_errors"]))

    def test_ambiguous_zone_failure_is_a_gap_not_a_mistimed_alert(self):
        # Reviewer fixture: a failed run stamped 17:00 CST. CST is ambiguous (US/China/Cuba), so it
        # must surface as a timestamp_unparseable gap, never a guessed instant that mistimes alerts.
        entry = self.configure(
            [],
            "ExecMainStatus=1\nResult=exit-code\nExecMainStartTimestamp=Fri 2026-11-04 17:00:00 CST\n"
            "ExecMainExitTimestamp=Fri 2026-11-04 17:00:00 CST\nActiveState=inactive\n",
            "UnitFileState=enabled\nActiveState=active\nLastTriggerUSec=Fri 2026-11-04 17:00:00 CST\n",
        )
        self.assertTrue(any(g["code"] == "timestamp_unparseable"
                            for g in entry["pass_history_errors"]))
        self.assertEqual(entry["failure_streaks"], [])

    def test_history_gap_degrades_assessment_only_under_unhealthy(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        ran = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
        wall = ran.strftime("%Y-%m-%d %H:%M:%S")
        self.stub("journalctl", ("*", ""))
        self.stub(
            "systemctl",
            ("list-units *", "factory-widgets.service loaded inactive dead\n"),
            ("list-unit-files *", "factory-widgets.service enabled\n"),
            ("show factory-widgets.service *",
             f"ExecMainStatus=1\nResult=exit-code\nExecMainStartTimestamp=Fri {wall} XYZ\n"
             f"ExecMainExitTimestamp=Fri {wall} XYZ\nActiveState=inactive\n"),
            ("show factory-widgets.timer *",
             f"UnitFileState=enabled\nActiveState=active\nLastTriggerUSec=Fri {wall} XYZ\n"),
        )
        # Force an otherwise-healthy snapshot so the only defect is the history gap.
        real = health.classify
        normal = lambda slug, sources, table: {**real(slug, sources, table), "assessment": "normal"}  # noqa: E731
        with mock.patch.object(health, "classify", normal):
            default_code, default_out = self.district("status", "--json")
            unhealthy_code, unhealthy_out = self.district("status", "--json", "--unhealthy")
        entry = json.loads(default_out)["acme/widgets"]
        # Incomplete native observation is not 'normal': the assessment degrades to unknown...
        self.assertEqual(entry["assessment"], "unknown")
        self.assertTrue(any(g["code"] == "timestamp_unparseable" for g in entry["pass_history_errors"]))
        self.assertNotIn("_incomplete_history", entry)  # internal marker never leaks into --json
        # ...the repo surfaces under --unhealthy with a non-zero exit...
        unhealthy = json.loads(unhealthy_out)
        self.assertEqual(unhealthy["acme/widgets"]["assessment"], "unknown")
        self.assertNotEqual(unhealthy_code, 0)
        # ...but the lenient default `district status` exit stays 0 (unchanged) for these gaps.
        self.assertEqual(default_code, 0, default_out)
