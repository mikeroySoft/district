"""Upgrade timer controls must not change enablement or touch services."""
import subprocess
import unittest
from pathlib import Path

from district.service import SystemdUserBackend


class UpgradeTimerCommandsTest(unittest.TestCase):
    def test_pause_and_resume_only_stop_and_start_timer(self):
        commands = []

        def run(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        backend = SystemdUserBackend(run, unit_dir=Path("/unused"))
        backend.pause_timer("factory-widgets.timer", timeout=10)
        backend.resume_timer("factory-widgets.timer", timeout=10)
        self.assertEqual(commands, [
            ["systemctl", "--user", "stop", "factory-widgets.timer"],
            ["systemctl", "--user", "start", "factory-widgets.timer"],
        ])
