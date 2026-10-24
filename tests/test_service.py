from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from district import host
from district.service import LaunchdBackend, ServiceDefinition


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

    def __call__(self, argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if argv[1:2] == ["print"]:
            return subprocess.CompletedProcess(argv, self.print_code, self.print_output, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class LaunchdBackendTest(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(run, Path(directory))
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
            backend.remove("factory-widgets")
            self.assertIn(["launchctl", "bootout", "gui/501/factory-widgets"], run.calls)
            self.assertFalse(path.exists())

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
