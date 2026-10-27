from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import doctor, host, rm, service, update_transaction as tx
from district.host import DistrictError
from district.service import LaunchdBackend, ServiceDefinition, SystemdUserBackend


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
RAW_PATH = (
    "/Users/mike/.local/bin:"
    "/Users/mike/.local/share/mise/installs/gh/latest/bin:"
    "/usr/bin"
)
FACTORY_ROOT = Path("/Users/mike/factories/widgets")


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.print_output = ""
        self.print_code = 0
        self.print_results: list[tuple[int, str, str]] = []

    def __call__(self, argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if argv[1:2] == ["print"]:
            if self.print_results:
                code, stdout, stderr = self.print_results.pop(0)
                return subprocess.CompletedProcess(argv, code, stdout, stderr)
            return subprocess.CompletedProcess(argv, self.print_code, self.print_output, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

class RunningRun:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded = True

    def __call__(self, argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if argv[1:2] == ["print"]:
            if self.loaded:
                return subprocess.CompletedProcess(argv, 0, "state = running\npid = 4242\n", "")
            return subprocess.CompletedProcess(argv, 3, "", "not found")
        if argv[1:2] == ["bootout"]:
            self.loaded = False
        return subprocess.CompletedProcess(argv, 0, "", "")


class FailedSystemctlRun:
    def __call__(self, argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
        proc = subprocess.CompletedProcess(argv, 1, "", "cannot connect to bus")
        if check:
            raise subprocess.CalledProcessError(proc.returncode, argv, proc.stdout, proc.stderr)
        return proc



class LaunchdBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(tmp.name) / "xdg"),
            "XDG_CACHE_HOME": str(Path(tmp.name) / "cache"),
        })
        env.start()
        self.addCleanup(env.stop)

    def definition(self, *, dashboard: bool = False, root: Path = FACTORY_ROOT) -> ServiceDefinition:
        if dashboard:
            return ServiceDefinition(
                label="factory-widgets-dashboard",
                description="factory dashboard for acme/widgets",
                argv=(
                    "/opt/factory/bin/python3", "-P", "-m", "factory", "dashboard",
                    "--host", "127.0.0.1", "--port", "8765", "--no-open",
                ),
                root=root,
                keep_alive=True,
            )
        return ServiceDefinition(
            label="factory-widgets",
            description="factory triage + dispatcher for acme/widgets (one pass)",
            argv=("/opt/factory/bin/python3", "-P", "-m", "factory", "dispatch"),
            root=root,
            environment={"UV_EXCLUDE_NEWER": "PT24H"},
            interval="10min",
            boot_delay="5min",
        )

    def backend(self, run: FakeRun, home: Path) -> LaunchdBackend:
        return LaunchdBackend(run, home=home, uid=501, raw_path=RAW_PATH)

    def test_generated_plists_match_goldens(self) -> None:
        run = FakeRun()
        backend = self.backend(run, Path("/Users/mike"))
        for definition, golden in (
            (self.definition(), "factory-widgets.plist"),
            (self.definition(dashboard=True), "factory-widgets-dashboard.plist"),
        ):
            with self.subTest(golden=golden):
                self.assertEqual(backend.render(definition), (GOLDEN / golden).read_bytes())

    def test_install_loads_and_remove_unloads_agent(self) -> None:
        run = FakeRun()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "factory"
            backend = self.backend(run, Path(directory))
            definition = self.definition(root=root)
            path = backend.install(definition)
            self.assertEqual(path, Path(directory) / "Library" / "LaunchAgents" / "factory-widgets.plist")
            self.assertTrue(path.is_file())
            self.assertTrue((root / ".factory" / "logs").is_dir())
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(path)], run.calls)
            backend.install(definition)
            self.assertEqual(run.calls.count(["launchctl", "bootstrap", "gui/501", str(path)]), 1)
            backend.remove("factory-widgets")
            self.assertIn(["launchctl", "bootout", "gui/501/factory-widgets"], run.calls)
            self.assertFalse(path.exists())

    def test_install_reloads_an_existing_plist_after_manual_bootout(self) -> None:
        run = FakeRun()
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            definition = self.definition(root=Path(directory) / "factory")
            path = backend.install(definition)
            run.print_code = 3
            backend.install(definition)
            self.assertEqual(run.calls.count(["launchctl", "bootstrap", "gui/501", str(path)]), 2)

    def test_remove_refuses_to_unload_a_running_pass_without_wait(self) -> None:
        run = RunningRun()
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            data = {"repo": {"acme/widgets": {"path": "/tmp/widgets"}}}
            with (
                mock.patch.object(rm.host, "load", return_value=data),
                mock.patch.object(rm.host, "select", return_value={"acme/widgets": data["repo"]["acme/widgets"]}),
                mock.patch.object(rm.host, "unit_name", return_value="factory-widgets"),
                mock.patch.object(rm.host, "service_backend", return_value=backend),
                mock.patch.object(rm.host, "save") as save,
                self.assertRaisesRegex(DistrictError, "is running a pass"),
            ):
                rm.main(["acme/widgets"])
        save.assert_not_called()
        self.assertNotIn(["launchctl", "bootout", "gui/501/factory-widgets"], run.calls)

    def test_disabling_timer_waits_for_running_job_before_bootout(self) -> None:
        run = FakeRun()
        run.print_results = [
            (0, "state = running\npid = 4242\n", ""),
            (0, "state = exited\nlast exit code = 0\n", ""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            backend.enable("factory-widgets.timer", False)
        print_calls = [index for index, call in enumerate(run.calls) if call[1:2] == ["print"]]
        bootout = run.calls.index(["launchctl", "bootout", "gui/501/factory-widgets"])
        self.assertEqual(len(print_calls), 2)
        self.assertLess(print_calls[-1], bootout)

    def test_disabling_a_hung_timer_reaches_the_update_deadline(self) -> None:
        run = RunningRun()
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            states = {
                name: {
                    "loaded": False,
                    "active_state": "inactive",
                    "unit_file_state": "not-found",
                    "argv": None,
                }
                for name in tx.UNITS
            }
            states[tx.TIMERS[0]].update(
                loaded=True,
                active_state="active",
                unit_file_state="enabled",
            )
            with (
                mock.patch.object(tx, "SERVICE_WAIT", 0),
                mock.patch.object(service.time, "sleep", side_effect=AssertionError("deadline was ignored")),
                self.assertRaisesRegex(tx.TransactionError, "still running after 0s"),
            ):
                tx.stop_services({}, states, backend)

    def test_update_boots_out_the_keepalive_dashboard(self) -> None:
        run = FakeRun()
        run.print_output = "state = running\npid = 4242\n"
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            states = {
                name: {
                    "loaded": False,
                    "active_state": "inactive",
                    "unit_file_state": "not-found",
                    "argv": None,
                }
                for name in tx.UNITS
            }
            states["district-dashboard.service"].update(
                loaded=True,
                active_state="active",
                unit_file_state="enabled",
            )
            tx.stop_services({}, states, backend)
        self.assertIn(["launchctl", "bootout", "gui/501/district-dashboard"], run.calls)
        self.assertFalse(any(call[1:2] == ["kill"] for call in run.calls))

    def test_disabling_a_keepalive_dashboard_does_not_wait_for_it_to_exit(self) -> None:
        run = FakeRun()
        run.print_output = "state = running\npid = 4242\n"
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            service.time,
            "sleep",
            side_effect=AssertionError("KeepAlive service cannot become idle before bootout"),
        ):
            backend = self.backend(run, Path(directory))
            backend.enable("factory-widgets-dashboard.service", False)
        self.assertIn(["launchctl", "bootout", "gui/501/factory-widgets-dashboard"], run.calls)

    def test_recovery_does_not_bootstrap_an_already_loaded_timer(self) -> None:
        run = FakeRun()
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            states = {
                name: {
                    "loaded": False,
                    "active_state": "inactive",
                    "unit_file_state": "not-found",
                    "argv": None,
                }
                for name in tx.UNITS
            }
            states[tx.TIMERS[0]].update(
                loaded=True,
                active_state="active",
                unit_file_state="enabled",
            )
            tx.restore_services({}, states, backend)
        self.assertFalse(any(call[1:2] == ["bootstrap"] for call in run.calls))

    def test_starting_a_hung_scheduled_job_has_a_deadline(self) -> None:
        run = RunningRun()
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            path = backend.agent_dir / "factory-widgets.plist"
            path.parent.mkdir(parents=True)
            path.write_bytes(backend.render(self.definition(root=Path(directory) / "factory")))
            with self.assertRaises(subprocess.TimeoutExpired):
                backend.start("factory-widgets.service", check=False, timeout=0)

    def test_doctor_reports_service_manager_failures_instead_of_raising(self) -> None:
        def failed(argv: list[str], *, check: bool = False, **_: object) -> subprocess.CompletedProcess:
            proc = subprocess.CompletedProcess(argv, 1, "", "cannot connect to bus")
            if check:
                raise DistrictError("cannot connect to bus")
            return proc

        for runner in (
            mock.Mock(side_effect=FileNotFoundError("systemctl not found")),
            failed,
        ):
            with self.subTest(runner=runner):
                with (
                    mock.patch.object(doctor.host, "load", return_value={}),
                    mock.patch.object(doctor.host, "run", runner),
                    mock.patch.object(doctor.shutil, "which", return_value=None),
                ):
                    rows = doctor.check()
                manager = next(row for row in rows if row["label"] == "systemd user manager")
                self.assertEqual(manager["status"], "FAIL")



    def test_start_loads_scheduled_job_and_returns_its_last_exit_code(self) -> None:
        run = FakeRun()
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
            path = backend.agent_dir / "factory-widgets.plist"
            path.parent.mkdir(parents=True)
            path.write_bytes(backend.render(self.definition(root=Path(directory) / "factory")))
            run.print_results = [
                (3, "", "not found"),
                (0, "state = running\npid = 4242\n", ""),
                (0, "state = exited\nlast exit code = 3\n", ""),
            ]
            proc = backend.start("factory-widgets.service", check=False)
            self.assertEqual(proc.returncode, 3)
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(path)], run.calls)
            self.assertIn(["launchctl", "bootout", "gui/501/factory-widgets"], run.calls)

    def test_systemd_status_propagates_show_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = SystemdUserBackend(FailedSystemctlRun(), unit_dir=Path(directory))
            with self.assertRaises(subprocess.CalledProcessError):
                backend.status("factory-widgets.service")

    def test_running_scheduled_job_is_activating_while_timer_stays_active(self) -> None:
        run = FakeRun()
        run.print_output = "state = running\npid = 4242\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "factory"
            backend = self.backend(run, Path(directory))
            backend.install(self.definition(root=root))
            self.assertEqual(backend.is_active("factory-widgets.service"), "activating")
            self.assertEqual(backend.is_active("factory-widgets.timer"), "active")
            status = backend.status("factory-widgets.service")
            self.assertEqual((status.loaded, status.active_state, status.main_pid), (True, "activating", 4242))

    def test_backend_selection_uses_host_platform(self) -> None:
        run = FakeRun()
        with tempfile.TemporaryDirectory() as directory:
            backend = host.service_backend(
                {"platform": "macos"}, run=run, home=Path(directory), uid=501, raw_path=RAW_PATH,
            )
        self.assertIsInstance(backend, LaunchdBackend)


if __name__ == "__main__":
    unittest.main()
