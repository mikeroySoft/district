"""Engine scheduling must pause without killing a running launchd pass."""
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import service


class EngineWindowTest(unittest.TestCase):
    def test_launchd_pause_preserves_process_and_respects_disabled_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {
                "HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"), "UV_TOOL_DIR": str(root / "tools"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
            }):
                disabled = set()
                killed = []

                def run(argv, **kwargs):
                    operation = argv[1]
                    if operation == "disable":
                        disabled.add("factory-widgets")
                    elif operation == "enable":
                        disabled.discard("factory-widgets")
                    elif operation in ("bootout", "kill"):
                        killed.append(operation)
                    output = ('"factory-widgets" => true' if disabled else "") if operation == "print-disabled" else "state = running\npid = 42\n"
                    return subprocess.CompletedProcess(argv, 0, output, "")

                backend = service.LaunchdBackend(run, home=root, uid=501, raw_path=os.defpath)
                backend.agent_dir.mkdir(parents=True)
                (backend.agent_dir / "factory-widgets.plist").write_bytes(plistlib.dumps({
                    "Label": "factory-widgets", "StartInterval": 600,
                    "ProgramArguments": [str(root / "tools/factory/bin/python"), "-P", "-m", "factory", "dispatch"],
                }))
                self.assertEqual(backend.is_active("factory-widgets.timer"), "active")
                backend.pause_timer("factory-widgets.timer", timeout=1)
                self.assertEqual(backend.is_active("factory-widgets.timer"), "inactive")
                self.assertEqual(backend.status("factory-widgets.service").main_pid, 42)
                backend.resume_timer("factory-widgets.timer")
                self.assertEqual(backend.is_active("factory-widgets.timer"), "active")
                self.assertEqual(killed, [])

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

    def test_systemd_zero_pause_budget_never_starts_unbounded_disable(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        backend = service.SystemdUserBackend(run, unit_dir=Path("/unused"))
        with self.assertRaises(subprocess.TimeoutExpired):
            backend.pause_timer("factory-widgets.timer", timeout=0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
