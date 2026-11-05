"""Systemd job-health edge regressions through the public status command."""

import json
from datetime import datetime, timedelta, timezone

from district import host
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
