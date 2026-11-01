"""Issue #136: one verified Factory uv tool per host, without host side effects."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import apply, host, install_state, service

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
        self.data = {
            "defaults": {"factory_repo": REPO, "factory_ref": "main",
                         "factory_source": str(self.checkout),
                         "engine": {"sha": OLD, "ref": "main"}},
            "repo": {"acme/widgets": {"path": str(self.repo)}},
        }
        self.calls = []
        self.events = []
        self.active = {"factory-widgets.timer"}
        self.busy = 0
        self.process_busy = 0
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
        self.sleep = self.enterContext(mock.patch.object(apply.time, "sleep"))
        self.install_receipt(OLD)
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def install_receipt(self, sha):
        self.tool.mkdir(parents=True, exist_ok=True)
        (self.tool / "bin").mkdir(exist_ok=True)
        (self.tool / "bin" / "factory").touch()
        (self.tool / "bin" / "python").touch()
        self.cli.parent.mkdir(parents=True, exist_ok=True)
        self.cli.unlink(missing_ok=True)
        self.cli.symlink_to(self.tool / "bin" / "factory")
        (self.tool / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{name = "factory", git = "{REPO}?rev={sha}"}}]\n'
            f'entrypoints = [{{name = "factory", install-path = "{self.cli}", from = "factory"}}]\n')

    def is_active(self, name, **kwargs):
        self.events.append(("state", name))
        if name.endswith(".service") and "dashboard" not in name and self.busy:
            self.busy -= 1
            return "activating"
        return "active" if name in self.active else "inactive"

    def enable(self, name, enabled=True, **kwargs):
        self.events.append(("enable" if enabled else "pause", name))
        self.active.add(name) if enabled else self.active.discard(name)
        return subprocess.CompletedProcess([], 0, "", "")

    def rendered(self):
        if self.render_plist:
            name = "factory-widgets.plist"
            body = plistlib.dumps({"Label": "factory-widgets", "ProgramArguments": [
                self.render_executable, "-P", "-m", "factory", "dispatch"]}).decode()
        else:
            name = "factory-widgets.service"
            body = f"[Service]\nExecStart={self.render_executable} -P -m factory dispatch\n"
        return name, body

    def run_cmd(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        out, code = "", 0
        if argv[:4] == ["git", "remote", "get-url", "origin"]:
            out = REPO + "\n"
        elif argv[:2] == ["git", "ls-remote"]:
            out = f"{self.tip}\trefs/heads/main\n"
        elif argv[:2] == ["gh", "api"]:
            endpoint = next((arg for arg in argv[2:] if arg.startswith("repos/")), "")
            if "check-runs" in endpoint:
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
                name, body = self.rendered()
                out = f"# {name}\n{body}\n"
            elif argv[1:] == ["install"]:
                name, body = self.rendered()
                directory = (Path.home() / "Library/LaunchAgents") if name.endswith(".plist") else host.unit_dir()
                directory.mkdir(parents=True, exist_ok=True)
                (directory / name).write_text(body)
                self.enable("factory-widgets.timer")
            elif argv[1:] == ["doctor", "--json"]:
                out = json.dumps({"rows": []})
            elif argv[1:] == ["dashboard", "--json"]:
                out = json.dumps({"version": "0.4.7", "dispatcher": {"consecutive_failures": 0}})
            elif argv[1:] != ["init", "--labels-only"]:
                self.fail(f"unexpected Factory command: {argv}")
        elif argv[0] == "ps":
            self.events.append(("processes", "scan"))
            if self.process_busy:
                self.process_busy -= 1
                out = f"123 {self.tool}/bin/python -m factory dispatch --ticket 12\n"
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

    def test_busy_engine_pauses_then_waits_for_services_and_processes(self):
        self.data["repo"]["acme/disabled"] = {"path": str(self.root / "disabled"), "disabled_at": "hold"}
        self.busy = 2
        self.process_busy = 1
        self.assertEqual(self.reconcile(), "applied")
        self.assertEqual(self.active, {"factory-widgets.timer"})
        self.assertEqual([e for e in self.events if e[0] in ("pause", "enable")], [
            ("pause", "factory-widgets.timer"), ("enable", "factory-widgets.timer")])
        pause = self.events.index(("pause", "factory-widgets.timer"))
        install = next(i for i, e in enumerate(self.events) if e[0] == "install")
        self.assertTrue(any(e == ("state", "factory-widgets.service") for e in self.events[pause + 1:install]))
        self.assertIn(("processes", "scan"), self.events[pause + 1:install])
        self.assertGreater(self.sleep.call_count, 0)

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
        self.assertEqual(self.apply(upgrade="HEAD"), 0)
        self.assertEqual(host.load()["defaults"]["engine"]["sha"], TARGET)
        self.assertFalse(any(kwargs.get("cwd") == self.checkout for _, kwargs in self.calls))
        self.assertFalse(any(argv[:2] in (["git", "archive"], ["git", "rev-parse"]) for argv, _ in self.calls))

    def test_idle_timeout_restores_timers_without_installing(self):
        self.data["defaults"]["engine_idle_timeout"] = "0s"
        self.busy = 5
        record = install_state.ApplyRun("", "", OLD)
        self.assertEqual(self.reconcile(record=record), "deferred")
        self.assertEqual(self.installs(), [])
        self.assertEqual(self.active, {"factory-widgets.timer"})

    def test_downgrade_refusal_names_both_shas_and_override_installs(self):
        self.compare_status = "behind"
        with self.assertRaises(host.DistrictError) as refused:
            self.reconcile()
        self.assertIn(OLD, str(refused.exception))
        self.assertIn(TARGET, str(refused.exception))
        self.assertEqual(self.installs(), [])
        self.backend.pause_timer.assert_not_called()
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

    def test_busy_service_with_inactive_timer_still_defers_without_install(self):
        self.active.clear()
        self.busy = 3
        self.data["defaults"]["engine_idle_timeout"] = "0s"
        self.assertEqual(self.reconcile(), "deferred")
        self.assertEqual(self.installs(), [])
        self.backend.pause_timer.assert_not_called()
        self.backend.resume_timer.assert_not_called()

    def test_restore_error_keeps_recovery_state_and_restores_other_timers(self):
        timers = ["factory-widgets.timer", "factory-other.timer"]
        self.data["defaults"]["engine"]["paused_timers"] = timers
        self.active.clear()
        def resume(timer):
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
        self.rendered = lambda: ("factory-widgets.timer", "[Timer]\nOnUnitActiveSec=10min\n")
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
        self.backend.start.assert_called_once_with("factory-widgets-dashboard.service", restart=True, check=False)
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

    def test_failed_install_and_rollback_with_missing_cli_fail_without_path_fallback(self):
        def broken_install(argv, **kwargs):
            if argv[:3] == ["uv", "tool", "install"]:
                self.calls.append((list(argv), kwargs))
                self.cli.unlink(missing_ok=True)
                return subprocess.CompletedProcess(argv, 1, "", "install and rollback unavailable")
            if argv[0] in ("factory", str(self.cli)):
                raise FileNotFoundError(str(self.cli))
            return self.run_cmd(argv, **kwargs)
        with mock.patch.object(apply, "run", side_effect=broken_install):
            self.assertEqual(self.apply(upgrade="HEAD"), 1)
        self.assertEqual(self.active, {"factory-widgets.timer"})
        self.assertEqual(len(self.installs()), 2)
        self.assertIn("acme/widgets", self.output.getvalue())
        self.assertIn(OLD, self.output.getvalue())
        self.assertIn(TARGET, self.output.getvalue())

    def test_idle_window_bounds_every_operation_by_one_deadline(self):
        for exhausted_at in ("discovery", "timer", "pause", "service", "process"):
            with self.subTest(exhausted_at=exhausted_at):
                self.data["defaults"]["engine_idle_timeout"] = "5s"
                self.data["defaults"]["engine"].pop("paused_timers", None)
                clock = [0.0]
                observed = []

                def operation(label, result, **kwargs):
                    remaining = 5 - clock[0]
                    self.assertGreater(remaining, 0, (label, observed))
                    self.assertGreater(kwargs.get("timeout", 0), 0, (label, kwargs))
                    self.assertLessEqual(kwargs["timeout"], remaining, (label, kwargs))
                    observed.append(label)
                    if label == exhausted_at:
                        clock[0] = 5
                    return result

                def active(name, **kwargs):
                    return operation("timer" if name.endswith(".timer") else "service",
                                     "active" if name.endswith(".timer") else "inactive", **kwargs)

                def processes(argv, **kwargs):
                    self.assertEqual(argv[0], "ps")
                    return operation("process", subprocess.CompletedProcess(argv, 0, "", ""), **kwargs)

                self.backend.factory_units.side_effect = lambda **kw: operation("discovery", set(), **kw)
                self.backend.is_active.side_effect = active
                self.backend.pause_timer.side_effect = lambda name, **kw: operation("pause", None, **kw)
                with mock.patch.object(apply.time, "monotonic", side_effect=lambda: clock[0]):
                    with apply._engine_window(self.data, "local", processes, self.backend, self.data["repo"], None) as ready:
                        self.assertFalse(ready)
                self.assertEqual(observed[-1], exhausted_at)
                self.assertEqual(host.engine(self.data, "local").get("paused_timers"), [])

    def test_zero_idle_budget_issues_no_discovery_probe_or_pause(self):
        self.data["defaults"]["engine_idle_timeout"] = "0s"
        self.assertEqual(self.reconcile(), "deferred")
        self.backend.factory_units.assert_not_called()
        self.backend.is_active.assert_not_called()
        self.backend.pause_timer.assert_not_called()
        self.assertFalse(any(argv[0] == "ps" for argv, _ in self.calls))

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
                    self.data["repo"]["acme/other"] = {"path": str(self.repo)}

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


if __name__ == "__main__":
    unittest.main()
