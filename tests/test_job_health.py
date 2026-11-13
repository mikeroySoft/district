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
        self.stub("systemctl", ("list-units *", "factory-widgets.service loaded inactive dead\n"),
                  ("*", "", 0))
        return data

    def entry(self):
        code, out = self.district("status", "--json")
        return code, json.loads(out)["acme/widgets"]

    def test_healthy_systemd_has_no_alert(self):
        self.systemd([0, 0, 0])
        _, entry = self.entry()
        self.assertEqual(entry["failure_streaks"], [])

    def test_systemd_three_failed_journal_runs_alert_with_first_error(self):
        self.systemd([1, 1, 1])
        code, entry = self.entry()
        self.assertEqual(code, 1, entry)
        self.assertEqual(entry["failure_streaks"][0]["count"], 3)
        self.assertIn(ERROR, entry["failure_streaks"][0]["first_error"])

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

    def test_unassigned_host_and_unrecognized_job_are_findings(self):
        self.launchd([1, 1, 1], assigned=False, label="factory-orphan")
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertIn("host_without_repos", out)
        self.assertIn("unrecognized_factory_job", out)
        self.assertIn("factory-orphan", out)

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

    def test_launchd_native_history_reports_not_yet_supported(self):
        # Native launchd run timestamps (the shell's local-zone `date` format) are out of scope and
        # tracked in district#204. They must surface as an explicit gap, never a streak, and the repo
        # must not be reported healthy on the strength of that absent, unresolvable evidence.
        self.launchd([1, 1, 1])
        code, entry = self.entry()
        self.assertEqual(entry["failure_streaks"], [], entry)
        self.assertNotEqual(entry["assessment"], "normal", entry)
        self.assertTrue(any(gap["code"] == "launchd_history_unsupported"
                            for gap in entry["pass_history_errors"]), entry)
        _, out = self.district("status")
        self.assertIn("not yet supported for launchd hosts (see district#204)", out)
