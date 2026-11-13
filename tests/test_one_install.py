"""Issue #136: one verified Factory uv tool per host, without host side effects."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock
from xml.parsers.expat import ExpatError

from district import apply, host, install_state, service
from tests.remote_transport import remote_command

OLD = "b" * 40
TARGET = "a" * 40
TIP = "c" * 40
REPO = "https://github.com/mikeroySoft/factory.git"


class OneInstallTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_BIN_HOME": str(self.root / "xdg-bin"),
            "UV_TOOL_DIR": str(self.root / "tools"),
            "UV_TOOL_BIN_DIR": str(self.root / "bin"),
        }))
        self.tool = self.root / "tools" / "factory"
        self.cli = self.root / "bin" / "factory"
        self.checkout = self.root / "stale-checkout"
        self.checkout.mkdir()
        self.repo = self.root / "widgets"
        self.repo.mkdir()
        self.other_repo = self.root / "other"
        self.other_repo.mkdir()
        self.data = {
            "defaults": {"factory_repo": REPO, "factory_ref": "main", "factory_track_branch": True,
                         "factory_source": str(self.checkout),
                         "engine": {"sha": OLD, "ref": "main"}},
            "repo": {"acme/widgets": {"path": str(self.repo)}},
        }
        self.calls = []
        self.events = []
        self.active = {"factory-widgets.timer"}
        self.service_states = {}
        self.busy = 0
        self.compare_status = "ahead"
        self.tip = TARGET
        self.bad_rev = False
        self.bad_version = False
        self.render_executable = str(self.tool / "bin" / "python")
        self.render_plist = False
        self.backend = mock.Mock()
        self.backend.is_active.side_effect = self.is_active
        self.backend.enable.side_effect = self.enable
        self.backend.start.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.backend.status.return_value = service.ServiceStatus("not-found", "inactive", "disabled", 0)
        self.backend.definitions_changed.return_value = True
        self.backend.configuration.return_value = {"platform": "linux", "unit_dir": str(host.unit_dir())}
        self.backend.scheduled_jobs.return_value = {}
        self.backend.factory_units.return_value = set()
        self.backend.pause_timer.side_effect = lambda name, **kwargs: self.enable(name, False, **kwargs)
        self.backend.resume_timer.side_effect = self.enable
        self.enterContext(mock.patch.object(apply, "run", side_effect=self.run_cmd))
        self.enterContext(mock.patch.object(host, "run", side_effect=self.run_cmd))
        self.enterContext(mock.patch.object(host, "service_backend", return_value=self.backend))
        self.enterContext(mock.patch.object(install_state, "timer_states", return_value={}))
        self.enterContext(mock.patch.object(apply.syslog, "syslog"))
        self.sleep = self.enterContext(mock.patch.object(apply.time, "sleep"))
        self.install_receipt(OLD)
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def install_receipt(self, sha):
        self.tool.mkdir(parents=True, exist_ok=True)
        (self.tool / "bin").mkdir(exist_ok=True)
        for name in ("factory", "python"):
            executable = self.tool / "bin" / name
            if not executable.exists() and not executable.is_symlink():
                executable.touch(mode=0o755)
        self.cli.parent.mkdir(parents=True, exist_ok=True)
        for name in ("gh", "git", "uv", "python3"):
            executable = self.cli.parent / name
            if not executable.exists():
                executable.touch(mode=0o755)
        self.cli.unlink(missing_ok=True)
        self.cli.symlink_to(self.tool / "bin" / "factory")
        (self.tool / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{name = "factory", git = "{REPO}?rev={sha}"}}]\n'
            f'entrypoints = [{{name = "factory", install-path = "{self.cli}", from = "factory"}}]\n')
        metadata = self.tool / "lib/python3.12/site-packages/factory-0.4.7.dist-info/METADATA"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text("Metadata-Version: 2.4\nName: factory\nVersion: 0.4.7\n")

    def is_active(self, name, **kwargs):
        self.events.append(("state", name))
        if name in self.service_states:
            return self.service_states[name]
        if name.endswith(".service") and "dashboard" not in name and self.busy:
            self.busy -= 1
            return "activating"
        return "active" if name in self.active else "inactive"

    def enable(self, name, enabled=True, **kwargs):
        self.events.append(("enable" if enabled else "pause", name))
        self.active.add(name) if enabled else self.active.discard(name)
        return subprocess.CompletedProcess([], 0, "", "")

    def rendered(self, cwd=None):
        environment = {"PATH": str(self.cli.parent), "HOME": str(Path.home())}
        owners = ([slug for slug, table in self.data["repo"].items()
                   if Path(table["path"]) == Path(cwd)] if cwd is not None else ["acme/widgets"])
        self.assertEqual(len(owners), 1, f"render cwd must identify one repo: {cwd}")
        unit = host.unit_name(owners[0])
        if self.render_plist:
            name = f"{unit}.plist"
            body = plistlib.dumps({
                "Label": unit,
                "ProgramArguments": [self.render_executable, "-P", "-m", "factory", "dispatch"],
                "EnvironmentVariables": environment,
            }).decode()
        else:
            name = f"{unit}.service"
            body = (
                "[Service]\n"
                f'Environment="PATH={environment["PATH"]}" "HOME={environment["HOME"]}"\n'
                f"ExecStart={self.render_executable} -P -m factory dispatch\n"
            )
        return name, body

    def run_isolated(self, argv, **kwargs):
        environment = {}
        index = 2
        while index < len(argv) and "=" in argv[index]:
            key, value = argv[index].split("=", 1)
            environment[key] = value
            index += 1
        command = argv[index:]
        if set(environment) != {"PATH", "HOME"}:
            self.fail(f"invalid isolated environment: {argv}")
        if command[:2] == ["/bin/sh", "-c"] and command[-1] == ".factory.toml":
            metadata = Path(kwargs.get("cwd") or ".") / command[-1]
            if not metadata.exists():
                return subprocess.CompletedProcess(argv, 44, "", "")
            try:
                return subprocess.CompletedProcess(argv, 0, metadata.read_text(), "")
            except OSError as exc:
                return subprocess.CompletedProcess(argv, 1, "", str(exc))
        if command[:2] == ["/bin/sh", "-c"] and command[-2] == "district-preflight":
            resolved = shutil.which(command[-1], path=environment["PATH"])
            return subprocess.CompletedProcess(
                argv, 0 if resolved else 127, f"{resolved}\n" if resolved else "", "")
        versions = {
            "factory": "factory 0.4.7",
            "gh": "gh version 2.65.0",
            "git": "git version 2.47.0",
            "uv": "uv 0.5.0",
            "python3": "Python 3.12.0",
        }
        if len(command) == 2 and command[1] in ("--version", "-version"):
            resolved = shutil.which(command[0], path=environment["PATH"])
            version = versions.get(Path(command[0]).name)
            if resolved and version:
                return subprocess.CompletedProcess(argv, 0, version + "\n", "")
            return subprocess.CompletedProcess(argv, 127, "", f"{command[0]}: not found")
        self.fail(f"unexpected isolated command: {argv}")

    def run_cmd(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        out, code = "", 0
        if argv[:2] == ["/usr/bin/env", "-i"]:
            return self.run_isolated(argv, **kwargs)
        if argv[:4] == ["git", "remote", "get-url", "origin"]:
            out = REPO + "\n"
        elif argv[:2] == ["git", "ls-remote"]:
            out = f"{TARGET}\trefs/tags/v0.4.7\n{self.tip}\trefs/heads/main\n"
        elif argv[:2] == ["gh", "api"]:
            endpoint = next((arg for arg in argv[2:] if arg.startswith("repos/")), "")
            if "/releases?" in endpoint:
                out = json.dumps([[{
                    "tag_name": "v0.4.7", "draft": False, "prerelease": False,
                    "published_at": "2026-01-01T00:00:00Z",
                }]])
            elif "check-runs" in endpoint:
                conclusion = "failure" if TIP in endpoint else "success"
                out = json.dumps({"total_count": 1, "check_runs": [
                    {"status": "completed", "conclusion": conclusion}]})
            elif "/compare/" in endpoint:
                out = json.dumps({"status": self.compare_status, "ahead_by": 1, "behind_by": 0})
            elif "/commits/" in endpoint:
                out = json.dumps({"sha": endpoint.rsplit("/", 1)[1]})
            elif "/commits" in endpoint:
                out = json.dumps([{"sha": TIP}, {"sha": TARGET}, {"sha": OLD}])
            else:
                self.fail(f"unexpected GitHub API: {argv}")
        elif argv[:3] == ["uv", "tool", "install"]:
            self.events.append(("install", argv[argv.index("--from") + 1]))
            source = argv[argv.index("--from") + 1]
            sha = OLD if f"@{OLD}" in source else TARGET
            self.install_receipt(OLD if self.bad_rev and sha == TARGET else sha)
        elif argv[0] in ("factory", str(self.cli)):
            if argv[1:] == ["--version"]:
                code = 1 if self.bad_version and len(self.installs()) == 1 else 0
                out = "factory 0.4.7\n"
            elif argv[1:] == ["install", "--print"]:
                name, body = self.rendered(kwargs.get("cwd"))
                out = f"# {name}\n{body}\n"
            elif argv[1:] == ["install"]:
                name, body = self.rendered(kwargs.get("cwd"))
                directory = (Path.home() / "Library/LaunchAgents") if name.endswith(".plist") else host.unit_dir()
                directory.mkdir(parents=True, exist_ok=True)
                (directory / name).write_text(body)
                self.enable(f"{Path(name).stem}.timer")
            elif argv[1:] == ["doctor", "--json"]:
                out = json.dumps({"rows": []})
            elif argv[1:] == ["dashboard", "--json"]:
                out = json.dumps({"version": "0.4.7", "dispatcher": {"consecutive_failures": 0}})
            elif argv[1:] != ["init", "--labels-only"]:
                self.fail(f"unexpected Factory command: {argv}")
        elif argv == ["systemctl", "--user", "daemon-reload"]:
            pass
        elif argv[:2] == ["git", "rev-parse"]:
            out = OLD + "\n"  # A stale checkout must never be used by --upgrade.
        elif argv[:2] not in (["git", "archive"], ["git", "status"]) and argv[0] != "tar":
            self.fail(f"unexpected command (no real commands are allowed): {argv}")
        return subprocess.CompletedProcess(argv, code, out, "")

    def installs(self):
        return [argv for argv, _ in self.calls if argv[:3] == ["uv", "tool", "install"]]

    def reconcile(self, **kwargs):
        return apply.reconcile_engine(self.data, backend=self.backend, **kwargs)

    def apply(self, upgrade=None):
        host.save(self.data)
        args = argparse.Namespace(slug=None, reset=None, upgrade=upgrade, allow_downgrade=False)
        return apply._run(args, install_state.ApplyRun("", "", OLD))

    def test_current_engine_dashboard_restart_failure_still_checks_repositories(self):
        self.install_receipt(TARGET)
        self.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
        self.backend.start.return_value = subprocess.CompletedProcess([], 1, "", "restart failed")
        for recorded in (OLD, None):
            with self.subTest(recorded=recorded):
                self.data["defaults"]["engine"] = {"ref": "main"}
                if recorded:
                    self.data["defaults"]["engine"]["sha"] = recorded
                self.calls.clear()
                self.output.truncate(0)
                self.output.seek(0)
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.apply(), 1)
                output = self.output.getvalue()
                self.assertNotIn("Traceback", output + errors.getvalue())
                self.assertIn("engine failed preparing", output)
                self.assertIn("factory-widgets-dashboard.service: restart exited 1", output)
                self.assertIn("acme/widgets  0.4.7", output)
                self.assertIn("engine step failed; install skipped", output)
                self.assertTrue(any(argv[1:] == ["doctor", "--json"] for argv, _ in self.calls))
                self.assertEqual(self.installs(), [])
                self.assertTrue(host.engine(host.load(), "local")["dashboards_pending"])

    def test_apply_recognizes_and_upgrades_tool_under_symlinked_uv_or_xdg_directory(self):
        self.backend.definitions_changed.return_value = False
        for setting in ("UV_TOOL_DIR", "XDG_DATA_HOME"):
            real = self.root / f"real-{setting}"
            real.mkdir()
            alias = self.root / f"link-{setting}"
            alias.symlink_to(real, target_is_directory=True)
            env = {setting: str(alias)}
            if setting == "XDG_DATA_HOME":
                env["UV_TOOL_DIR"] = ""
            with mock.patch.dict(os.environ, env):
                self.tool, self.cli = host.factory_tool_paths()
                self.render_executable = str(self.tool / "bin" / "python")
                for upgrade in (False, True):
                    with self.subTest(setting=setting, upgrade=upgrade):
                        installed = OLD if upgrade else TARGET
                        self.install_receipt(installed)
                        self.data["defaults"]["engine"] = {"sha": installed, "ref": "main"}
                        self.calls.clear()
                        self.events.clear()
                        self.output.truncate(0)
                        self.output.seek(0)
                        self.assertEqual(self.apply(upgrade="main" if upgrade else None), 0, self.output.getvalue())
                        output = self.output.getvalue()
                        self.assertIn("acme/widgets  0.4.7", output)
                        self.assertNotIn("FAIL", output)
                        self.assertTrue(any(argv[1:] == ["doctor", "--json"] for argv, _ in self.calls))
                        self.assertEqual(len(self.installs()), int(upgrade))
                        self.assertEqual(self.active, {"factory-widgets.timer"})
                        self.assertEqual([event for event in self.events if event[0] in ("pause", "enable")],
                                         [("pause", "factory-widgets.timer"), ("enable", "factory-widgets.timer")]
                                         if upgrade else [])
                        engine = host.engine(host.load(), "local")
                        self.assertEqual(engine["sha"], TARGET)
                        self.assertFalse(engine.get("paused_timers"))

    def alias_tool_runner(self, remote):
        alias = self.root / "tool-link"
        alias.symlink_to(self.tool.parent, target_is_directory=True)
        os.environ["UV_TOOL_DIR"] = str(alias)
        self.tool = alias / "factory"
        self.install_receipt(OLD)
        if not remote:
            return None
        self.data["hosts"] = {"remote": {"ssh": "unused", "platform": "linux",
                                       "engine": {"sha": OLD, "ref": "main"}}}
        self.data["repo"]["acme/widgets"]["host"] = "remote"
        runner, _, _ = self.timeout_remote("none")
        return runner

    def test_alias_renderer_accepts_tool_bin_but_rejects_outside_argv(self):
        outside_python = self.root / "system-python"
        outside_python.touch(mode=0o444)
        os.utime(outside_python, (1, 1))
        for remote in (False, True):
            with self.subTest(remote=remote), contextlib.ExitStack() as stack:
                if remote:
                    (self.root / "tool-link").unlink()
                    self.tool = self.root / "tools/factory"
                runner = self.alias_tool_runner(remote)
                if runner:
                    stack.enter_context(mock.patch.object(host, "remote_runner", return_value=runner))
                self.install_receipt(TARGET)
                self.backend.definitions_changed.return_value = False
                real_bin = self.tool.resolve() / "bin"
                # Resolving python itself would lose venv identity.
                (real_bin / "python").unlink()
                (real_bin / "python").symlink_to(outside_python)
                resolve = Path.resolve

                def no_controller_resolution(path, *args, **kwargs):
                    if remote and (path == self.cli or path.is_relative_to(self.tool)
                                   or path.is_relative_to(real_bin)):
                        self.fail(f"remote tool path resolved on controller: {path}")
                    return resolve(path, *args, **kwargs)

                stack.enter_context(mock.patch.object(Path, "resolve", new=no_controller_resolution))
                for executable, accepted in ((real_bin / "python", True),
                                             (real_bin / "factory", True),
                                             (self.root / "legacy/bin/python", False),
                                             (self.root / "legacy/bin/factory", False)):
                    for plist in (False, True):
                        with self.subTest(executable=executable, plist=plist):
                            self.render_executable = str(executable)
                            self.render_plist = plist
                            self.output.seek(0)
                            self.output.truncate(0)
                            self.assertEqual(self.apply(), 0 if accepted else 1, self.output.getvalue())
                            self.assertIn("acme/widgets  0.4.7", self.output.getvalue())
                            if not accepted:
                                self.assertIn("Factory argv must use", self.output.getvalue())
                # Owners can touch read-only files; pin mtime to catch write-through locally too.
                self.assertEqual(outside_python.stat().st_mtime, 1)

    def tank_rendered(self, cwd=None):
        body = (Path(__file__).parent / "fixtures/factory-district.service").read_text()
        return "factory-widgets.service", body.format(
            repo=self.repo, cli_bin=self.cli.parent, home=Path.home(), tool=self.tool)

    def test_tank_oneshot_render_checks_allow_install(self):
        self.rendered = self.tank_rendered
        name, body = self.rendered()
        output = f"# {name}\n{body}"
        self.assertEqual(apply._render_errors(output, self.tool, self.cli, None), [])
        errors = {}
        self.assertEqual(apply.plan_apply(
            self.data["repo"], self.backend, run_cmd=self.run_cmd, errors=errors),
            {"acme/widgets": True})
        self.assertEqual(errors, {})

    def test_tank_oneshot_apply_writes_rendered_unit_without_factory_install(self):
        self.rendered = self.tank_rendered
        self.assertEqual(self.apply(), 0, self.output.getvalue())
        self.assertNotIn([str(self.cli), "install"], [argv for argv, _ in self.calls])
        self.assertEqual(
            (host.unit_dir() / "factory-widgets.service").read_text(), self.tank_rendered()[1])
        self.assertNotIn("missing Factory argv", self.output.getvalue())

    def test_tank_oneshot_rollback_writes_rendered_unit_without_factory_install(self):
        self.rendered = self.tank_rendered
        self.install_receipt(TARGET)
        self.data["defaults"]["engine"] = {"sha": TARGET, "ref": "main"}
        self.compare_status = "behind"
        host.save(self.data)
        args = argparse.Namespace(slug=None, reset=None, upgrade=OLD, allow_downgrade=True)
        self.assertEqual(apply._run(args, install_state.ApplyRun("", "", TARGET)), 0,
                         self.output.getvalue())
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], OLD)
        self.assertNotIn([str(self.cli), "install"], [argv for argv, _ in self.calls])
        self.assertEqual(
            (host.unit_dir() / "factory-widgets.service").read_text(), self.tank_rendered()[1])
        self.assertNotIn("missing Factory argv", self.output.getvalue())

    def test_tank_oneshot_rejects_each_outside_execstart(self):
        name, body = self.tank_rendered()
        for verb in ("triage", "dispatch"):
            for executable in ("/outside/bin/python", "/usr/bin/env factory"):
                with self.subTest(verb=verb, executable=executable):
                    offending = f"{executable} -P -m factory {verb}"
                    invalid = body.replace(f"{self.tool}/bin/python -P -m factory {verb}", offending)
                    errors = apply._render_errors(f"# {name}\n{invalid}", self.tool, self.cli, None)
                    self.assertEqual(len(errors), 1)
                    self.assertIn(name, errors[0])
                    self.assertIn(offending, errors[0])
        invalid = body.replace(f"{self.tool}/bin/python", "/outside/bin/python")
        errors = apply._render_errors(f"# {name}\n{invalid}", self.tool, self.cli, None)
        self.assertEqual(len(errors), 2)
        self.assertIn("/outside/bin/python -P -m factory triage", errors[0])
        self.assertIn("/outside/bin/python -P -m factory dispatch", errors[1])

    def test_tank_oneshot_accepts_systemd_exec_prefixes(self):
        name, body = self.tank_rendered()
        for prefix in ("", "-", "@", ":", "+", "!", "!!", "-@:", "-+"):
            for command in (f"{self.tool}/bin/python -P -m factory",
                            f"{self.tool}/bin/python -m factory", str(self.cli)):
                with self.subTest(prefix=prefix, command=command):
                    prefixed = body.replace("ExecStart=-", "ExecStart=")
                    prefixed = prefixed.replace(
                        f"ExecStart={self.tool}/bin/python -P -m factory", f"ExecStart={prefix}{command}")
                    self.assertEqual(apply._render_errors(
                        f"# {name}\n{prefixed}", self.tool, self.cli, None), [])

    def test_tank_oneshot_without_execstart_reports_missing_argv(self):
        name, body = self.tank_rendered()
        body = "\n".join(line for line in body.splitlines() if not line.startswith("ExecStart="))
        self.assertEqual(apply._render_errors(f"# {name}\n{body}", self.tool, self.cli, None),
                         [f"{name}: missing Factory argv"])


    def test_one_tool_and_rendered_units_reject_versioned_venvs(self):
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.installs(), [["uv", "tool", "install", "--force", "--from",
                                          f"git+{REPO}@{TARGET}", "factory"]])
        self.assertEqual(self.cli.resolve(), self.tool / "bin" / "factory")
        installed = host.unit_dir() / "factory-widgets.service"
        self.assertIn(str(self.tool / "bin" / "python"), installed.read_text())
        install_calls = [argv for argv, _ in self.calls if argv[1:2] == ["install"]]
        self.assertTrue(all(argv[0] == str(self.cli) for argv in install_calls), install_calls)
        for plist in (False, True):
            with self.subTest(plist=plist):
                self.render_plist = plist
                self.render_executable = str(self.root / "data/district/factory-old/.venv/bin/python")
                self.assertEqual(self.apply(), 1, "a versioned engine must fail the repo row")
                self.assertIn("factory-widgets" + (".plist" if plist else ".service"), self.output.getvalue())

    def test_apply_fails_row_for_unit_or_plist_outside_uv_tool(self):
        for plist in (False, True):
            with self.subTest(plist=plist):
                self.render_plist = plist
                self.render_executable = str(self.root / "data/district/factory-old/.venv/bin/python")
                self.assertEqual(self.apply(), 1, "a versioned engine must fail the repo row")
                self.assertIn("factory-widgets" + (".plist" if plist else ".service"), self.output.getvalue())


    def test_failed_verification_rolls_back_and_restores_exact_timers(self):
        for failure in ("receipt", "version"):
            with self.subTest(failure=failure):
                self.calls.clear()
                self.events.clear()
                self.install_receipt(OLD)
                self.data["defaults"]["engine"] = {"sha": OLD, "ref": "main"}
                self.bad_rev = failure == "receipt"
                self.bad_version = failure == "version"
                self.assertEqual(self.reconcile(), "failed")
                sources = [argv[argv.index("--from") + 1] for argv in self.installs()]
                self.assertEqual(sources, [f"git+{REPO}@{TARGET}", f"git+{REPO}@{OLD}"])
                self.assertEqual(self.data["defaults"]["engine"]["sha"], OLD)
                self.assertEqual(self.active, {"factory-widgets.timer"})
                self.assertIn(OLD, self.output.getvalue())
                self.assertIn(TARGET, self.output.getvalue())

    def test_cleanup_scans_units_plists_config_and_receipt(self):
        base = self.root / "data/district"
        folders = {name: base / f"factory-{name}" for name in ("unused", "unit", "plist", "config", "receipt")}
        for path in folders.values():
            path.mkdir(parents=True)
        host.unit_dir().mkdir(parents=True)
        (host.unit_dir() / "factory-other.service").write_text(f"ExecStart={folders['unit']}/bin/python\n")
        agents = Path.home() / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.example.factory-other.plist").write_bytes(plistlib.dumps({
            "ProgramArguments": [str(folders["plist"] / "bin/python")]}))
        self.data["defaults"]["archived_path"] = str(folders["config"])
        original_install = self.install_receipt
        def receipt_with_reference(sha):
            original_install(sha)
            with (self.tool / "uv-receipt.toml").open("a") as stream:
                stream.write(f'legacy = "{folders["receipt"]}"\n')
        self.install_receipt = receipt_with_reference
        self.assertEqual(self.apply(), 0)
        self.assertFalse(folders["unused"].exists(), "unreferenced per-version install was not removed")
        for name in ("unit", "plist", "config", "receipt"):
            self.assertTrue(folders[name].exists(), name)
            self.assertIn(str(folders[name]), self.output.getvalue())

    def test_newest_green_skips_red_tip_records_and_syncs_target(self):
        self.tip = TIP
        self.data["hosts"] = {"remote": {"ssh": "test@example", "platform": "linux"}}
        self.data["repo"]["acme/widgets"]["host"] = "remote"
        self.assertEqual(self.reconcile(host_name="remote"), "applied")
        engine = self.data["hosts"]["remote"]["engine"]
        self.assertEqual(engine["sha"], TARGET)
        self.assertEqual(engine["target"], TARGET)
        self.assertEqual(engine["target_ref"], "main")
        self.assertIn("resolved_at", engine)
        self.assertEqual(host.remote_config(self.data, "remote")["defaults"]["engine"]["target"], TARGET)
        self.assertEqual(host.load()["hosts"]["remote"]["engine"]["target"], TARGET)

    def test_second_reconcile_is_noop_with_verified_target_installed(self):
        self.assertEqual(self.reconcile(), "applied")
        engine = host.load()["defaults"]["engine"]
        self.assertEqual(engine.get("target"), TARGET)
        self.calls.clear()
        self.backend.reset_mock()
        self.assertEqual(self.reconcile(), "unchanged")
        self.assertEqual(self.installs(), [])
        self.backend.enable.assert_not_called()
        self.backend.is_active.assert_not_called()
        self.backend.start.assert_not_called()

    def test_upgrade_uses_remote_ref_not_stale_checkout_head(self):
        self.assertEqual(self.apply(upgrade="main"), 0)
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], TARGET)
        self.assertFalse(any(kwargs.get("cwd") == self.checkout for _, kwargs in self.calls))
        self.assertFalse(any(argv[:2] in (["git", "archive"], ["git", "rev-parse"]) for argv, _ in self.calls))


    def test_downgrade_refusal_names_both_shas_and_override_installs(self):
        self.compare_status = "behind"
        with self.assertRaises(host.DistrictError) as refused:
            self.reconcile()
        self.assertIn(OLD, str(refused.exception))
        self.assertIn(TARGET, str(refused.exception))
        self.assertEqual(self.installs(), [])
        self.backend.pause_timer.assert_not_called()
        self.assertFalse(host.engine(host.load(), "local").get("install_pending"))
        self.data["defaults"].pop("factory_ref")
        self.assertEqual(self.reconcile(), "unchanged")
        self.assertEqual(self.installs(), [])
        self.data["defaults"]["factory_ref"] = "main"
        self.assertEqual(self.reconcile(allow_downgrade=True), "applied")
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], TARGET)

    def test_crash_after_install_recovers_paused_timer_and_state_without_uv(self):
        self.data["defaults"]["engine"]["paused_timers"] = ["factory-widgets.timer"]
        self.active.clear()
        self.install_receipt(TARGET)
        self.assertEqual(self.reconcile(), "unchanged")
        self.assertEqual(self.active, {"factory-widgets.timer"})
        self.assertEqual(self.installs(), [])
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], TARGET)
        self.assertEqual(host.load()["defaults"]["engine"]["paused_timers"], [])

    def test_recorded_sha_without_matching_receipt_is_reinstalled(self):
        self.data["defaults"]["engine"]["sha"] = TARGET
        self.assertEqual(self.reconcile(), "applied")
        self.assertEqual(len(self.installs()), 1)
        self.assertIn(TARGET, (self.tool / "uv-receipt.toml").read_text())

    def test_unexpected_install_exception_still_restores_timers(self):
        def fail_install(argv, **kwargs):
            if argv[:3] == ["uv", "tool", "install"]:
                raise RuntimeError("installer crashed")
            return self.run_cmd(argv, **kwargs)
        with mock.patch.object(apply, "run", side_effect=fail_install):
            with self.assertRaisesRegex(RuntimeError, "installer crashed"):
                self.reconcile()
        self.assertEqual(self.active, {"factory-widgets.timer"})
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], OLD)

    def test_explicit_commit_upgrade_uses_remote_commit_and_records_target(self):
        self.assertEqual(apply.upgrade(self.data, TARGET, backend=self.backend), "applied")
        self.assertEqual(host.load()["defaults"]["engine"]["target"], TARGET)
        self.assertFalse(any(argv[:2] == ["git", "rev-parse"] for argv, _ in self.calls))

    def test_red_pinned_tag_is_not_replaced_by_an_older_green_commit(self):
        def red_tag(argv, **kwargs):
            if argv[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(argv, 0, f"{TIP}\trefs/tags/v0.4.7\n", "")
            return self.run_cmd(argv, **kwargs)
        with mock.patch.object(apply, "run", side_effect=red_tag):
            self.assertEqual(apply.upgrade(self.data, "v0.4.7", backend=self.backend), "failed")
        self.assertEqual(self.installs(), [])
        self.backend.pause_timer.assert_not_called()
        self.assertFalse(any("/commits?" in " ".join(argv) for argv, _ in self.calls))

    def test_unknown_explicit_upgrade_touches_no_timer(self):
        def missing_ref(argv, **kwargs):
            if argv[:2] == ["git", "ls-remote"]:
                return subprocess.CompletedProcess(argv, 2, "", "missing remote ref")
            return self.run_cmd(argv, **kwargs)
        with mock.patch.object(apply, "run", side_effect=missing_ref):
            self.assertEqual(apply.upgrade(self.data, "missing", backend=self.backend), "failed")
        self.assertEqual(self.installs(), [])
        self.backend.pause_timer.assert_not_called()

    def test_install_failure_and_timeout_roll_back_before_restoring(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.calls.clear()
                def fail_target(argv, **kwargs):
                    if argv[:3] == ["uv", "tool", "install"] and f"git+{REPO}@{TARGET}" in argv:
                        self.calls.append((list(argv), kwargs))
                        if timeout:
                            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                        return subprocess.CompletedProcess(argv, 1, "", "install failed")
                    return self.run_cmd(argv, **kwargs)
                with mock.patch.object(apply, "run", side_effect=fail_target):
                    self.assertEqual(self.reconcile(), "failed")
                self.assertEqual(len(self.installs()), 2)
                self.assertEqual(self.active, {"factory-widgets.timer"})
                self.assertEqual(host.load()["defaults"]["engine"]["sha"], OLD)


    def test_restore_error_keeps_recovery_state_and_restores_other_timers(self):
        timers = ["factory-widgets.timer", "factory-other.timer"]
        self.data["defaults"]["engine"]["paused_timers"] = timers
        self.active.clear()
        def resume(timer, **kwargs):
            if timer == timers[0]:
                raise OSError("unavailable")
            return self.enable(timer)
        self.backend.resume_timer.side_effect = resume
        with self.assertRaises(host.DistrictError):
            self.reconcile()
        self.assertEqual(self.backend.resume_timer.call_count, 2)
        self.assertEqual(host.load()["defaults"]["engine"]["paused_timers"], [timers[0]])
        self.assertEqual(self.active, {timers[1]})
        self.assertEqual(self.installs(), [])

    def test_timer_only_render_cannot_converge(self):
        self.rendered = lambda cwd=None: ("factory-widgets.timer", "[Timer]\nOnUnitActiveSec=10min\n")
        self.assertEqual(self.apply(), 1)

    def test_crash_with_unverified_target_rolls_back_to_recorded_previous_sha(self):
        self.data["defaults"]["engine"]["paused_timers"] = ["factory-widgets.timer"]
        self.active.clear()
        self.install_receipt(TARGET)
        def broken_target(argv, **kwargs):
            if argv == [str(self.cli), "--version"] and TARGET in (self.tool / "uv-receipt.toml").read_text():
                return subprocess.CompletedProcess(argv, 1, "", "broken target")
            return self.run_cmd(argv, **kwargs)
        with mock.patch.object(apply, "run", side_effect=broken_target):
            self.assertEqual(self.reconcile(), "failed")
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], OLD)
        self.assertEqual(self.active, {"factory-widgets.timer"})
        self.assertEqual([argv[argv.index("--from") + 1] for argv in self.installs()],
                         [f"git+{REPO}@{TARGET}", f"git+{REPO}@{OLD}"])

    def test_second_apply_has_no_uv_or_timer_mutation(self):
        self.assertEqual(self.apply(), 0)
        self.data = host.load()
        self.calls.clear()
        self.events.clear()
        self.backend.definitions_changed.return_value = False
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.installs(), [])
        self.assertEqual([event for event in self.events if event[0] in ("pause", "enable")], [])

    def test_crash_after_timer_restore_finishes_dashboard_restart_without_uv(self):
        self.data["defaults"]["engine"].update(sha=TARGET, dashboards_pending=True)
        self.install_receipt(TARGET)
        self.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
        self.assertEqual(self.reconcile(), "unchanged")
        self.assertEqual(self.installs(), [])
        self.assertFalse(host.load()["defaults"]["engine"]["dashboards_pending"])


    def test_unchanged_fleet_does_not_restart_dashboards_or_pause_timers(self):
        self.install_receipt(TARGET)
        self.data["defaults"]["engine"]["sha"] = TARGET
        self.data["hosts"] = {"remote": {"engine": {"sha": TARGET}}}
        self.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
        context = (self.run_cmd, self.backend, self.data["repo"])
        self.assertEqual(apply.reconcile_fleet_engine(self.data, None, {"local": context, "remote": context}), "unchanged")
        self.assertEqual(self.installs(), [])
        self.backend.start.assert_not_called()
        self.backend.pause_timer.assert_not_called()
        self.backend.resume_timer.assert_not_called()


    def test_configured_extras_are_preserved_on_target_and_rollback(self):
        self.data["defaults"]["factory_extras"] = ["atlas", "ops"]
        self.bad_rev = True
        self.assertEqual(self.reconcile(), "failed")
        self.assertEqual([argv[argv.index("--from") + 1] for argv in self.installs()],
                         [f"git+{REPO}@{TARGET}[atlas,ops]", f"git+{REPO}@{OLD}[atlas,ops]"])
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], OLD)
        self.assertEqual(self.active, {"factory-widgets.timer"})


    def test_stale_controller_and_remote_apply_migrate_bytes_and_clean_historical_engine(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                remote = self.root / f"remote-{explicit}"
                remote_home = remote / "home"
                remote_config = remote / "config/factory/config.toml"
                remote_tool = remote / "tools/factory"
                remote_cli = remote / "bin/factory"
                legacy = remote_home / ".local/share/district" / ("factory-" + "d" * 40)
                legacy.mkdir(parents=True)
                remote_config.parent.mkdir(parents=True)
                remote_original = b'# remote original\r\n[defaults]\r\nfactory_source = "/old/remote"\r\n'
                remote_config.write_bytes(remote_original)
                original = (
                    f'# controller original\n[defaults]\nfactory_repo = "{REPO}"\nfactory_ref = "main"\n'
                    'factory_track_branch = true\n'
                    f'factory_source = "{self.checkout}"\n'
                    '[hosts.remote]\nssh = "fake@remote"\nplatform = "linux"\n'
                    f'factory_source = "{self.checkout}"\n'
                    f'[hosts.remote.engine]\nsha = "{OLD}"\nref = "main"\n'
                    f'[hosts.remote.defaults]\nfactory_source = "{self.checkout}"\n'
                    f'[repo."acme/widgets"]\npath = "{remote}/widgets"\nhost = "remote"\n'
                ).replace("\n", "\r\n").encode()
                host.path().parent.mkdir(parents=True, exist_ok=True)
                host.path().write_bytes(original)  # Do not call host.save before apply.
                agents = remote_home / "Library/LaunchAgents"
                agents.mkdir(parents=True)
                reference = agents / "com.example.factory-archived.plist"
                reference.write_bytes(plistlib.dumps({
                    "Label": "factory-archived", "ProgramArguments": [str(legacy / ".venv/bin/python")]}))
                remote_env = {
                    "HOME": str(remote_home), "PATH": os.defpath,
                    "XDG_CONFIG_HOME": str(remote / "config"), "XDG_DATA_HOME": str(remote / "data"),
                    "XDG_STATE_HOME": str(remote / "state"), "XDG_CACHE_HOME": str(remote / "cache"),
                    "UV_TOOL_DIR": str(remote / "tools"), "UV_TOOL_BIN_DIR": str(remote / "bin"),
                }
                commands = []
                installed = []

                def receipt(sha):
                    (remote_tool / "bin").mkdir(parents=True, exist_ok=True)
                    for name in ("factory", "python"):
                        executable = remote_tool / "bin" / name
                        if not executable.exists():
                            executable.touch(mode=0o755)
                    remote_cli.parent.mkdir(parents=True, exist_ok=True)
                    for name in ("gh", "git", "uv", "python3"):
                        executable = remote_cli.parent / name
                        if not executable.exists():
                            executable.touch(mode=0o755)
                    remote_cli.unlink(missing_ok=True)
                    remote_cli.symlink_to(remote_tool / "bin/factory")
                    (remote_tool / "uv-receipt.toml").write_text(
                        f'[tool]\nrequirements = [{{name="factory", rev="{sha}"}}]\n')
                    metadata = remote_tool / "lib/python3.12/site-packages/factory-0.4.7.dist-info/METADATA"
                    metadata.parent.mkdir(parents=True, exist_ok=True)
                    metadata.write_text("Metadata-Version: 2.4\nName: factory\nVersion: 0.4.7\n")
                def transport(argv, **kwargs):
                    parts = shlex.split(remote_command(argv))
                    command_cwd = None
                    if parts[:2] == ["python3", "-c"] and parts[2] == host.REMOTE_DEADLINE:
                        parts = shlex.split(parts[-1])
                    if parts[:1] == ["cd"]:
                        command_cwd = Path(parts[1])
                        self.assertNotEqual(command_cwd, self.checkout)
                        self.assertEqual(parts[2], "&&")
                        parts = parts[3:]
                    if parts[:1] == ["env"]:
                        parts = parts[1:]
                        while parts and "=" in parts[0]:
                            parts = parts[1:]
                    self.assertFalse(any(str(self.checkout) in part for part in parts),
                                     "remote commands must never consult the stale checkout")
                    commands.append(parts)
                    if parts[:2] == ["/usr/bin/env", "-i"]:
                        return self.run_cmd(parts, cwd=command_cwd, **kwargs)
                    if parts[:2] == ["python3", "-c"]:
                        return subprocess.run([sys.executable, *parts[1:]], env=remote_env,
                                              capture_output=True, text=True, check=False,
                                              input=kwargs.get("input"), timeout=kwargs.get("timeout"))
                    if parts[:2] == ["git", "ls-remote"]:
                        self.assertIn(REPO, parts)
                        return subprocess.CompletedProcess(parts, 0, f"{self.tip}\trefs/heads/main\n", "")
                    if parts[:2] == ["gh", "api"]:
                        return self.run_cmd(parts, **kwargs)
                    if parts[:3] == ["uv", "tool", "install"]:
                        installed.append(parts)
                        receipt(OLD if f"@{OLD}" in " ".join(parts) else TARGET)
                        return subprocess.CompletedProcess(parts, 0, "", "")
                    if parts[0] in (str(remote_cli), "factory"):
                        if parts[1:] in (["install", "--print"], ["install"]):
                            body = (
                                "[Service]\n"
                                f'Environment="PATH={remote_cli.parent}" "HOME={remote_home}"\n'
                                f"ExecStart={remote_tool}/bin/python -m factory dispatch\n"
                            )
                            if parts[1:] == ["install", "--print"]:
                                return subprocess.CompletedProcess(parts, 0, "# factory-widgets.service\n" + body, "")
                            units = remote / "config/systemd/user"
                            units.mkdir(parents=True, exist_ok=True)
                            (units / "factory-widgets.service").write_text(body)
                        if parts[1:2] == ["install"]:
                            return subprocess.CompletedProcess(parts, 0, "", "")
                        return self.run_cmd(["factory", *parts[1:]], **kwargs)
                    if parts == ["systemctl", "--user", "daemon-reload"]:
                        return subprocess.CompletedProcess(parts, 0, "", "")
                    self.fail(f"unexpected remote command (checkout reads/archive forbidden): {parts}")

                receipt(OLD)
                self.tip, self.compare_status = TARGET, "ahead"
                runner = host.RemoteRunner("remote", "fake@remote", transport=transport)
                def controller(argv, **kwargs):
                    self.assertNotIn(argv[0], ("git", "gh"), "target resolution must run remotely")
                    self.assertNotEqual(kwargs.get("cwd"), self.checkout)
                    return self.run_cmd(argv, **kwargs)

                original_open = Path.open
                def open_without_checkout(path, *args, **kwargs):
                    self.assertFalse(path.is_relative_to(self.checkout), "stale checkout must never be read")
                    return original_open(path, *args, **kwargs)

                with mock.patch.object(host, "remote_runner", return_value=runner), mock.patch.object(
                        apply, "run", side_effect=controller), mock.patch.object(
                        host, "run", side_effect=controller), mock.patch.object(
                        install_state, "load_releases", return_value=install_state.unknown_releases()), mock.patch.object(
                        install_state, "tool_versions", return_value={"district": "test", "factory": "test"}), mock.patch.object(
                        Path, "open", new=open_without_checkout):
                    self.assertEqual(apply.main(["--upgrade", "main"] if explicit else []), 0)
                    for config, before in ((host.path(), original), (remote_config, remote_original)):
                        backups = list(config.parent.glob("config.toml.*.bak"))
                        self.assertIn(before, [backup.read_bytes() for backup in backups])
                        self.assertNotIn("factory_source", config.read_text())
                    self.assertEqual(host.load()["hosts"]["remote"]["engine"]["target"], TARGET)
                    self.assertEqual(tomllib.loads(remote_config.read_text())["defaults"]["engine"]["sha"], TARGET)
                    self.assertEqual(len(installed), 1)
                    self.assertTrue(legacy.exists(), "remote historical folder is still referenced")
                    self.tip, self.compare_status = OLD, "behind"
                    self.assertEqual(apply.main(["--upgrade", "main"]), 1)
                    self.assertIn(TARGET, self.output.getvalue())
                    self.assertIn(OLD, self.output.getvalue())
                    self.assertEqual(len(installed), 1, "ancestor must not install without override")
                    self.tip, self.compare_status = TARGET, "ahead"
                    reference.unlink()
                    self.assertEqual(apply.main(["--upgrade", "main"]), 0)
                    self.assertFalse(legacy.exists(), "unreferenced historical folder must be removed remotely")
                    self.assertEqual(len(installed), 1)
                self.assertFalse(any(parts[:2] in (["git", "archive"], ["git", "rev-parse"]) for parts in commands))

    def test_saved_recovery_precedes_resolution_and_runs_without_policy_locally_and_in_fleet(self):
        for fleet in (False, True):
            for no_policy in (False, True):
                with self.subTest(fleet=fleet, no_policy=no_policy):
                    self.calls.clear()
                    self.events.clear()
                    self.backend.start.reset_mock()
                    self.active.clear()
                    self.install_receipt(TARGET)
                    self.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
                    names = ("local", "remote") if fleet else ("local",)
                    self.data["hosts"] = {"remote": {"ssh": "fake", "platform": "linux"}}
                    self.data["defaults"]["factory_ref"] = "main"
                    if no_policy:
                        self.data["defaults"].pop("factory_ref")
                    for name in names:
                        engine = {"sha": TARGET if no_policy else OLD, "ref": "main",
                                  "target": TARGET, "target_ref": "main", "dashboards_pending": True,
                                  "paused_timers": [] if no_policy else [f"factory-{name}.timer"]}
                        host.set_engine(self.data, name, engine)

                    def offline(argv, **kwargs):
                        if argv[0] in ("git", "gh"):
                            self.events.append(("resolution", "offline"))
                            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                        return self.run_cmd(argv, **kwargs)

                    contexts = {name: (offline, self.backend, self.data["repo"]) for name in names}
                    if fleet:
                        result = apply.reconcile_fleet_engine(self.data, None, contexts)
                    else:
                        result = self.reconcile(run_cmd=offline)
                    self.assertEqual(result, "unchanged" if no_policy else "failed")
                    for name in names:
                        engine = host.engine(self.data, name)
                        self.assertEqual(engine["paused_timers"], [])
                        self.assertFalse(engine["dashboards_pending"])
                        self.assertEqual(engine["sha"], TARGET)
                    self.assertEqual(self.installs(), [])
                    self.assertEqual(self.backend.start.call_count, len(names))
                    if not no_policy:
                        resolution = self.events.index(("resolution", "offline"))
                        for name in names:
                            self.assertIn(("enable", f"factory-{name}.timer"), self.events[:resolution])

    def test_no_policy_apply_recovers_interrupted_install_without_active_timers(self):
        self.data["defaults"].pop("factory_ref")
        self.active.clear()
        self.backend.status.side_effect = lambda name, **_: service.ServiceStatus(
            "loaded" if name.endswith(".timer") else "not-found", "inactive", "enabled")

        def crash(argv, **kwargs):
            if argv[:3] == ["uv", "tool", "install"]:
                self.cli.unlink()
                (self.tool / "uv-receipt.toml").unlink()
                raise SystemExit("simulated crash during uv replacement")
            return self.run_cmd(argv, **kwargs)

        with self.assertRaises(SystemExit):
            apply.upgrade(self.data, backend=self.backend, run_cmd=crash)
        interrupted = host.load()
        self.data = interrupted
        self.assertEqual(self.apply(), 0)
        self.assertEqual(host.engine(host.load(), "local")["sha"], TARGET)
        self.assertFalse(host.engine(host.load(), "local").get("install_pending"))
        self.assertEqual(interrupted["defaults"]["engine"]["install_pending"],
                         {"target": TARGET, "previous": OLD, "ref": "v0.4.7"})
        self.assertEqual(len(self.installs()), 1)
        self.backend.pause_timer.assert_not_called()
        self.backend.resume_timer.assert_not_called()

    def test_no_policy_recovery_verifies_completed_install_without_reinstalling(self):
        self.data["defaults"].pop("factory_ref")
        self.active.clear()

        def crash_after_install(argv, **kwargs):
            if argv == [str(self.cli), "--version"] and self.installs():
                raise SystemExit("simulated crash before verified state save")
            return self.run_cmd(argv, **kwargs)

        with self.assertRaises(SystemExit):
            apply.upgrade(self.data, backend=self.backend, run_cmd=crash_after_install)
        self.data = host.load()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(host.engine(host.load(), "local")["sha"], TARGET)
        self.assertFalse(host.engine(host.load(), "local").get("install_pending"))
        self.assertEqual(len(self.installs()), 1)

    def test_inflight_recovery_clears_marker_only_after_verified_install_or_rollback(self):
        self.data["defaults"].pop("factory_ref")
        self.active.clear()
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails):
                self.install_receipt(OLD)
                self.cli.unlink()
                self.data["defaults"]["engine"] = {
                    "sha": OLD, "target": TARGET,
                    "install_pending": {"target": TARGET, "previous": OLD, "ref": "main"}}

                def failed_install(argv, **kwargs):
                    if argv[:3] == ["uv", "tool", "install"] and (
                            rollback_fails or f"@{TARGET}" in argv[argv.index("--from") + 1]):
                        return subprocess.CompletedProcess(argv, 1, "", "offline")
                    return self.run_cmd(argv, **kwargs)

                self.assertEqual(self.reconcile(run_cmd=failed_install), "failed")
                engine = host.engine(host.load(), "local")
                self.assertEqual(bool(engine.get("install_pending")), rollback_fails)
                if rollback_fails:
                    self.assertFalse(self.cli.exists())
                else:
                    self.assertEqual(engine["sha"], OLD)
                    self.assertTrue(self.cli.exists())


    def test_no_policy_apply_rejects_legacy_cli_symlink_missing_cli_and_receipt(self):
        self.data["defaults"].pop("factory_ref")
        legacy = self.root / "data/district/factory-old/.venv/bin/factory"
        legacy.parent.mkdir(parents=True)
        legacy.touch()
        self.render_executable = str(self.cli)
        for invalid in ("legacy-symlink", "missing-cli", "missing-receipt"):
            with self.subTest(invalid=invalid):
                self.install_receipt(OLD)
                self.calls.clear()
                self.output.truncate(0)
                self.output.seek(0)
                if invalid == "missing-receipt":
                    (self.tool / "uv-receipt.toml").unlink()
                else:
                    self.cli.unlink()
                    if invalid == "legacy-symlink":
                        self.cli.symlink_to(legacy)
                self.assertEqual(self.apply(), 1)
                self.assertIn("acme/widgets", self.output.getvalue())
                self.assertIn(str(self.cli), self.output.getvalue())
                self.assertFalse(any(argv[0] in ("factory", str(self.cli)) for argv, _ in self.calls))
                self.assertEqual(self.installs(), [])


    def test_resolution_timeouts_fail_engine_but_finish_single_and_fleet_rows(self):
        for fleet in (False, True):
            for boundary in ("refs", "checks", "history", "compare"):
                with self.subTest(fleet=fleet, boundary=boundary):
                    self.calls.clear()
                    self.events.clear()
                    self.output.truncate(0)
                    self.output.seek(0)
                    self.tip = TIP if boundary == "history" else TARGET
                    self.data["defaults"]["engine"] = {"sha": OLD, "ref": "main"}
                    self.data["hosts"] = {"remote": {"ssh": "fake", "platform": "linux"}}
                    self.data["repo"]["acme/widgets"]["hosts"] = ["local", "remote"] if fleet else ["local"]
                    if fleet:
                        self.data["repo"]["acme/widgets"]["triage_host"] = "local"
                    else:
                        self.data["repo"]["acme/widgets"].pop("triage_host", None)
                    self.data["repo"]["acme/other"] = {"path": str(self.other_repo)}

                    def timeout(argv, **kwargs):
                        endpoint = " ".join(argv)
                        if ((boundary == "refs" and argv[:2] == ["git", "ls-remote"])
                                or (boundary == "checks" and "check-runs" in endpoint)
                                or (boundary == "history" and "/commits?" in endpoint)
                                or (boundary == "compare" and "/compare/" in endpoint)):
                            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                        return self.run_cmd(argv, **kwargs)

                    runner = mock.Mock(side_effect=timeout)
                    runner.facts.return_value = {"home": str(Path.home()), "path": os.environ["PATH"]}
                    with mock.patch.object(apply, "run", side_effect=timeout), mock.patch.object(
                            host, "claims_supported", return_value=True), mock.patch.object(
                            apply, "_host_context", return_value=(runner, self.backend)):
                        self.assertEqual(self.apply(), 1)
                    self.assertEqual(self.installs(), [])
                    self.backend.pause_timer.assert_not_called()
                    self.assertIn("engine", self.output.getvalue())
                    self.assertIn("timed out", self.output.getvalue())
                    self.assertIn("acme/other  0.4.7", self.output.getvalue())
                    self.assertEqual(sum(argv[1:] == ["doctor", "--json"] for argv, _ in self.calls),
                                     3 if fleet else 2)

    def test_launchd_dashboard_hangs_preserve_pending_intent_within_budget(self):
        launchctl = self.root / "launchctl"
        counter = self.root / "prints"
        backend_home = Path.home()
        agents = backend_home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "factory-widgets-dashboard.plist").write_bytes(plistlib.dumps({
            "Label": "factory-widgets-dashboard", "KeepAlive": True,
            "ProgramArguments": [str(self.cli), "dashboard"]}))
        for stalled in ("print", "kickstart"):
            with self.subTest(stalled=stalled):
                counter.write_text("0")
                launchctl.write_text(
                    f"#!{sys.executable}\n"
                    "import pathlib,sys,time\n"
                    f"counter = pathlib.Path({str(counter)!r})\n"
                    "command = sys.argv[1]\n"
                    "if command == 'print':\n"
                    "    n = int(counter.read_text()) + 1\n"
                    "    counter.write_text(str(n))\n"
                    f"    if {stalled!r} == 'print' and n == 2: time.sleep(1.5)\n"
                    f"if command == {stalled!r} == 'kickstart': time.sleep(1.5)\n"
                    "print('state = running\\npid = 42')\n")
                launchctl.chmod(0o755)

                def transport(argv, **kwargs):
                    # Execute the real remote watchdog locally, never SSH/launchctl.
                    return subprocess.run(shlex.split(argv[-1]), capture_output=True, text=True,
                                          timeout=kwargs.get("timeout"), input=kwargs.get("input"))

                runner = host.RemoteRunner("fake-mac", "unused", transport=transport)
                backend = service.LaunchdBackend(
                    runner, home=runner.path(str(backend_home)), uid=501, executable=str(launchctl))
                self.data["defaults"]["engine"]["dashboards_pending"] = True
                host.save(self.data)
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    apply._finish_dashboards(
                        self.data, "local", self.run_cmd, self.data["repo"], backend,
                        deadline=started + 0.5)
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertTrue(host.engine(host.load(), "local")["dashboards_pending"])

    def timeout_remote(self, boundary):
        fired = []
        synced = []
        commands = []
        receipts = []

        def transport(argv, **kwargs):
            parts = shlex.split(remote_command(argv))
            command_cwd = None
            if parts[:2] == ["python3", "-c"] and parts[2] == host.REMOTE_DEADLINE:
                parts = shlex.split(parts[-1])
            if parts[0] == "cd":
                command_cwd = Path(parts[1])
                parts = parts[3:]
            if parts[0] == "env" and parts[:3] != ["env", "LC_ALL=C", "ps"]:
                parts = parts[1:]
                while "=" in parts[0]:
                    parts = parts[1:]
            commands.append(parts)
            if parts[:2] == ["/usr/bin/env", "-i"]:
                return self.run_cmd(parts, cwd=command_cwd, **kwargs)
            if parts[:2] == ["python3", "-c"]:
                sync = "os.replace" in parts[2] and parts[-1].endswith("config.toml")
                if synced and synced[-1].get("target") == TARGET and parts[-1].endswith("uv-receipt.toml"):
                    receipts.append(parts)
                engine = tomllib.loads(kwargs["input"]).get("defaults", {}).get("engine", {}) if sync else {}
                if sync:
                    synced.append(engine)
                expire = (
                    boundary == "initial-sync" and sync
                    or boundary == "target-save" and sync and engine.get("target") == TARGET
                    or boundary == "receipt" and synced and synced[-1].get("target") == TARGET
                    and parts[-1].endswith("uv-receipt.toml")
                    or boundary == "verification" and len(receipts) == 2
                    or boundary == "dashboard-save" and sync and engine.get("dashboards_pending") is False
                )
                if expire and not fired:
                    self.assertGreater(kwargs.get("timeout", 0), 0)
                    fired.append(parts)
                    raise subprocess.TimeoutExpired(parts, kwargs["timeout"])
                return subprocess.run([sys.executable, *parts[1:]], capture_output=True, text=True,
                                      input=kwargs.get("input"), timeout=kwargs.get("timeout"))
            return self.run_cmd(parts, **kwargs)

        runner = host.RemoteRunner("remote", "unused", transport=transport)
        runner._facts = {"home": str(Path.home()), "path": os.environ["PATH"], "uid": 501,
                         "config_home": str(self.root / "remote-config"),
                         "data_home": str(self.root / "remote-data"),
                         "tool_dir": str(self.tool.parent), "tool_bin": str(self.cli.parent)}
        return runner, fired, commands

    def test_initial_remote_sync_timeout_fails_named_host_and_checks_next_host(self):
        for policy in (False, True):
            with self.subTest(policy=policy):
                self.data["defaults"]["engine"] = {"sha": TARGET, "ref": "main"}
                if not policy:
                    self.data["defaults"].pop("factory_ref", None)
                else:
                    self.data["defaults"]["factory_ref"] = "main"
                self.data["hosts"] = {"remote": {"ssh": "unused", "platform": "linux",
                                                "engine": {"sha": TARGET, "dashboards_pending": True}}}
                self.data["repo"] = {
                    "acme/widgets": {"path": str(self.repo), "host": "remote"},
                    "acme/other": {"path": str(self.other_repo)},
                }
                self.install_receipt(TARGET)
                self.calls.clear()
                self.output.truncate(0)
                self.output.seek(0)
                self.backend.definitions_changed.return_value = False
                runner, fired, commands = self.timeout_remote("initial-sync")
                with mock.patch.object(host, "remote_runner", return_value=runner):
                    self.assertEqual(self.apply(), 1)
                self.assertEqual(len(fired), 1)
                self.assertIn("acme/widgets: host remote", self.output.getvalue())
                self.assertIn("timed out", self.output.getvalue())
                self.assertIn("acme/other  0.4.7", self.output.getvalue())
                self.assertFalse(any(argv[1:] == ["doctor", "--json"] for argv in commands))
                self.assertFalse(any(argv[1:] == ["install"] for argv in commands))
                self.assertTrue(host.engine(host.load(), "remote")["dashboards_pending"])
                self.assertEqual(self.installs(), [])

    def test_target_sync_and_receipt_timeouts_fail_engine_without_install_and_check_other_repos(self):
        for boundary in ("target-save", "receipt", "verification"):
            with self.subTest(boundary=boundary):
                self.data["hosts"] = {"remote": {"ssh": "unused", "platform": "linux",
                                                "engine": {"sha": OLD, "ref": "main"}}}
                self.data["repo"] = {
                    "acme/widgets": {"path": str(self.repo), "host": "remote"},
                    "acme/other": {"path": str(self.other_repo), "host": "remote"},
                }
                self.install_receipt(OLD)
                self.calls.clear()
                self.output.truncate(0)
                self.output.seek(0)
                self.backend.definitions_changed.return_value = True
                runner, fired, commands = self.timeout_remote(boundary)
                with mock.patch.object(host, "remote_runner", return_value=runner):
                    self.assertEqual(self.apply(), 1)
                self.assertEqual(len(fired), 1)
                self.assertIn("engine:remote", self.output.getvalue())
                self.assertIn("timed out", self.output.getvalue())
                self.assertIn("acme/other  0.4.7", self.output.getvalue())
                self.assertEqual(sum(argv[1:] == ["doctor", "--json"] for argv in commands), 2)
                self.assertFalse(any(argv[1:] == ["install"] for argv in commands))
                self.assertEqual(self.installs(), [])
                self.assertEqual(host.engine(host.load(), "remote")["sha"], OLD)

    def test_fleet_target_and_dashboard_timeouts_fail_named_engine_and_continue_hosts(self):
        for boundary in ("target-save", "dashboard-command", "dashboard-save"):
            with self.subTest(boundary=boundary):
                self.data["defaults"]["engine"] = {"sha": OLD, "ref": "main"}
                self.data["hosts"] = {"remote": {"ssh": "unused", "platform": "linux",
                                                "engine": {"sha": OLD, "ref": "main"}}}
                self.data["repo"] = {
                    "acme/widgets": {"path": str(self.repo), "host": "remote"},
                    "acme/other": {"path": str(self.other_repo)},
                }
                self.active.clear()
                self.install_receipt(OLD)
                self.calls.clear()
                self.output.truncate(0)
                self.output.seek(0)
                self.backend.definitions_changed.return_value = True
                self.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
                runner, fired, commands = self.timeout_remote(boundary)

                def dashboard(name, **kwargs):
                    if boundary == "dashboard-command" and name == "factory-widgets-dashboard.service":
                        fired.append(name)
                        raise subprocess.TimeoutExpired(name, kwargs["timeout"])
                    return subprocess.CompletedProcess([], 0, "", "")

                self.backend.start.side_effect = dashboard
                with mock.patch.object(host, "remote_runner", return_value=runner):
                    self.assertEqual(self.apply(), 1)
                self.assertTrue(fired)
                self.assertIn("remote", self.output.getvalue())
                self.assertIn("timed out", self.output.getvalue())
                self.assertIn("acme/other  0.4.7", self.output.getvalue())
                if boundary == "target-save":
                    self.assertEqual(self.installs(), [])
                    self.assertFalse(any(argv[1:] == ["install"] for argv in commands))
                else:
                    self.assertTrue(host.engine(host.load(), "remote")["dashboards_pending"])
                    self.assertFalse(host.engine(host.load(), "local")["dashboards_pending"])

    def binary_launchd_backend(self, remote):
        home = self.root / "binary-home"
        agents = home / "Library/LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        self.launchd_disabled = set()
        self.launchd_running = True
        self.launchd_calls = []

        def launchctl(argv, **kwargs):
            self.launchd_calls.append((argv, kwargs))
            operation = argv[1]
            label = argv[-1].rsplit("/", 1)[-1]
            if operation == "print" and not (agents / f"{label}.plist").exists():
                return subprocess.CompletedProcess(argv, 3, "", "not loaded")
            if operation == "disable":
                self.launchd_disabled.add(label)
            elif operation == "enable":
                self.launchd_disabled.discard(label)
            else:
                self.assertIn(operation, ("print", "print-disabled", "bootstrap", "kickstart", "bootout"))
            running = self.launchd_running
            if operation == "print":
                try:
                    document = plistlib.loads((agents / f"{label}.plist").read_bytes())
                except (plistlib.InvalidFileException, ExpatError, ValueError):
                    document = {}
                scheduled = "StartInterval" in document or "StartCalendarInterval" in document
                running = running and label not in self.launchd_disabled if scheduled else running
            output = ("state = running\npid = 42\n" if running else
                      "state = exited\nlast exit code = 0\n")
            if operation == "print-disabled":
                output = "\n".join(f'"{name}" => true' for name in self.launchd_disabled)
            return subprocess.CompletedProcess(argv, 0, output, "")

        runner = None
        if remote:
            self.data["hosts"] = {"remote": {"ssh": "unused", "platform": "macos",
                                           "engine": {"sha": OLD, "ref": "main"}}}
            self.data["repo"]["acme/widgets"]["host"] = "remote"
            runner, _, _ = self.timeout_remote("none")
            transport = runner.transport

            def remote_transport(argv, **kwargs):
                parts = shlex.split(remote_command(argv))
                if parts[:2] == ["python3", "-c"] and parts[2] == host.REMOTE_DEADLINE:
                    parts = shlex.split(parts[-1])
                if parts[0] == "launchctl":
                    return launchctl(parts, **kwargs)
                return transport(argv, **kwargs)

            runner.transport = remote_transport
        backend = service.LaunchdBackend(runner or launchctl,
            home=runner.path(str(home)) if runner else home, uid=501, raw_path=os.defpath)
        return backend, runner, agents

    def test_binary_launchd_plists_discover_status_and_refuse_engine_upgrade(self):
        for remote in (False, True):
            with self.subTest(remote=remote), contextlib.ExitStack() as stack:
                self.install_receipt(OLD)
                self.calls.clear()
                self.render_plist = True
                backend, runner, agents = self.binary_launchd_backend(remote)
                stack.enter_context(mock.patch.object(host, "service_backend", return_value=backend))
                if runner:
                    stack.enter_context(mock.patch.object(host, "remote_runner", return_value=runner))
                for label, scheduled in (("factory-aux", True), ("factory-aux-worker", False)):
                    document = {"Label": label, "ProgramArguments": [
                        str(self.tool / "bin/python"), "-m", "factory", "dispatch"],
                        "EnvironmentVariables": {"UV_EXCLUDE_NEWER": "PT24H"}}
                    if scheduled:
                        document["StartInterval"] = 600
                    (agents / f"{label}.plist").write_bytes(
                        plistlib.dumps(document, fmt=plistlib.FMT_BINARY))
                    with self.subTest(status=label):
                        status = backend.status(f"{label}.service", timeout=2)
                        self.assertEqual(status.environment, {"UV_EXCLUDE_NEWER": "PT24H"})
                        self.assertEqual(status.active_state, "activating" if scheduled else "active")
                        self.assertEqual(status.main_pid, 42)
                        self.assertEqual(status.argv, tuple(document["ProgramArguments"]))
                with self.subTest(discovery=True):
                    self.assertEqual(backend.factory_units(timeout=2),
                                     {"factory-aux.service", "factory-aux.timer", "factory-aux-worker.service"})
                self.launchd_running = False
                with self.subTest(apply=True):
                    # macOS engine upgrades are unsupported pending #199: reconcile_engine
                    # refuses with unsupported_platform and issues no launchctl mutation.
                    with self.assertRaises(host.DistrictError) as raised:
                        apply.reconcile_engine(
                            self.data, backend=backend, run_cmd=runner or apply.run,
                            host_name="remote" if remote else "local")
                    self.assertIn("unsupported_platform", str(raised.exception))
                    self.assertIn("issues/199", str(raised.exception))
                    self.assertEqual(self.installs(), [])
                    operations = [argv[1] for argv, _ in self.launchd_calls]
                    for forbidden in ("disable", "enable", "kill", "bootout"):
                        self.assertNotIn(forbidden, operations,
                                         f"macOS engine refusal must never {forbidden}")
                    self.assertEqual(self.launchd_disabled, set())
                    self.assertEqual(host.engine(host.load(), "remote" if remote else "local")["sha"], OLD)
                for path in agents.glob("*.plist"):
                    path.unlink()

    def test_malformed_launchd_plist_status_is_tolerant_and_macos_apply_refuses(self):
        for remote in (False, True):
            with self.subTest(remote=remote), contextlib.ExitStack() as stack:
                self.install_receipt(OLD)
                self.calls.clear()
                backend, runner, agents = self.binary_launchd_backend(remote)
                stack.enter_context(mock.patch.object(host, "service_backend", return_value=backend))
                if runner:
                    stack.enter_context(mock.patch.object(host, "remote_runner", return_value=runner))
                (agents / "factory-broken.plist").write_bytes(
                    b'<?xml version="1.0"?><plist version="1.0"><dict><key>StartInterval</key>'
                    b'<integer>not-an-integer</integer></dict></plist>')
                with self.subTest(status=True):
                    status = backend.status("factory-broken.service", timeout=2)
                    self.assertEqual(status.environment, {})
                    self.assertEqual(status.active_state, "active")
                self.launchd_running = False
                with self.subTest(apply=True):
                    # macOS engine upgrades are refused pending #199 before the engine
                    # step touches units; a remote darwin host fails the row (exit 1),
                    # a local darwin host fails the apply outright.
                    if remote:
                        self.assertEqual(self.apply(), 1, self.output.getvalue())
                        self.assertIn("unsupported_platform", self.output.getvalue())
                    else:
                        with self.assertRaises(host.DistrictError) as raised:
                            self.apply()
                        self.assertIn("unsupported_platform", str(raised.exception))
                    self.assertEqual(self.installs(), [])


    def test_registered_launchd_plist_bytes_reconcile_full_repo_drift(self):
        for remote in (False, True):
            for fmt in (plistlib.FMT_BINARY, plistlib.FMT_XML):
                for drift in ("unchanged", "changed", "malformed", "malformed-xml"):
                    with self.subTest(remote=remote, fmt=fmt, drift=drift), contextlib.ExitStack() as stack:
                        self.install_receipt(TARGET)
                        self.calls.clear()
                        self.events.clear()
                        self.data["defaults"]["engine"] = {"sha": TARGET, "ref": "main"}
                        if not remote:
                            self.data.pop("hosts", None)
                            self.data["repo"]["acme/widgets"].pop("host", None)
                        backend, runner, agents = self.binary_launchd_backend(remote)
                        if remote:
                            self.data["hosts"]["remote"]["engine"]["sha"] = TARGET
                            stack.enter_context(mock.patch.object(host, "remote_runner", return_value=runner))
                        stack.enter_context(mock.patch.object(host, "service_backend", return_value=backend))
                        self.launchd_running = False
                        filename = "factory-widgets.plist"
                        document = {"Label": "factory-widgets", "StartInterval": 600,
                                    "ProgramArguments": [str(self.tool / "bin/python"), "-m", "factory", "dispatch"],
                                    "EnvironmentVariables": {"PATH": str(self.cli.parent)}}
                        body = plistlib.dumps(document).decode()
                        stack.enter_context(mock.patch.object(self, "rendered", return_value=(filename, body)))
                        existing = dict(document, StartInterval=900) if drift == "changed" else document
                        raw = (b"not a plist" if drift == "malformed" else b"<plist><dict></plist>"
                               if drift == "malformed-xml" else plistlib.dumps(existing, fmt=fmt))
                        (agents / filename).write_bytes(raw)
                        gh = self.cli.parent / "gh"
                        gh.write_text("#!/bin/sh\nexit 0\n")
                        gh.chmod(0o755)
                        (self.tool / "bin/factory").chmod(0o755)
                        original = self.run_cmd
                        repo_installs = []

                        def commands(argv, **kwargs):
                            if argv == [str(gh), "--version"]:
                                return subprocess.CompletedProcess(argv, 0, "gh fake\n", "")
                            if argv == [str(self.cli), "install"]:
                                repo_installs.append(list(argv))
                                (agents / filename).write_text(body)
                                return subprocess.CompletedProcess(argv, 0, "", "")
                            return original(argv, **kwargs)

                        stack.enter_context(mock.patch.object(self, "run_cmd", side_effect=commands))
                        stack.enter_context(mock.patch.object(apply, "run", side_effect=commands))
                        errors = {}
                        changes = apply.plan_apply(self.data["repo"], backend, run_cmd=runner or apply.run, errors=errors)
                        self.assertEqual(changes, {"acme/widgets": drift != "unchanged"})
                        if drift.startswith("malformed"):
                            self.assertIn(filename, " ".join(errors["acme/widgets"]))
                        else:
                            self.assertEqual(errors, {})
                        self.assertEqual(self.apply(), int(drift.startswith("malformed")), self.output.getvalue())
                        self.assertEqual(self.installs(), [])
                        self.assertEqual(len(repo_installs), int(drift == "changed"))
                        if drift == "changed":
                            self.assertEqual(plistlib.loads((agents / filename).read_bytes()), document)
                        else:
                            self.assertEqual((agents / filename).read_bytes(), raw)
                        if drift.startswith("malformed"):
                            self.assertIn(filename, self.output.getvalue())

    def test_already_current_upgrade_keeps_operator_disabled_timer_inactive(self):
        self.install_receipt(TARGET)
        self.data["defaults"]["engine"] = {"sha": TARGET, "ref": "main"}
        self.backend.definitions_changed.return_value = False
        self.active = set()  # the timer is inactive before apply
        # An existing operator-disabled timer, not a first install (unit_file_state present).
        self.backend.status.return_value = service.ServiceStatus("loaded", "inactive", "disabled", 0)
        self.backend.stop.side_effect = lambda name, **kwargs: self.enable(name, False, **kwargs)
        self.apply(upgrade="main")
        self.assertNotIn("factory-widgets.timer", self.active)
        self.backend.stop.assert_called_once_with("factory-widgets.timer", timeout=apply.DRAIN_TIMEOUT)

    def test_double_install_failure_with_missing_cli_has_no_path_fallback_and_restores_timers(self):
        def broken_install(argv, **kwargs):
            if argv[:3] == ["uv", "tool", "install"]:
                self.calls.append((list(argv), kwargs))
                self.events.append(("install", argv[argv.index("--from") + 1]))
                self.cli.unlink(missing_ok=True)
                return subprocess.CompletedProcess(argv, 1, "", "install and rollback unavailable")
            if argv[0] in ("factory", str(self.cli)):
                raise FileNotFoundError(str(self.cli))  # never fall back to another factory on PATH
            return self.run_cmd(argv, **kwargs)

        with mock.patch.object(apply, "run", side_effect=broken_install):
            self.assertEqual(self.apply(upgrade="main"), 1)
        self.assertEqual(len(self.installs()), 2)  # TARGET then OLD rollback, no PATH fallback
        self.assertIn(TARGET, self.output.getvalue())
        self.assertIn(OLD, self.output.getvalue())
        self.assertEqual(self.active, {"factory-widgets.timer"})  # timers restored on exit
        self.assertFalse(host.load()["defaults"]["engine"].get("paused_timers"))

    def _fleet_backend(self, active_set, units):
        backend = mock.Mock()
        backend.factory_units.return_value = set(units)
        backend.is_active.side_effect = lambda name, **_: "active" if name in active_set else "inactive"
        backend.pause_timer.side_effect = lambda name, **_: (
            active_set.discard(name) or subprocess.CompletedProcess([], 0, "", ""))
        backend.resume_timer.side_effect = lambda name, **_: (
            active_set.add(name) or subprocess.CompletedProcess([], 0, "", ""))
        backend.status.return_value = service.ServiceStatus("loaded", "inactive", "enabled")
        backend.configuration.return_value = {"platform": "linux", "unit_dir": str(host.unit_dir())}
        backend.definitions_changed.return_value = False
        return backend

    def test_fleet_busy_worker_blocks_every_install_and_restores_timers(self):
        worker = "factory-unregistered-worker.service"
        self.data["hosts"] = {"remote": {"engine": {"sha": OLD}}}
        self.active = {"factory-widgets.timer"}
        self.backend.factory_units.return_value = {worker, "factory-widgets.timer"}
        remote_active = {"factory-other.timer"}
        remote = self._fleet_backend(remote_active, {"factory-other.timer"})
        remote_repos = {"acme/other": {"path": str(self.other_repo)}}
        contexts = {
            "local": (self.run_cmd, self.backend, self.data["repo"]),
            "remote": (self.run_cmd, remote, remote_repos),
        }
        self.service_states[worker] = "active"  # the discovered worker never drains
        clock = [0.0]
        self.sleep.side_effect = lambda s: clock.__setitem__(0, clock[0] + s)
        record = install_state.ApplyRun("", "", OLD, drain_timeout=2)
        with mock.patch.object(apply.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(apply.reconcile_fleet_engine(self.data, record, contexts),
                             "failed", self.output.getvalue())
        self.assertIn(worker, self.output.getvalue())
        self.assertEqual(self.installs(), [])
        self.assertEqual(host.engine(self.data, "local")["sha"], OLD)
        self.assertEqual(host.engine(self.data, "remote")["sha"], OLD)
        self.assertEqual(self.active, {"factory-widgets.timer"})  # paused then restored
        self.assertEqual(remote_active, {"factory-other.timer"})
        self.backend.stop.assert_not_called()
        remote.stop.assert_not_called()

    def test_fleet_rechecks_factory_service_before_each_host_install(self):
        worker = "factory-unregistered-worker.service"
        self.data["hosts"] = {"remote": {"engine": {"sha": OLD}}}
        self.backend.factory_units.return_value = {worker, "factory-widgets.timer"}
        clock = [0.0]
        self.sleep.side_effect = lambda s: clock.__setitem__(0, clock[0] + s)

        def install_then_activate(argv, **kwargs):
            result = self.run_cmd(argv, **kwargs)
            if argv[:3] == ["uv", "tool", "install"] and f"@{TARGET}" in argv[5]:
                self.service_states[worker] = "active"  # a pass starts after the first install
            return result

        context = (install_then_activate, self.backend, self.data["repo"])
        record = install_state.ApplyRun("", "", OLD, drain_timeout=2)
        with mock.patch.object(apply.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(apply.reconcile_fleet_engine(
                self.data, record, {"local": context, "remote": context}), "failed", self.output.getvalue())
        self.assertEqual(len(self.installs()), 1)
        self.assertIn(worker, record.deferred.get("engine:remote", {}).get("reason", ""))
        self.assertIn("could not roll back local", self.output.getvalue())
        self.assertEqual(host.engine(self.data, "local")["sha"], TARGET)
        self.assertEqual(host.engine(self.data, "remote")["sha"], OLD)
        self.assertEqual(self.active, {"factory-widgets.timer"})

    def test_macos_engine_mixed_fleet_skips_darwin_and_upgrades_linux(self):
        # Mixed fleet: the Linux host (local) upgrades normally — engine installed, its
        # active timer paused then restored — while the darwin host (remote Ultron) is
        # skipped per host with an unsupported_platform row and NO mutation at all; the
        # run ends with a partial summary and a non-zero exit.
        self.data["hosts"] = {"ultron": {"ssh": "factory@ultron", "platform": "macos",
                                         "engine": {"sha": OLD, "ref": "main"}}}
        self.data["repo"]["acme/ios"] = {"path": str(self.other_repo), "host": "ultron"}
        with mock.patch.object(host, "remote_runner") as remote_runner:
            self.assertEqual(self.apply(upgrade=TARGET), 1, self.output.getvalue())
            remote_runner.assert_not_called()  # darwin host never contacted => untouched
        output = self.output.getvalue()
        self.assertIn("unsupported_platform", output)
        self.assertIn("ultron", output)
        self.assertIn("https://github.com/mikeroySoft/district/issues/199", output)
        self.assertIn("partial:", output)
        self.assertRegex(output, r"acme/ios\b.*unsupported_platform")  # per-host skip row
        self.assertTrue(self.installs())  # Linux host upgraded
        self.assertEqual(host.engine(host.load(), "local")["sha"], TARGET)
        self.backend.pause_timer.assert_called()
        self.backend.resume_timer.assert_called()
        self.assertEqual(self.active, {"factory-widgets.timer"})  # paused then restored
        self.assertEqual(host.engine(host.load(), "ultron")["sha"], OLD)  # darwin unchanged

    def test_macos_engine_darwin_only_fleet_skips_everything(self):
        # Darwin-only fleet: nothing is mutated, a partial summary is printed, exit is
        # non-zero.
        self.data["hosts"] = {"ultron": {"ssh": "factory@ultron", "platform": "macos",
                                         "engine": {"sha": OLD, "ref": "main"}}}
        self.data["repo"] = {"acme/ios": {"path": str(self.other_repo), "host": "ultron"}}
        with mock.patch.object(host, "remote_runner") as remote_runner:
            self.assertEqual(self.apply(upgrade=TARGET), 1, self.output.getvalue())
            remote_runner.assert_not_called()
        output = self.output.getvalue()
        self.assertIn("unsupported_platform", output)
        self.assertIn("ultron", output)
        self.assertIn("partial:", output)
        self.assertEqual(self.installs(), [])
        self.backend.pause_timer.assert_not_called()
        self.backend.resume_timer.assert_not_called()
        self.backend.stop.assert_not_called()
        self.backend.start.assert_not_called()
        self.assertEqual(host.engine(host.load(), "ultron")["sha"], OLD)

    def test_macos_local_darwin_skipped_alone(self):
        # A darwin local (patched sys.platform) is the only host: skipped per host with
        # ZERO mutation (byte-identical config, unchanged mtime, no local-claim sync), a
        # partial summary, and a non-zero exit.
        host.save(self.data)
        config = host.path()
        before = hashlib.sha256(config.read_bytes()).hexdigest()
        before_mtime = config.stat().st_mtime_ns
        synced = mock.Mock()
        args = argparse.Namespace(slug=None, reset=None, upgrade=TARGET, allow_downgrade=False)
        with mock.patch.object(apply.sys, "platform", "darwin"), \
                mock.patch.object(host, "sync_remote_config", synced), \
                mock.patch.object(host, "sync_local_claims") as local_sync:
            self.assertEqual(apply._run(args, install_state.ApplyRun("", "", OLD)), 1,
                             self.output.getvalue())
        output = self.output.getvalue()
        self.assertIn("unsupported_platform", output)
        self.assertIn("local", output)
        self.assertIn("https://github.com/mikeroySoft/district/issues/199", output)
        self.assertIn("partial:", output)
        self.backend.pause_timer.assert_not_called()
        self.backend.resume_timer.assert_not_called()
        self.backend.stop.assert_not_called()
        self.backend.start.assert_not_called()
        self.assertEqual(self.installs(), [])
        self.assertEqual(host.engine(host.load(), "local")["sha"], OLD)
        self.assertEqual(hashlib.sha256(config.read_bytes()).hexdigest(), before)
        self.assertEqual(config.stat().st_mtime_ns, before_mtime)
        synced.assert_not_called()
        local_sync.assert_not_called()

    def test_macos_local_darwin_skipped_with_remote_linux_host(self):
        # A darwin local is skipped per host; a remote Linux host is NOT skipped and is
        # reached (the old fleet-wide refusal would have blocked it).
        self.install_receipt(TARGET)
        self.data["defaults"]["engine"]["sha"] = TARGET
        other = self.root / "remote-repo"
        other.mkdir()
        self.data["hosts"] = {"remote": {"ssh": "unused", "platform": "linux",
                                         "engine": {"sha": TARGET, "ref": "main"}}}
        self.data["repo"]["acme/other"] = {"path": str(other), "host": "remote"}
        self.backend.definitions_changed.return_value = False
        runner, _, _ = self.timeout_remote("none")
        with mock.patch.object(apply.sys, "platform", "darwin"), \
                mock.patch.object(host, "remote_runner", return_value=runner) as remote_runner:
            self.assertEqual(self.apply(upgrade=TARGET), 1, self.output.getvalue())
            remote_runner.assert_called()  # the Linux host is reached, not blocked
        output = self.output.getvalue()
        self.assertIn("unsupported_platform", output)
        self.assertIn("local", output)
        self.assertIn("partial:", output)
        self.assertIn("factory-widgets.timer", self.active)  # local darwin timer untouched
        self.assertEqual(host.engine(host.load(), "local")["sha"], TARGET)

    def test_crash_recovery_restores_only_saved_timers_and_leaves_inactive_untouched(self):
        # SIGKILL between stop and resume leaves the durable paused set with no readable
        # receipt, target, or recorded SHA. A new apply restores exactly the saved
        # active-before timers and never starts a timer that was inactive before.
        self.data["defaults"].pop("factory_ref")
        self.data["defaults"]["engine"] = {"paused_timers": ["factory-widgets.timer"]}
        self.active.clear()  # widgets stopped by the crash; factory-dormant.timer inactive before
        (self.tool / "uv-receipt.toml").unlink()
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.active, {"factory-widgets.timer"})
        resumed = [call.args[0] for call in self.backend.resume_timer.call_args_list]
        self.assertEqual(resumed, ["factory-widgets.timer"])
        self.assertEqual(host.engine(host.load(), "local")["paused_timers"], [])

    def test_recovery_without_readable_receipt_or_recorded_revision_names_paused_timers(self):
        # Restored from origin/main, adapted: recovery with no receipt/target/SHA now
        # restores the named paused timers (instead of leaving them stopped) and fails.
        self.data["defaults"].pop("factory_ref")
        self.data["defaults"]["engine"] = {"paused_timers": ["factory-widgets.timer"]}
        self.active.clear()
        (self.tool / "uv-receipt.toml").unlink()
        self.assertEqual(self.apply(), 1)
        self.assertRegex(self.output.getvalue(),
                         r"engine:local restart factory-widgets\.timer ActiveState=active")
        self.assertEqual(self.active, {"factory-widgets.timer"})
        self.assertEqual(host.engine(host.load(), "local")["paused_timers"], [])
        self.assertEqual(self.installs(), [])
        # A second apply has nothing to recover and does not restore again.
        self.data = host.load()
        self.install_receipt(TARGET)
        self.backend.resume_timer.reset_mock()
        self.assertEqual(self.apply(), 0)
        self.backend.resume_timer.assert_not_called()
        self.assertEqual(host.engine(host.load(), "local")["paused_timers"], [])

    def test_fleet_engine_change_skips_darwin_before_any_write(self):
        # F2: a factory_ref-driven fleet change skips a darwin host before writing or
        # syncing its engine target; the Linux host still moves. The remote-config sync is
        # never called and no engine target is recorded for the darwin host.
        self.data["hosts"] = {"ultron": {"ssh": "factory@ultron", "platform": "macos",
                                         "engine": {"sha": OLD, "ref": "main"}}}
        host.save(self.data)
        local_ctx = (self.run_cmd, self.backend, self.data["repo"])
        ultron_ctx = (self.run_cmd, self.backend, {})
        synced = mock.Mock()
        skipped: dict[str, bool] = {}
        with mock.patch.object(host, "sync_remote_config", synced):
            result = apply.reconcile_fleet_engine(
                self.data, None, {"local": local_ctx, "ultron": ultron_ctx}, skipped=skipped)
        self.assertEqual(result, "applied", self.output.getvalue())
        self.assertEqual(set(skipped), {"ultron"})
        self.assertTrue(self.installs())  # Linux host moved
        self.assertEqual(host.engine(self.data, "local")["sha"], TARGET)
        ultron = host.engine(self.data, "ultron")
        self.assertEqual(ultron["sha"], OLD)  # darwin unchanged
        self.assertNotIn("target", ultron)  # no engine target written/synced
        synced.assert_not_called()

    def test_recovery_interrupt_during_verification_still_restores_saved_timers(self):
        # F3: a signal during the receipt/verify checks must still restore the durable
        # saved set before honoring the interrupt (restore runs before the checks escape).
        self.data["defaults"].pop("factory_ref")
        self.data["defaults"]["engine"] = {"sha": OLD, "paused_timers": ["factory-widgets.timer"]}
        self.active.clear()  # widgets stopped by the crash, pending restore
        with mock.patch.object(apply, "_verify_engine", side_effect=SystemExit(143)):
            with self.assertRaises(SystemExit) as raised:
                self.apply()
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(self.active, {"factory-widgets.timer"})  # restored before honoring
        resumed = [call.args[0] for call in self.backend.resume_timer.call_args_list]
        self.assertEqual(resumed, ["factory-widgets.timer"])
        self.assertEqual(host.engine(host.load(), "local").get("paused_timers"), [])

    def test_restoration_recheck_remote_error_does_not_abandon_later_timers(self):
        # F4: host.RemoteHostError from the post-interrupt is_active recheck subclasses
        # RuntimeError, not OSError; it must not escape the sweep and abandon later timers.
        self.data["defaults"]["engine"] = {
            "sha": OLD, "paused_timers": ["factory-a.timer", "factory-b.timer"]}
        resumes, rechecked = [], []

        def resume(name, **kwargs):
            resumes.append(name)
            if len(resumes) == 1:
                raise SystemExit(143)  # the signal lands during the first resume
            return subprocess.CompletedProcess([], 0, "", "")

        def is_active(name, **kwargs):
            if not rechecked:
                rechecked.append(name)
                raise host.RemoteHostError("ultron", "ssh query failed")
            return "active"

        self.backend.resume_timer.side_effect = resume
        self.backend.is_active.side_effect = is_active
        with self.assertRaises(SystemExit) as raised:
            apply._restore_engine_timers(self.data, "local", self.run_cmd, self.backend,
                                         install_state.ApplyRun("", "", OLD))
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(set(resumes), {"factory-a.timer", "factory-b.timer"})  # none abandoned
        self.assertEqual(host.engine(host.load(), "local").get("paused_timers"), [])

    def test_linux_failure_still_shows_darwin_skip_row_and_partial(self):
        # F5: a propagated DistrictError on the Linux host must not swallow the per-host
        # darwin skip row (emitted when decided) or the partial summary.
        self.data["hosts"] = {"ultron": {"ssh": "factory@ultron", "platform": "macos",
                                         "engine": {"sha": OLD, "ref": "main"}}}
        self.data["repo"]["acme/ios"] = {"path": str(self.other_repo), "host": "ultron"}
        real_apply_host = apply._apply_host

        def failing(data, host_name, *args, **kwargs):
            if host_name == "local":
                raise host.DistrictError("drain timed out on local")
            return real_apply_host(data, host_name, *args, **kwargs)

        with mock.patch.object(host, "remote_runner"), \
                mock.patch.object(apply, "_apply_host", side_effect=failing):
            with self.assertRaises(host.DistrictError):
                self.apply(upgrade=TARGET)
        output = self.output.getvalue()
        self.assertRegex(output, r"acme/ios\b.*unsupported_platform")  # skip row before failure
        self.assertIn("partial:", output)
        self.assertIn("ultron", output)

if __name__ == "__main__":
    unittest.main()

