"""Issue #170: engine applies pause Factory timers, drain, verify, and restore."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import unittest
from unittest import mock

from district import apply, host, install_state, service
from tests import test_one_install as fixtures


class UpgradePauseTest(unittest.TestCase):
    timer = "factory-widgets.timer"
    worker = "factory-widgets.service"

    @contextlib.contextmanager
    def case(self, *, active: bool = True, changed: bool = False):
        case = fixtures.OneInstallTest()
        case.setUp()
        case.lifecycle = []
        case.active = {self.timer} if active else set()
        case.backend.factory_units.return_value = {self.worker, self.timer}
        case.backend.definitions_changed.return_value = changed
        case.backend.configuration.return_value = {
            "platform": "linux",
            "executable": "fake-systemctl",
            "unit_dir": str(host.unit_dir()),
        }

        def pause(name, **_kwargs):
            self.assertTrue(name.endswith(".timer"), name)
            self.assertIn(name, case.active, f"inactive timer paused: {name}")
            case.lifecycle.append(("pause", name))
            case.active.discard(name)
            return subprocess.CompletedProcess([], 0, "", "")

        def resume(name, **_kwargs):
            self.assertTrue(name.endswith(".timer"), name)
            self.assertNotIn(name, case.active, f"active timer resumed: {name}")
            case.lifecycle.append(("resume", name))
            case.active.add(name)
            return subprocess.CompletedProcess([], 0, "", "")

        def direct_mutation(name, **_kwargs):
            self.fail(f"apply directly started/stopped {name}; use the timer pause lifecycle")

        def enable_now(name, *_args, **_kwargs):
            self.fail(f"apply enabled {name} with --now during the paused window")

        def status(name, **_kwargs):
            if name.endswith(".timer"):
                state = "active" if name in case.active else "inactive"
                return service.ServiceStatus("loaded", state, "enabled")
            state = case.service_states.get(name, "inactive")
            return service.ServiceStatus(
                "loaded" if state != "inactive" else "not-found", state, "enabled", 42,
                (str(case.cli), "dispatch") if state != "inactive" else None,
            )
        case.backend.pause_timer.side_effect = pause
        case.backend.resume_timer.side_effect = resume
        case.backend.start.side_effect = direct_mutation
        case.backend.stop.side_effect = direct_mutation
        case.backend.enable.side_effect = enable_now
        case.backend.status.side_effect = status
        cleanup = {"folders_before": 1, "folders_after": 1,
                   "scan_errors": [], "remove_errors": []}
        try:
            with mock.patch.object(apply, "smoke", return_value=(0, [])), \
                    mock.patch.object(apply, "cleanup", return_value=cleanup), \
                    mock.patch.object(apply.postcheck, "check", return_value=[]), \
                    mock.patch.object(install_state, "load_releases", return_value={}), \
                    mock.patch.object(install_state, "tool_versions", return_value={
                        "district": "0.1.8", "factory": "0.4.7",
                    }):
                yield case
        finally:
            case.doCleanups()

    @staticmethod
    def args(*, upgrade: str = fixtures.TARGET, timeout: float = 5) -> argparse.Namespace:
        return argparse.Namespace(
            slug=None,
            reset=None,
            upgrade=upgrade,
            allow_downgrade=False,
            drain=False,
            drain_timeout=timeout,
        )

    def run_apply(self, case, *, upgrade: str = fixtures.TARGET, timeout: float = 5,
                  record: install_state.ApplyRun | None = None) -> tuple[int, install_state.ApplyRun]:
        host.save(case.data)
        record = record or install_state.ApplyRun("", "", fixtures.OLD)
        return apply._run(self.args(upgrade=upgrade, timeout=timeout), record), record

    def test_default_drain_timeout_is_90_minutes_recorded_as_seconds(self):
        with self.case() as case:
            record = install_state.ApplyRun("", "", fixtures.OLD)
            host.save(case.data)
            with mock.patch.object(install_state.ApplyRun, "start", return_value=record):
                self.assertEqual(apply.main(["--upgrade", fixtures.TARGET]), 0,
                                 case.output.getvalue())
            self.assertEqual(record.drain_timeout, 90 * 60)

    def test_busy_service_drains_then_upgrade_restores_exact_active_timers(self):
        with self.case() as case:
            dormant = "factory-dormant.timer"
            case.backend.factory_units.return_value = {self.worker, self.timer, dormant}
            clock = [0.0]
            observations = []
            states = iter(("active", "active", "inactive", "inactive"))
            original_active = case.is_active

            def active(name, **kwargs):
                if name == self.worker:
                    self.assertNotIn(self.timer, case.active,
                                     "the first busy probe ran before timers were paused")
                    state = next(states, "inactive")
                    observations.append(state)
                    return state
                return original_active(name, **kwargs)

            installed_while_active = []

            def runner(argv, **kwargs):
                if argv[:3] == ["uv", "tool", "install"]:
                    installed_while_active.append(self.timer in case.active)
                return case.run_cmd(argv, **kwargs)

            case.backend.is_active.side_effect = active
            case.sleep.side_effect = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
            with mock.patch.object(apply, "run", side_effect=runner), \
                    mock.patch.object(apply.time, "monotonic", side_effect=lambda: clock[0]):
                code, record = self.run_apply(case)

            self.assertEqual(code, 0, case.output.getvalue())
            self.assertGreaterEqual(len(observations), 3)
            self.assertEqual(installed_while_active, [False])
            self.assertEqual(case.lifecycle, [("pause", self.timer), ("resume", self.timer)])
            self.assertEqual(case.active, {self.timer})
            self.assertEqual(record.disabled, {self.timer})
            self.assertEqual(record.reenabled, {self.timer})
            self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.TARGET)
            case.backend.start.assert_not_called()
            case.backend.stop.assert_not_called()

    def test_timeout_restores_timer_without_install_and_reports_blocking_ticket(self):
        with self.case() as case:
            clock = [0.0]
            record = install_state.ApplyRun("", "", fixtures.OLD)
            case.service_states[self.worker] = "active"
            case.sleep.side_effect = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

            def runner(argv, **kwargs):
                if argv[1:] == ["dashboard", "--json"]:
                    case.calls.append((list(argv), kwargs))
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "version": "0.4.7",
                        "dispatcher": {"consecutive_failures": 0},
                        "tickets": [{"number": 170, "stage": "working"}],
                    }), "")
                return case.run_cmd(argv, **kwargs)

            host.save(case.data)
            with mock.patch.object(apply, "run", side_effect=runner), \
                    mock.patch.object(apply.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(install_state.ApplyRun, "start", return_value=record):
                with self.assertRaises(host.DistrictError) as raised:
                    apply.main(["--upgrade", fixtures.TARGET, "--drain-timeout", "2s"])

            diagnostic = f"{raised.exception}\n{case.output.getvalue()}"
            self.assertIn(self.worker, diagnostic)
            self.assertIn("170", diagnostic)
            self.assertIn("working", diagnostic)
            self.assertIn("timeout", diagnostic.lower())
            self.assertEqual(record.drain_timeout, 2)
            self.assertEqual(case.installs(), [])
            self.assertEqual(case.lifecycle, [("pause", self.timer), ("resume", self.timer)])
            self.assertEqual(case.active, {self.timer})
            self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.OLD)

    def test_apply_exception_restores_timer(self):
        with self.case() as case:
            def explode(*_args, **_kwargs):
                self.assertNotIn(self.timer, case.active,
                                 "repository apply escaped the shared paused window")
                raise RuntimeError("render exploded")

            with mock.patch.object(apply, "plan_apply", side_effect=explode):
                with self.assertRaisesRegex(RuntimeError, "render exploded"):
                    self.run_apply(case)

            self.assertEqual(case.lifecycle, [("pause", self.timer), ("resume", self.timer)])
            self.assertEqual(case.active, {self.timer})
            case.backend.start.assert_not_called()
            case.backend.stop.assert_not_called()

    def test_bad_receipt_or_version_result_rolls_back_and_verifies_rollback(self):
        for failure in ("bad-receipt", "nonzero", "dirty", "wrong-version"):
            with self.subTest(failure=failure), self.case() as case:
                rollback_installed = [False]
                rollback_verified = []
                installs_paused = []

                def runner(argv, **kwargs):
                    if argv[:2] == ["git", "ls-remote"] and any(
                            arg.startswith("refs/tags/v0.4.7") for arg in argv):
                        case.calls.append((list(argv), kwargs))
                        return subprocess.CompletedProcess(
                            argv, 0, f"{fixtures.TARGET}\trefs/tags/v0.4.7\n", "")
                    if argv[:3] == ["uv", "tool", "install"]:
                        installs_paused.append(self.timer not in case.active)
                        source = argv[argv.index("--from") + 1]
                        if f"@{fixtures.OLD}" in source:
                            rollback_installed[0] = True
                        result = case.run_cmd(argv, **kwargs)
                        if f"@{fixtures.TARGET}" in source and failure == "bad-receipt":
                            case.install_receipt(fixtures.OLD)
                        return result
                    if argv == [str(case.cli), "--version"]:
                        receipt = (case.tool / "uv-receipt.toml").read_text()
                        revision = fixtures.TARGET if fixtures.TARGET in receipt else fixtures.OLD
                        if self.timer not in case.active and rollback_installed[0]:
                            rollback_verified.append(revision)
                        result = case.run_cmd(argv, **kwargs)
                        if revision == fixtures.TARGET and failure == "nonzero":
                            return subprocess.CompletedProcess(argv, 9, "", "version command failed")
                        if revision == fixtures.TARGET and failure == "dirty":
                            return subprocess.CompletedProcess(argv, 0, "factory 0.4.7+dirty\n", "")
                        if revision == fixtures.TARGET and failure == "wrong-version":
                            return subprocess.CompletedProcess(argv, 0, "factory 0.4.6\n", "")
                        return result
                    return case.run_cmd(argv, **kwargs)

                with mock.patch.object(apply, "run", side_effect=runner):
                    code, _ = self.run_apply(case, upgrade="v0.4.7")

                sources = [argv[argv.index("--from") + 1] for argv in case.installs()]
                self.assertEqual(code, 1, case.output.getvalue())
                self.assertEqual(sources, [
                    f"git+{fixtures.REPO}@{fixtures.TARGET}",
                    f"git+{fixtures.REPO}@{fixtures.OLD}",
                ])
                self.assertIn(fixtures.OLD, rollback_verified, "rollback was not verified before resume")
                self.assertTrue(
                    installs_paused and all(installs_paused),
                    "target or rollback install escaped paused window",
                )
                self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.OLD)
                self.assertEqual(case.active, {self.timer})
                self.assertIn("rolled back", case.output.getvalue().lower())

    def test_inactive_timer_is_never_started(self):
        with self.case(active=False) as case:
            code, record = self.run_apply(case)

            self.assertEqual(code, 0, case.output.getvalue())
            self.assertEqual(case.lifecycle, [])
            self.assertEqual(case.active, set())
            self.assertEqual(record.disabled, set())
            self.assertEqual(record.reenabled, set())
            case.backend.pause_timer.assert_not_called()
            case.backend.resume_timer.assert_not_called()

    def test_full_run_rerenders_and_reloads_without_starting_stopping_or_killing_service(self):
        with self.case(changed=True) as case:
            target_installed = [False]
            rendered_paused = []
            reload_paused = []

            def runner(argv, **kwargs):
                if (argv[:3] == ["uv", "tool", "install"]
                        and any(f"@{fixtures.TARGET}" in arg for arg in argv)):
                    target_installed[0] = True
                if argv == [str(case.cli), "install", "--print"] and target_installed[0]:
                    rendered_paused.append(self.timer not in case.active)
                if argv and argv[-1] == "daemon-reload":
                    case.calls.append((list(argv), kwargs))
                    reload_paused.append(self.timer not in case.active)
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return case.run_cmd(argv, **kwargs)

            with mock.patch.object(apply, "run", side_effect=runner):
                code, _ = self.run_apply(case)

            self.assertEqual(code, 0, case.output.getvalue())
            self.assertEqual(rendered_paused, [True])
            self.assertEqual(reload_paused, [True])
            self.assertTrue((host.unit_dir() / self.worker).is_file())
            self.assertFalse(any(argv == [str(case.cli), "install"] for argv, _ in case.calls),
                             "public factory install starts one-shot services")
            self.assertFalse(any("--now" in argv for argv, _ in case.calls))
            self.assertFalse(any("kill" in argv for argv, _ in case.calls))
            self.assertEqual(case.lifecycle, [("pause", self.timer), ("resume", self.timer)])
            case.backend.start.assert_not_called()
            case.backend.stop.assert_not_called()

    def test_sigterm_and_sigint_restore_timer(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum), self.case() as case:
                case.service_states[self.worker] = "active"
                case.sleep.side_effect = lambda _seconds: os.kill(os.getpid(), signum)
                host.save(case.data)

                with self.assertRaises(SystemExit) as raised:
                    apply.main(["--upgrade", fixtures.TARGET])

                self.assertEqual(raised.exception.code, 128 + signum)
                self.assertEqual(case.lifecycle, [("pause", self.timer), ("resume", self.timer)])
                self.assertEqual(case.active, {self.timer})
                self.assertEqual(case.installs(), [])

    def test_prerelease_upgrade_is_verified_and_stays_current(self):
        for tag, normalized in (("v0.5.1-rc.1", "0.5.1rc1"),
                                ("v0.5.1-dev.2", "0.5.1.dev2")):
            with self.subTest(tag=tag), self.case() as case:
                def runner(argv, **kwargs):
                    if argv[:2] == ["git", "ls-remote"]:
                        case.calls.append((list(argv), kwargs))
                        return subprocess.CompletedProcess(
                            argv, 0, f"{fixtures.TARGET}\trefs/tags/{tag}\n", "")
                    result = case.run_cmd(argv, **kwargs)
                    if argv[:3] == ["uv", "tool", "install"]:
                        metadata, = case.tool.glob("lib/python*/site-packages/factory-*.dist-info/METADATA")
                        metadata.write_text(f"Metadata-Version: 2.4\nName: factory\nVersion: {normalized}\n")
                    if argv == [str(case.cli), "--version"] and case.installs():
                        return subprocess.CompletedProcess(argv, 0, f"factory {normalized}\n", "")
                    return result

                with mock.patch.object(apply, "run", side_effect=runner):
                    code, _ = self.run_apply(case, upgrade=tag)
                    self.assertEqual(code, 0, case.output.getvalue())
                    self.assertTrue(apply._engine_current(
                        fixtures.TARGET, case.tool, case.cli, runner, ref=tag))
                    code, _ = self.run_apply(case, upgrade=tag)

                self.assertEqual(code, 0, case.output.getvalue())
                self.assertEqual(len(case.installs()), 1, case.output.getvalue())
                self.assertEqual(case.lifecycle, [("pause", self.timer), ("resume", self.timer)])
                self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.TARGET)

    def test_paused_render_enables_installable_units_without_activating_them(self):
        with self.case(changed=True) as case:
            case.data["repo"]["acme/other"] = {"path": str(case.other_repo)}
            dormant = "factory-widgets-triage-probe.timer"
            case.backend.factory_units.return_value = {self.worker, self.timer, dormant}
            enabled = set()
            enable_paused = []

            def runner(argv, **kwargs):
                if argv[:3] == ["fake-systemctl", "--user", "enable"]:
                    case.calls.append((list(argv), kwargs))
                    self.assertNotIn("--now", argv)
                    enable_paused.append(self.timer not in case.active)
                    enabled.add(argv[-1])
                    return subprocess.CompletedProcess(argv, 0, "", "")
                if argv == ["fake-systemctl", "--user", "daemon-reload"]:
                    case.calls.append((list(argv), kwargs))
                    return subprocess.CompletedProcess(argv, 0, "", "")
                result = case.run_cmd(argv, **kwargs)
                if argv == [str(case.cli), "install", "--print"]:
                    unit = "factory-other" if kwargs.get("cwd") == case.other_repo else "factory-widgets"
                    for suffix in (".timer", "-eval.timer", "-triage-probe.timer", "-dashboard.service"):
                        kind = case.rendered(kwargs.get("cwd"))[1] if suffix.endswith(".service") else (
                            "[Timer]\nOnUnitActiveSec=10min")
                        result.stdout += f"\n# {unit}{suffix}\n{kind}\n[Install]\nWantedBy=default.target\n"
                return result

            enabling = service.SystemdUserBackend(
                runner, executable="fake-systemctl", unit_dir=host.unit_dir())
            case.backend.enable.side_effect = enabling.enable
            with mock.patch.object(apply, "run", side_effect=runner):
                code, _ = self.run_apply(case)

            self.assertEqual(code, 0, case.output.getvalue())
            self.assertEqual(enabled, {
                f"factory-{slug}{suffix}" for slug in ("widgets", "other")
                for suffix in (".timer", "-eval.timer", "-triage-probe.timer", "-dashboard.service")
            })
            self.assertTrue(all(enable_paused), "unit enablement escaped the paused window")
            self.assertEqual(case.active, {self.timer})
            self.assertEqual(case.lifecycle, [("pause", self.timer), ("resume", self.timer)])
            case.backend.start.assert_not_called()
            case.backend.stop.assert_not_called()
            self.assertFalse(any("--now" in argv or "kill" in argv for argv, _ in case.calls))

    def test_commit_target_version_must_match_distribution_metadata(self):
        with self.case() as case:
            def runner(argv, **kwargs):
                if argv == [str(case.cli), "--version"] and len(case.installs()) == 1:
                    return subprocess.CompletedProcess(argv, 0, "0.4.6\n", "")
                return case.run_cmd(argv, **kwargs)

            with mock.patch.object(apply, "run", side_effect=runner):
                code, _ = self.run_apply(case)
            self.assertEqual(code, 1, case.output.getvalue())
            self.assertEqual(len(case.installs()), 2)
            self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.OLD)
            self.assertEqual(case.active, {self.timer})
            self.assertIn("rolled back", case.output.getvalue())



if __name__ == "__main__":
    unittest.main()
