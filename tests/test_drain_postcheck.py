"""Issue #149: consumer-visible checks after a drained engine replacement."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import host, postcheck, service

PREVIOUS = "a" * 40
TARGET = "b" * 40
CURRENT = "c" * 40
FACTORY_REPO = "https://github.com/mikeroySoft/factory.git"


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def receipt(sha: str) -> str:
    return f'[tool]\nrequirements = [{{name="factory", git="{FACTORY_REPO}?rev={sha}"}}]\n'


class Backend:
    def __init__(self, *, loaded=True):
        state = "loaded" if loaded else "not-found"
        active = "active" if loaded else "inactive"
        self.dashboard = service.ServiceStatus(
            state,
            active,
            "enabled" if loaded else "disabled",
            42 if loaded else 0,
            ("/district/python", "-m", "district", "dashboard", "--port", "8760"),
        )
        self.starts = []
        self.statuses = []

    def status(self, name, **kwargs):
        if name != "district-dashboard.service":
            raise AssertionError(f"unexpected status probe: {name}")
        self.statuses.append((name, kwargs))
        return self.dashboard

    def start(self, name, **kwargs):
        self.starts.append((name, kwargs))
        return completed([name])


class FakeRunner:
    """Strict local runner; commands not belonging to the postcheck are test failures."""

    def __init__(self, root: Path):
        self.root = root
        self.calls = []
        self.http_status = {}
        self.git_roots = {root / "widgets"}
        self.fail = set()
        self.stage_roots = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        cwd = kwargs.get("cwd")

        if argv[:2] == ["gh", "api"] and "/compare/" in argv[-1]:
            return completed(argv, stdout=json.dumps({"status": "ahead"}))

        if argv and argv[0] == "git":
            root = Path(cwd) if cwd is not None else None
            return completed(argv, returncode=0 if root in self.git_roots else 128,
                             stdout=(str(root) + "\n") if root in self.git_roots else "",
                             stderr="fatal: not a git repository" if root not in self.git_roots else "")

        if argv[:3] == ["uv", "tool", "install"]:
            env = kwargs.get("env") or {}
            tool_dir = Path(env["UV_TOOL_DIR"])
            bin_dir = Path(env["UV_TOOL_BIN_DIR"])
            self.stage_roots.append(tool_dir.parent)
            if tool_dir == self.root / "tools" or bin_dir == self.root / "bin":
                raise AssertionError("throwaway verification mutated the live uv tool directories")
            (tool_dir / "factory").mkdir(parents=True, exist_ok=True)
            (tool_dir / "factory" / "uv-receipt.toml").write_text(receipt(PREVIOUS))
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "factory").touch()
            return completed(argv)

        executable = Path(argv[0]).name if argv else ""
        if executable == "factory":
            if argv[1:] == ["--help"]:
                if "help-timeout" in self.fail:
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
                return completed(argv, returncode=2 if "help" in self.fail else 0,
                                 stderr="broken help" if "help" in self.fail else "")
            if argv[1:] == ["--version"]:
                staged = str(argv[0]).startswith(tuple(str(root) for root in self.stage_roots))
                key = "previous-version" if staged else "version"
                return completed(argv, returncode=1 if key in self.fail else 0,
                                 stdout="factory 0.5.0\n" if key not in self.fail else "",
                                 stderr="broken version" if key in self.fail else "")
            if argv[1:] == ["watch", "--dry-run"]:
                return completed(argv, returncode=1 if "watch" in self.fail else 0,
                                 stderr="watch failed" if "watch" in self.fail else "")

        if len(argv) >= 3 and Path(argv[0]).name.startswith("python") and "-c" in argv:
            script = argv[argv.index("-c") + 1]
            if "import factory" in script:
                staged = str(argv[0]).startswith(tuple(str(root) for root in self.stage_roots))
                key = "previous-import" if staged else "import"
                if key in self.fail:
                    raise OSError(f"{key} unavailable")
                return completed(argv)
            if "urlopen" in script or "HTTPConnection" in script:
                url = next(arg for arg in argv[3:] if arg.startswith("http"))
                if "http-timeout" in self.fail:
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
                status = self.http_status.get(url, 200)
                return completed(argv, returncode=0 if status == 200 else 1,
                                 stdout=f"{status}\n", stderr="unhealthy" if status != 200 else "")

        raise AssertionError(f"unexpected command: {argv} {kwargs}")


class FakeRemote(host.RemoteRunner):
    def __init__(self):
        super().__init__("tank", "fake@tank")
        self.calls = []
        self._facts = {
            "home": "/home/factory",
            "config_home": "/home/factory/.config",
            "data_home": "/home/factory/.local/share",
            "tool_dir": "/remote/tools",
            "tool_bin": "/remote/bin",
            "path": "/usr/bin",
            "uid": 1000,
        }

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[:2] == ["python3", "-c"] and "read_text" in argv[2]:
            return completed(argv, stdout=receipt(CURRENT))
        if argv[:2] == ["gh", "api"]:
            return completed(argv, stdout=json.dumps({"status": "ahead"}))
        if argv and argv[0] == "git":
            return completed(argv, stdout="/srv/widgets\n")
        if Path(argv[0]).name == "factory" and argv[1:] in (
            ["--help"], ["--version"], ["watch", "--dry-run"],
        ):
            return completed(argv, stdout="factory 0.5.0\n" if argv[1:] == ["--version"] else "")
        if len(argv) >= 3 and Path(argv[0]).name.startswith("python") and "-c" in argv:
            script = argv[argv.index("-c") + 1]
            if "import factory" in script:
                return completed(argv)
            if "urlopen" in script or "HTTPConnection" in script:
                return completed(argv, stdout="200\n")
        raise AssertionError(f"unexpected remote command: {argv} {kwargs}")


class DrainPostcheckTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "UV_TOOL_DIR": str(self.root / "tools"),
            "UV_TOOL_BIN_DIR": str(self.root / "bin"),
        }))
        self.repo = self.root / "widgets"
        self.repo.mkdir()
        self.tool = self.root / "tools" / "factory"
        self.cli = self.root / "bin" / "factory"
        self.tool.mkdir(parents=True)
        self.cli.parent.mkdir(parents=True)
        (self.tool / "uv-receipt.toml").write_text(receipt(CURRENT))
        self.cli.touch()
        self.runner = FakeRunner(self.root)
        self.backend = Backend()
        self.data = {
            "defaults": {
                "factory_repo": FACTORY_REPO,
                "engine": {"sha": CURRENT, "target": TARGET},
            },
            "repo": {
                "acme/widgets": {
                    "path": str(self.repo),
                    "dashboard": {"port": 9001},
                },
            },
        }
        self.repos = self.data["repo"]

    def test_postcheck_runs_factory_watch_from_repo_checkout(self):
        with contextlib.chdir(self.root):
            failures = postcheck.check(
                self.data, "local", self.runner, self.backend, self.repos,
            )

        self.assertEqual(failures, [])
        commands = [(argv, kwargs) for argv, kwargs in self.runner.calls]
        self.assertTrue(any(argv[1:] == ["--help"] for argv, _ in commands))
        self.assertTrue(any(argv[1:] == ["--version"] for argv, _ in commands))
        self.assertTrue(any("import factory" in argv for argv, _ in commands))
        self.assertIn(
            ["gh", "api", f"repos/mikeroySoft/factory/compare/{TARGET}...{CURRENT}"],
            [argv for argv, _ in commands],
        )
        watch = [(argv, kwargs) for argv, kwargs in commands if argv[1:] == ["watch", "--dry-run"]]
        self.assertEqual(len(watch), 1)
        self.assertEqual(Path(watch[0][1]["cwd"]), self.repo)
        urls = [next(arg for arg in argv[3:] if arg.startswith("http")) for argv, _ in commands
                if len(argv) > 2 and argv[1] == "-c"
                and ("urlopen" in argv[2] or "HTTPConnection" in argv[2])]
        self.assertIn("http://127.0.0.1:9001/", urls)
        self.assertIn("http://127.0.0.1:8760/", urls)
        self.assertEqual(
            self.backend.starts,
            [("district-dashboard.service", {"restart": True, "timeout": postcheck.PROBE_TIMEOUT})],
        )

    def test_failures_are_reported_and_later_checks_continue(self):
        self.runner.fail.update({"help-timeout", "version", "import", "watch"})
        self.runner.http_status["http://127.0.0.1:9001/"] = 503

        failures = postcheck.check(
            self.data, "local", self.runner, self.backend, self.repos,
        )

        report = "\n".join(failures)
        for expected in ("factory --help", "factory --version", "import factory",
                         "dashboard", "HTTP 503", "factory watch --dry-run"):
            self.assertIn(expected, report)
        self.assertIn("district-dashboard.service", [name for name, _ in self.backend.starts])
        self.assertTrue(any(argv[1:] == ["watch", "--dry-run"] for argv, _ in self.runner.calls))

    def test_watch_uses_remote_registered_checkout(self):
        runner = FakeRemote()
        backend = Backend(loaded=False)
        data = {
            "defaults": {"factory_repo": FACTORY_REPO},
            "hosts": {"tank": {"engine": {"sha": CURRENT, "target": TARGET}}},
        }
        repos = {"acme/widgets": {"path": "/srv/widgets", "dashboard": {"port": 9001}}}

        failures = postcheck.check(data, "tank", runner, backend, repos)

        self.assertEqual(failures, [])
        git = [(argv, kwargs) for argv, kwargs in runner.calls if argv[0] == "git"]
        watch = [(argv, kwargs) for argv, kwargs in runner.calls
                 if argv[1:] == ["watch", "--dry-run"]]
        self.assertTrue(git, "the checkout must be verified by git on its owning host")
        self.assertEqual(Path(git[0][1]["cwd"]), Path("/srv/widgets"))
        self.assertEqual(Path(watch[0][1]["cwd"]), Path("/srv/widgets"))

    def test_no_registered_checkout_is_a_clear_failure(self):
        failures = postcheck.check(
            self.data, "local", self.runner, Backend(loaded=False), {},
        )

        self.assertIn("no factory repo checkout", "\n".join(failures))
        self.assertFalse(any(argv[1:] == ["watch", "--dry-run"]
                             for argv, _ in self.runner.calls))

    def test_previous_engine_uses_isolated_throwaway_install_and_always_cleans_it(self):
        live_receipt = (self.tool / "uv-receipt.toml").read_text()
        self.runner.fail.add("previous-import")

        failures = postcheck.check(
            self.data, "local", self.runner, Backend(loaded=False), self.repos,
            previous=PREVIOUS,
        )

        self.assertIn("previous engine import factory", "\n".join(failures))
        installs = [(argv, kwargs) for argv, kwargs in self.runner.calls
                    if argv[:3] == ["uv", "tool", "install"]]
        self.assertEqual(len(installs), 1)
        env = installs[0][1]["env"]
        self.assertNotEqual(Path(env["UV_TOOL_DIR"]), self.root / "tools")
        self.assertNotEqual(Path(env["UV_TOOL_BIN_DIR"]), self.root / "bin")
        self.assertTrue(self.runner.stage_roots)
        self.assertTrue(all(not root.exists() for root in self.runner.stage_roots))
        self.assertEqual((self.tool / "uv-receipt.toml").read_text(), live_receipt)

    def test_shared_deadline_bounds_commands_install_and_backend_probes(self):
        with mock.patch.object(postcheck.service, "remaining_timeout", return_value=7):
            failures = postcheck.check(
                self.data, "local", self.runner, self.backend, self.repos,
                previous=PREVIOUS, deadline=123,
            )

        self.assertEqual(failures, [])
        self.assertTrue(self.runner.calls)
        self.assertTrue(all(kwargs["timeout"] == 7 for _, kwargs in self.runner.calls))
        self.assertEqual(self.backend.statuses[0][1]["timeout"], 7)
        self.assertEqual(self.backend.starts[0][1]["timeout"], 7)

    def test_receipt_and_target_ancestry_failures_are_consumer_visible(self):
        (self.tool / "uv-receipt.toml").write_text(receipt(PREVIOUS))

        failures = postcheck.check(
            self.data, "local", self.runner, Backend(loaded=False), self.repos,
        )

        self.assertIn(f"receipt rev {PREVIOUS} != recorded engine {CURRENT}", "\n".join(failures))

        (self.tool / "uv-receipt.toml").write_text(receipt(CURRENT))
        original = self.runner.__call__

        def behind(argv, **kwargs):
            if list(argv)[:2] == ["gh", "api"]:
                self.runner.calls.append((list(argv), kwargs))
                return completed(argv, stdout=json.dumps({"status": "behind"}))
            return original(argv, **kwargs)

        failures = postcheck.check(
            self.data, "local", lambda argv, **kwargs: behind(argv, **kwargs),
            Backend(loaded=False), self.repos,
        )
        self.assertIn(f"recorded engine {CURRENT} is not target {TARGET} or newer", "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
