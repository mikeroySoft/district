"""Engine upgrades are gated by Factory services, not the host process table."""
import subprocess
import sys
import unittest
from unittest import mock

from district import apply, host, service
from tests import test_one_install as fixtures


class EngineBusyTest(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.OneInstallTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_sshd_with_unreadable_cwd_does_not_block_engine_swap(self):
        case = self.case
        proc_root = case.root / "proc"
        sshd = proc_root / "123"
        sshd.mkdir(parents=True)
        (sshd / "stat").write_text("123 (sshd) S 1 " + "0 " * 17 + "100 0\n")
        (sshd / "cmdline").write_bytes(b"sshd: mike@pts/0\0")
        # No cwd link: the daemon's working directory cannot be read.
        original = case.run_cmd

        def commands(argv, **kwargs):
            if argv[0] == "ps":
                return subprocess.CompletedProcess(argv, 0, "123 sshd: mike@pts/0\n", "")
            if argv[:2] == ["python3", "-c"] and "/proc/" in argv[2]:
                script = argv[2].replace("'/proc/'", repr(str(proc_root) + "/"))
                return subprocess.run([sys.executable, "-c", script, *argv[3:]],
                                      input=kwargs.get("input"), capture_output=True,
                                      text=True, timeout=kwargs["timeout"])
            return original(argv, **kwargs)

        with mock.patch.object(apply, "run", side_effect=commands):
            self.assertEqual(case.apply(upgrade=fixtures.TARGET), 0, case.output.getvalue())
        self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.TARGET,
                         case.output.getvalue())
        self.assertEqual(len(case.installs()), 1)
        self.assertEqual(case.active, {"factory-widgets.timer"})

    def test_five_active_dashboards_restart_only_after_engine_swap(self):
        case = self.case
        case.data["repo"] = {
            f"acme/repo-{port}": {"path": str(case.repo), "dashboard_port": port}
            for port in range(8765, 8770)
        }
        dashboards = {f"factory-repo-{port}-dashboard.service" for port in range(8765, 8770)}
        case.active = set(dashboards)
        case.backend.factory_units.return_value = dashboards
        case.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
        restarted = []

        def restart(unit, **kwargs):
            self.assertTrue(kwargs["restart"])
            self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.TARGET)
            self.assertEqual(len(case.installs()), 1)
            restarted.append(unit)
            return subprocess.CompletedProcess([], 0, "", "")

        case.backend.start.side_effect = restart
        self.assertEqual(case.reconcile(), "applied", case.output.getvalue())
        self.assertEqual(sorted(restarted), sorted(dashboards))
        self.assertEqual(case.active, dashboards)
        self.assertFalse(host.engine(host.load(), "local")["dashboards_pending"])


if __name__ == "__main__":
    unittest.main()
