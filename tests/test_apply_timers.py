"""Issue #161: paused timers are visible without failing a clean apply."""
from __future__ import annotations

import argparse
import contextlib
import subprocess
import unittest
from unittest import mock

from district import apply, host, install_state, service
from tests import test_one_install as fixtures


class ApplyTimerTest(unittest.TestCase):
    @contextlib.contextmanager
    def case(self, active="inactive", enabled="enabled"):
        case = fixtures.OneInstallTest()
        case.setUp()
        try:
            case.data["defaults"].pop("factory_ref")
            case.backend.definitions_changed.return_value = False
            case.service_states["factory-widgets.timer"] = active
            missing = enabled == "not-found"
            timer = service.ServiceStatus("not-found" if missing else "loaded", active, enabled)
            case.backend.status.side_effect = lambda name, **_: (
                timer if name == "factory-widgets.timer"
                else service.ServiceStatus("not-found", "inactive", "not-found")
            )
            with mock.patch.object(install_state, "load_releases", return_value={}), \
                    mock.patch.object(install_state, "tool_versions", return_value={
                        "district": "0.1.7", "factory": "0.4.7"}):
                yield case
        finally:
            case.doCleanups()

    def test_apply_accepts_operator_paused_timer_including_rollback(self):
        for rollback in (False, True):
            with self.subTest(rollback=rollback), self.case() as case:
                argv = []
                if rollback:
                    case.compare_status = "behind"
                    argv = ["--upgrade", "main", "--allow-downgrade"]
                host.save(case.data)

                code = apply.main(argv)

                self.assertEqual(code, 0, case.output.getvalue())
                self.assertIn("timer paused", case.output.getvalue())
                self.assertEqual(case.backend.is_active("factory-widgets.timer"), "inactive")
                if rollback:
                    self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.TARGET)

    def test_apply_timer_health_and_engine_pause_precedence(self):
        cases = [
            ("inactive", "enabled", False, 0, "paused"),
            ("inactive", "disabled", False, 1, "inactive"),
            ("failed", "enabled", False, 1, "failed"),
            ("inactive", "not-found", False, 1, "inactive"),
            ("inactive", "disabled", True, 0, "paused"),
            ("failed", "enabled", True, 1, "failed"),
            ("inactive", "not-found", True, 1, "inactive"),
        ]
        for active, enabled, recorded, expected_code, expected_timer in cases:
            with self.subTest(active=active, enabled=enabled, recorded=recorded), self.case(active, enabled) as case:
                if recorded:
                    case.data["defaults"]["engine"]["paused_timers"] = ["factory-widgets.timer"]
                host.save(case.data)
                record = install_state.ApplyRun("", "", fixtures.OLD)

                rows, code, _ = apply._apply_host(
                    case.data, "local", case.data["repo"],
                    argparse.Namespace(upgrade=None, reset=None, allow_downgrade=False),
                    record, "OK", engine_result="unchanged", context=(case.run_cmd, case.backend),
                )

                self.assertEqual(code, expected_code, case.output.getvalue())
                self.assertEqual(rows[0]["timer"], expected_timer)
                self.assertEqual(rows[0]["fail"], [])
                self.assertIsNone(rows[0]["disabled"])

    def test_apply_paused_timer_does_not_hide_repo_failures(self):
        for failure in ("doctor", "disabled_at"):
            with self.subTest(failure=failure), self.case() as case:
                if failure == "disabled_at":
                    case.data["repo"]["acme/widgets"]["disabled_at"] = "2026-11-02T00:00:00Z"
                else:
                    original = case.run_cmd

                    def fail_doctor(argv, **kwargs):
                        result = original(argv, **kwargs)
                        if argv == ["factory", "doctor", "--json"]:
                            result.stdout = '{"rows":[{"status":"FAIL","label":"broken"}]}'
                        return result

                    case.enterContext(mock.patch.object(apply, "run", side_effect=fail_doctor))
                host.save(case.data)

                self.assertEqual(apply.main([]), 1, case.output.getvalue())
                self.assertIn("DISABLED since" if failure == "disabled_at" else "FAIL doctor", case.output.getvalue())

    def test_apply_launchd_hold_requires_recorded_engine_pause(self):
        cases = [
            (True, False, False, 1, "inactive", "disabled"),
            (True, True, False, 0, "paused", "disabled"),
            (False, False, False, 0, "active", "enabled"),
            (True, True, True, 1, "inactive", "not-found"),
        ]
        for held, recorded, missing, expected_code, expected_timer, expected_enabled in cases:
            with self.subTest(held=held, recorded=recorded, missing=missing), self.case() as case:
                timer = "factory-widgets.timer"

                def launchctl(argv, **kwargs):
                    if argv == ["launchctl", "print", "gui/501/factory-widgets"]:
                        return subprocess.CompletedProcess(
                            argv, 3 if missing else 0, "" if missing else "state = waiting\n", "")
                    self.assertEqual(argv, ["launchctl", "print-disabled", "gui/501"])
                    return subprocess.CompletedProcess(
                        argv, 0, f'gui/501 {{\n "factory-widgets" => {str(held).lower()}\n}}\n', "")

                backend = service.LaunchdBackend(launchctl, home=case.root / "home", uid=501)
                if not missing:
                    case.render_plist = True
                    name, body = case.rendered()
                    backend.agent_dir.mkdir(parents=True)
                    (backend.agent_dir / name).write_text(body)
                case.backend.status.side_effect = lambda name, **kwargs: (
                    backend.status(name, **kwargs) if name == timer
                    else service.ServiceStatus("not-found", "inactive", "not-found")
                )
                case.backend.is_active.side_effect = lambda name, **kwargs: (
                    backend.is_active(name, **kwargs) if name == timer else case.is_active(name, **kwargs)
                )
                if recorded:
                    case.data["defaults"]["engine"]["paused_timers"] = [timer]
                host.save(case.data)

                rows, code, _ = apply._apply_host(
                    case.data, "local", case.data["repo"],
                    argparse.Namespace(upgrade=None, reset=None, allow_downgrade=False),
                    install_state.ApplyRun("", "", fixtures.OLD), "OK",
                    engine_result="unchanged", context=(case.run_cmd, case.backend),
                )

                self.assertEqual(code, expected_code, case.output.getvalue())
                self.assertEqual(rows[0]["timer"], expected_timer)
                self.assertEqual(rows[0]["fail"], [])
                self.assertIsNone(rows[0]["disabled"])
                self.assertEqual(backend.status(timer).unit_file_state, expected_enabled)
                if expected_timer == "paused":
                    self.assertIn("timer paused", case.output.getvalue())
                else:
                    self.assertNotIn("timer paused", case.output.getvalue())
