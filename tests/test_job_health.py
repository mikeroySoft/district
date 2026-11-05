"""Fleet job health through the public status/doctor commands and real transports."""

import json
import os
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from district import host
from tests.test_district import DistrictCase
from tests import test_streak
from tests.test_streak import ERROR


class TestJobHealth(DistrictCase):
    def launchd(self, codes, *, age=0, dated=True, assigned=True, label="com.mikeroysoft.factory-widgets"):
        data = test_streak.TestFailureStreak.register(self, "launchd", codes)
        data.pop("defaults")
        home = Path(json.loads((self.tmp / "ssh.json").read_text())["targets"]["factory@ultron.example"]["home"])
        agents = home / "Library/LaunchAgents"
        old = agents / "factory-widgets.plist"
        doc = plistlib.loads(old.read_bytes())
        old.unlink()
        doc.update(Label=label, StartInterval=600, WorkingDirectory=data["repo"]["acme/widgets"]["path"])
        (agents / f"{label}.plist").write_bytes(plistlib.dumps(doc))
        logfile = Path(doc["StandardOutPath"])
        at = datetime.now(timezone.utc) - timedelta(hours=age, minutes=10 * len(codes))
        lines = []
        for i, code in enumerate(codes):
            when = (at + timedelta(minutes=10 * (i + 1))).astimezone(timezone(timedelta(hours=-8)))
            stamp = when.strftime("%a %b %e %H:%M:%S PST %Y") if dated else "undated"
            lines.append(f"=== pass {stamp}\ntriage OK\n{ERROR}\nwatch OK\n=== end rc={code}")
        logfile.write_text("\n".join(lines) + "\n")
        stamp = (datetime.now(timezone.utc) - timedelta(hours=age)).timestamp()
        os.utime(logfile, (stamp, stamp))
        self.stub("launchctl", ("print-disabled *", "{}"),
                  (f"print */{label}", f"state = exited\nruns = {len(codes)}\nlast exit code = {codes[-1] if codes else 0}\n"))
        if not assigned:
            data["repo"] = {}
        host.save(data)
        return data, logfile

    def systemd(self, codes, *, age=0, enabled=True):
        data = test_streak.TestFailureStreak.register(self, "systemd", [])
        data.pop("defaults")
        host.save(data)
        at = datetime.now(timezone.utc) - timedelta(hours=age, minutes=10 * len(codes))
        records = []
        for i, code in enumerate(codes):
            when = at + timedelta(minutes=10 * (i + 1))
            records.extend([
                {"__REALTIME_TIMESTAMP": str(int(when.timestamp() * 1e6) - 1),
                 "_SYSTEMD_USER_UNIT": "factory-widgets.service", "MESSAGE": ERROR},
                {"__REALTIME_TIMESTAMP": str(int(when.timestamp() * 1e6)),
                 "USER_UNIT": "factory-widgets.service", "ExecMainStatus": str(code),
                 "RESULT": "exit-code" if code else "success",
                 "MESSAGE_ID": "be02cf6855d2428ba40df7e9d022f03d" if code else "39f53479d3a045ac8e11786248231fbf",
                 "MESSAGE": "Failed" if code else "Finished"},
            ])
        self.stub("journalctl", ("*", "\n".join(map(json.dumps, records))))
        when = at + timedelta(minutes=10 * len(codes))
        self.stub("systemctl", ("list-units *", "factory-widgets.service loaded inactive dead\n"),
                  ("show factory-widgets.service *", f"Id=factory-widgets.service\nExecMainStatus={codes[-1] if codes else 0}\nResult={'exit-code' if codes and codes[-1] else 'success'}\nExecMainStartTimestamp={when.isoformat()}\n"),
                  ("show factory-widgets.timer *", f"Id=factory-widgets.timer\nUnitFileState={'enabled' if enabled else 'disabled'}\nLastTriggerUSec={when.isoformat()}\n"))
        return data

    def entry(self):
        code, out = self.district("status", "--json")
        return code, json.loads(out)["acme/widgets"]

    def test_launchd_plain_date_three_dispatch_failures_alert(self):
        self.launchd([1, 1, 1])
        code, entry = self.entry()
        self.assertEqual(code, 1, entry)
        alert = entry["failure_streaks"][0]
        self.assertEqual((alert["host"], alert["label"], alert["count"]),
                         ("ultron", "com.mikeroysoft.factory-widgets", 3))
        self.assertIn(ERROR, alert["first_error"])
        self.assertEqual(entry["assessment"], "attention")
        _, out = self.district("status")
        for text in ("ultron", alert["label"], "3", ERROR):
            self.assertIn(text, out)
        _, out = self.district("doctor", "--json")
        self.assertTrue(any(row["status"] == "FAIL" and ERROR in row["detail"]
                            for row in json.loads(out)["rows"]))

    def test_healthy_launchd_has_no_alert(self):
        self.launchd([0, 0, 0])
        _, entry = self.entry()
        self.assertEqual(entry["failure_streaks"], [])

    def test_undated_runs_are_counted_and_timing_gap_is_visible(self):
        self.launchd([1, 1, 1], dated=False)
        code, entry = self.entry()
        self.assertEqual(code, 1, entry)
        self.assertEqual(entry["failure_streaks"][0]["count"], 3)
        self.assertIn("undated", str(entry["pass_history_errors"]))

    def test_systemd_three_failed_exec_status_runs_alert(self):
        self.systemd([1, 1, 1])
        code, entry = self.entry()
        self.assertEqual(code, 1, entry)
        self.assertEqual(entry["failure_streaks"][0]["count"], 3)
        self.assertIn(ERROR, entry["failure_streaks"][0]["first_error"])

    def test_healthy_systemd_has_no_alert(self):
        self.systemd([0, 0, 0])
        _, entry = self.entry()
        self.assertEqual(entry["failure_streaks"], [])

    def test_more_than_half_failed_in_six_hours_even_after_success(self):
        for backend in (self.launchd, self.systemd):
            with self.subTest(backend=backend.__name__):
                backend([1, 0, 1, 1, 0])
                code, entry = self.entry()
                self.assertEqual(code, 1, entry)
                rate = next(a for a in entry["failure_streaks"] if a["rule"] == "failure_rate")
                self.assertEqual((rate["count"], rate["total"], rate["window_hours"]), (3, 5, 6))

    def test_half_failed_and_old_failures_do_not_trigger_rate(self):
        self.systemd([1, 0])
        self.assertEqual(self.entry()[1]["failure_streaks"], [])
        self.systemd([1, 0, 1, 1, 0], age=7, enabled=False)
        self.assertEqual(self.entry()[1]["failure_streaks"], [])

    def test_enabled_but_stalled_job_alerts_on_both_backends(self):
        for backend in (self.launchd, self.systemd):
            with self.subTest(backend=backend.__name__):
                backend([0], age=3)
                code, entry = self.entry()
                self.assertEqual(code, 1, entry)
                stalled = next(a for a in entry["failure_streaks"] if a["rule"] == "stalled")
                self.assertEqual(stalled["window_hours"], 2)
                self.assertEqual(stalled["count"], 0)

    def test_unassigned_host_and_unrecognized_job_are_findings(self):
        self.launchd([1, 1, 1], assigned=False, label="factory-orphan")
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertIn("host_without_repos", out)
        self.assertIn("unrecognized_factory_job", out)
        self.assertIn("factory-orphan", out)
        self.assertIn(ERROR, out)

    def test_missing_journalctl_is_named_in_status_and_health(self):
        self.systemd([0])
        self.stub("journalctl", ("*", "journalctl: command not found", 127))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertIn("journalctl_unavailable", out)
        _, out = self.district("doctor", "--json")
        self.assertIn("journalctl_unavailable", out)

    def test_dashboard_import_missing_journalctl_becomes_named_finding(self):
        self.systemd([0])
        self.stub("factory", ("dashboard --json", "", 1))
        # Match the real failure where Factory writes its traceback on stderr.
        executable = self.bin / "factory"
        executable.write_text(executable.read_text().replace(
            "sys.stdout.write(out)",
            "sys.stderr.write(\"FileNotFoundError: [Errno 2] No such file or directory: 'journalctl'\\n\")"))
        _, entry = self.entry()
        self.assertIn("journalctl_unavailable", str(entry["findings"]))

    def test_systemd_native_timestamp_detects_stall_without_journal_history(self):
        self.systemd([0])
        when = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%a %Y-%m-%d %H:%M:%S UTC")
        self.stub("journalctl", ("*", ""))
        self.stub("systemctl", ("show factory-widgets.service *",
                               f"ExecMainStatus=0\nResult=success\nExecMainStartTimestamp={when}\n"),
                  ("show factory-widgets.timer *", f"UnitFileState=enabled\nLastTriggerUSec={when}\n"))
        code, entry = self.entry()
        self.assertEqual(code, 1, entry)
        self.assertEqual(entry["failure_streaks"][0]["rule"], "stalled")

    def test_manager_exit_does_not_invent_historical_runs(self):
        self.launchd([])
        self.stub("launchctl", ("print */com.mikeroysoft.factory-widgets",
                               "state = exited\nruns = 300\nlast exit code = 1\n"))
        _, entry = self.entry()
        self.assertFalse(any(a["rule"] == "failure_streak" for a in entry["failure_streaks"]))
        rate = next(a for a in entry["failure_streaks"] if a["rule"] == "failure_rate")
        self.assertEqual((rate["count"], rate["total"], rate["undated_count"]), (1, 1, 1))

    def test_disabled_or_running_launchd_does_not_stall(self):
        self.launchd([0], age=3)
        for state, disabled in (("exited", "true"), ("running", "false")):
            with self.subTest(state=state):
                self.stub("launchctl", ("print-disabled *", f'"com.mikeroysoft.factory-widgets" => {disabled}'),
                          ("print */com.mikeroysoft.factory-widgets",
                           f"state = {state}\nruns = 1\nlast exit code = 0\n"))
                self.assertEqual(self.entry()[1]["failure_streaks"], [])

    def test_streak_and_rate_share_one_finding_with_both_evidence_windows(self):
        self.launchd([1, 1, 1])
        _, entry = self.entry()
        findings = [f for f in entry["findings"] if f["condition_code"] == "runtime.dispatcher_failed"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0]["evidence"]), 2)

    def test_exact_job_label_wins_over_shared_working_directory(self):
        data = self.systemd([1, 1, 1])
        data["repo"]["acme/other"] = dict(data["repo"]["acme/widgets"])
        host.save(data)
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        fleet = json.loads(out)
        self.assertNotIn("host:local", fleet)
        self.assertEqual(fleet["acme/widgets"]["failure_streaks"][0]["count"], 3)
        self.assertEqual(fleet["acme/other"]["failure_streaks"], [])
