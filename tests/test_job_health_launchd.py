"""Launchd-specific fleet job health regressions."""

import json
import os
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from district import host
from tests import test_streak
from tests.test_district import DistrictCase


class TestLaunchdJobHealth(DistrictCase):
    def test_undated_manager_run_does_not_use_old_files_as_run_time(self):
        data = test_streak.TestFailureStreak.register(self, "launchd", [])
        home = Path(json.loads((self.tmp / "ssh.json").read_text())
                    ["targets"]["factory@ultron.example"]["home"])
        agents = home / "Library/LaunchAgents"
        old = agents / "factory-widgets.plist"
        document = plistlib.loads(old.read_bytes())
        old.unlink()

        label = "com.mikeroysoft.factory-widgets"
        document.update(Label=label, StartInterval=600,
                        WorkingDirectory=data["repo"]["acme/widgets"]["path"])
        plist = agents / f"{label}.plist"
        plist.write_bytes(plistlib.dumps(document))
        log = Path(document["StandardOutPath"])
        log.write_text("")
        stale = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
        os.utime(log, (stale, stale))
        os.utime(plist, (stale, stale))
        self.stub("launchctl", ("print-disabled *", "{}"),
                  (f"print */{label}", "state = exited\nruns = 1\nlast exit code = 0\n"))
        host.save(data)

        _, output = self.district("status", "--json")
        entry = json.loads(output)["acme/widgets"]
        self.assertTrue(any(error["code"] == "undated_history" and error["label"] == label
                            for error in entry["pass_history_errors"]), output)
        self.assertFalse(any(alert["rule"] == "stalled" for alert in entry["failure_streaks"]), output)
