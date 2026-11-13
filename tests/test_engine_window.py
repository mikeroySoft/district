"""Engine scheduling must pause without killing a running launchd pass."""
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import host, service


class EngineWindowTest(unittest.TestCase):
    def test_launchd_pause_unloads_idle_without_disable_and_never_kills_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {
                "HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"), "UV_TOOL_DIR": str(root / "tools"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
            }):
                state = {"loaded": True, "running": True}
                ops = []

                def run(argv, **kwargs):
                    operation = argv[1]
                    ops.append(operation)
                    self.assertNotIn(operation, ("disable", "enable", "kill"),
                                     f"launchd pause/resume must never {operation}")
                    if operation == "bootout":
                        self.assertTrue(not state["running"], "bootout would kill a running pass")
                        state["loaded"] = False
                    elif operation == "bootstrap":
                        state["loaded"] = True
                    elif operation == "print-disabled":
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    elif operation == "print":
                        if not state["loaded"]:
                            return subprocess.CompletedProcess(argv, 3, "", "not loaded")
                        body = "state = running\npid = 42\n" if state["running"] else "state = exited\nlast exit code = 0\n"
                        return subprocess.CompletedProcess(argv, 0, body, "")
                    return subprocess.CompletedProcess(argv, 0, "", "")

                backend = service.LaunchdBackend(run, home=root, uid=501, raw_path=os.defpath)
                backend.agent_dir.mkdir(parents=True)
                (backend.agent_dir / "factory-widgets.plist").write_bytes(plistlib.dumps({
                    "Label": "factory-widgets", "StartInterval": 600,
                    "ProgramArguments": [str(root / "tools/factory/bin/python"), "-P", "-m", "factory", "dispatch"],
                }))
                # A pass is in flight: pausing must not bootout (which would kill it) and
                # must not disable; the District drain flag the pass consults gates new work.
                self.assertEqual(backend.is_active("factory-widgets.service"), "activating")
                backend.pause_timer("factory-widgets.timer", timeout=1)
                self.assertNotIn("bootout", ops)
                self.assertEqual(backend.status("factory-widgets.service").main_pid, 42)
                # The pass finishes; now pausing unloads the agent without disabling it.
                state["running"] = False
                backend.pause_timer("factory-widgets.timer", timeout=1)
                self.assertIn("bootout", ops)
                self.assertEqual(backend.is_active("factory-widgets.timer"), "inactive")
                # Resume reloads the agent; the plist was never disabled.
                backend.resume_timer("factory-widgets.timer")
                self.assertIn("bootstrap", ops)
                self.assertEqual(backend.is_active("factory-widgets.timer"), "active")

    def test_discovery_includes_auxiliary_and_loaded_unregistered_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "factory-widgets-eval.service").touch()
            (root / "factory-widgets-eval.timer").touch()
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0,
                "factory-unregistered.service loaded active running\n", ""))
            backend = service.SystemdUserBackend(run, unit_dir=root)
            self.assertEqual(backend.factory_units(), {
                "factory-widgets-eval.service", "factory-widgets-eval.timer", "factory-unregistered.service",
            })

    def test_launchd_compound_timer_probe_stops_when_budget_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [0.0]
            calls = []

            def run(argv, **kwargs):
                calls.append(argv[1])
                self.assertEqual(kwargs["timeout"], 1)
                clock[0] = 1
                return subprocess.CompletedProcess(argv, 0, "state = running\npid = 42\n", "")

            backend = service.LaunchdBackend(run, home=Path(directory), uid=501, raw_path=os.defpath)
            with mock.patch.object(service.time, "monotonic", side_effect=lambda: clock[0]):
                with self.assertRaises(subprocess.TimeoutExpired):
                    backend.is_active("factory-widgets.timer", timeout=1)
            self.assertEqual(calls, ["print"])

    def test_scheduled_jobs_keep_completed_probe_when_budget_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [0.0]
            calls = []

            def run(argv, **kwargs):
                calls.append(argv)
                clock[0] = 1
                return subprocess.CompletedProcess(argv, 0, "state = running\npid = 42\n", "")

            backend = service.LaunchdBackend(run, home=Path(directory), uid=501, raw_path=os.defpath)
            with mock.patch.object(service.time, "monotonic", side_effect=lambda: clock[0]):
                states = backend.scheduled_jobs({"factory-a.timer", "factory-b.timer"}, timeout=1)
            self.assertEqual(set(states), {"factory-a.timer"})
            self.assertEqual(states["factory-a.timer"].service_active_state, "active")
            self.assertEqual(len(calls), 1)

    def test_remote_missing_launchd_job_does_not_probe_again_after_deadline(self):
        clock = [0.0]
        calls = []
        def run(argv, **kwargs):
            self.assertGreater(kwargs.get("timeout", 0), 0, "remote probe must remain bounded")
            calls.append(argv)
            if argv[0] == "python3":
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            clock[0] = 1
            return subprocess.CompletedProcess(argv, 3, "", "job not loaded")
        backend = service.LaunchdBackend(
            run, home=host.RemotePath(run, "/isolated/home"), uid=501, raw_path=os.defpath)
        with mock.patch.object(service.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(backend.is_active("factory-widgets.timer", timeout=1), "inactive")
        self.assertEqual(len(calls), 2)

    def test_systemd_zero_pause_budget_never_starts_unbounded_disable(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        backend = service.SystemdUserBackend(run, unit_dir=Path("/unused"))
        with self.assertRaises(subprocess.TimeoutExpired):
            backend.pause_timer("factory-widgets.timer", timeout=0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
