"""Explicit drain flag lifecycle and failure handling."""
from __future__ import annotations

import contextlib
import json
import os
import signal
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from tests import test_one_install as fixtures
from tests.remote_transport import remote_command
from district import apply, drain, host, install_state


class DrainTest(unittest.TestCase):
    @contextlib.contextmanager
    def case(self):
        case = fixtures.OneInstallTest()
        case.setUp()
        try:
            case.backend.definitions_changed.return_value = False
            with mock.patch.object(apply, "smoke", return_value=(0, [])), \
                    mock.patch.object(apply.postcheck, "check", return_value=[]), \
                    mock.patch.object(install_state, "load_releases", return_value={}), \
                    mock.patch.object(install_state, "tool_versions", return_value={"district": "0.1.6", "factory": "0.4.7"}):
                yield case
        finally:
            case.doCleanups()

    def run_apply(self, case, argv=None):
        host.save(case.data)
        return apply.main(["--drain"] if argv is None else argv)

    def record(self):
        return install_state.read_record()["last_apply"]


    def test_drain_waits_for_running_pass_then_applies(self):
        with self.case() as case:
            flag = drain.path()
            case.busy = 3
            seen = []
            original = case.is_active

            def state(name, **kwargs):
                if name == "factory-widgets.service":
                    self.assertTrue(flag.is_file(), "drain must precede the first idle probe")
                    seen.append(json.loads(flag.read_text()))
                return original(name, **kwargs)

            case.backend.is_active.side_effect = state
            self.assertEqual(self.run_apply(case), 0)
            self.assertGreaterEqual(len(seen), 4)
            self.assertTrue(case.installs())
            self.assertFalse(flag.exists())
            self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.TARGET)
            entry = self.record()["drain"]
            for key in ("requested_at", "idle_at", "applied_at", "cleared_at"):
                self.assertIsInstance(entry[key], str)
            self.assertEqual(entry["folders_before"], entry["folders_after"])


    def test_drain_clears_flag_on_failure(self):
        with self.case() as case:
            with mock.patch.object(apply, "policy", side_effect=host.DistrictError("policy failure")):
                with self.assertRaisesRegex(host.DistrictError, "policy failure"):
                    self.run_apply(case)
            self.assertFalse(drain.path().exists())
            self.assertEqual(self.record()["exit_status"], 1)
            self.assertIsNotNone(self.record()["drain"]["cleared_at"])
            self.assertFalse(case.installs())

    def test_drain_clears_flag_on_rollback(self):
        with self.case() as case:
            case.bad_rev = True
            self.assertEqual(self.run_apply(case), 1)
            self.assertEqual(len(case.installs()), 2)
            self.assertEqual(host.engine(host.load(), "local")["sha"], fixtures.OLD)
            self.assertFalse(drain.path().exists())
            self.assertIsNone(self.record()["drain"]["applied_at"])

    def timeout_case(self, case):
        clock = [0.0]
        case.busy = 100
        case.sleep.side_effect = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        with mock.patch.object(apply.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(host.DistrictError, "drain timeout.*factory-widgets.service"):
                self.run_apply(case, ["--drain", "--drain-timeout", "2s"])

    def test_drain_clears_flag_on_timeout(self):
        with self.case() as case:
            self.timeout_case(case)
            self.assertFalse(drain.path().exists())
            self.assertIsNotNone(self.record()["drain"]["cleared_at"])
            self.assertIsNone(self.record()["drain"]["idle_at"])
            self.assertEqual(self.record()["drain"]["waited_s"], 2)


    def test_drain_clears_flag_on_signal(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum), self.case() as case:
                case.busy = 10
                case.sleep.side_effect = lambda _seconds: os.kill(os.getpid(), signum)
                with self.assertRaises(SystemExit) as caught:
                    self.run_apply(case)
                self.assertEqual(caught.exception.code, 128 + signum)
                self.assertFalse(drain.path().exists())
                self.assertEqual(self.record()["exit_status"], 128 + signum)
                self.assertIsNotNone(self.record()["drain"]["cleared_at"])
                self.assertFalse(case.installs())

    def test_new_pass_starting_during_drain_counts_busy(self):
        with self.case() as case:
            states = iter(("inactive", "active", "active", "inactive", "inactive"))
            observations = []
            original = case.is_active

            def state(name, **kwargs):
                if name == "factory-widgets.service":
                    value = next(states, "inactive")
                    observations.append(value)
                    if value == "active":
                        self.assertFalse(case.installs())
                    return value
                return original(name, **kwargs)

            case.backend.is_active.side_effect = state
            self.assertEqual(self.run_apply(case), 0)
            self.assertEqual(observations[:5], ["inactive", "active", "active", "inactive", "inactive"])
            self.assertTrue(case.installs())
            self.assertFalse(drain.path().exists())


    def test_operator_stopped_timer_stays_stopped_through_drain(self):
        with self.case() as case:
            case.active.clear()
            self.assertEqual(self.run_apply(case), 1)
            self.assertEqual(case.backend.is_active("factory-widgets.timer"), "inactive")
            self.assertTrue(case.installs())

    def test_handland_units_do_not_count_busy_during_drain(self):
        with self.case() as case:
            handland = "handland-137.service"
            case.active.add(handland)
            self.assertEqual(self.run_apply(case), 0)
            self.assertTrue(case.installs())
            self.assertNotIn(("state", handland), case.events)

    def test_drain_waits_for_discovered_active_service(self):
        with self.case() as case:
            worker = "factory-unregistered-worker.service"
            case.backend.factory_units.return_value = {worker}
            states = iter(("active", "inactive", "inactive"))
            observed = []
            original = case.is_active

            def state(name, **kwargs):
                if name == worker:
                    value = next(states, "inactive")
                    observed.append(value)
                    if value == "active":
                        self.assertTrue(drain.path().exists())
                        self.assertFalse(case.installs())
                    return value
                return original(name, **kwargs)

            case.backend.is_active.side_effect = state
            self.assertEqual(self.run_apply(case), 0)
            self.assertEqual(observed[:3], ["active", "inactive", "inactive"])
            self.assertTrue(case.installs())
            self.assertGreaterEqual(case.sleep.call_count, 2)

    def test_explicit_drain_skips_current_engines_on_local_and_remote_hosts(self):
        for placement in ("local", "remote", "both"):
            with self.subTest(placement=placement), self.case() as case:
                case.install_receipt(fixtures.TARGET)
                case.data["defaults"]["engine"]["sha"] = fixtures.TARGET
                case.busy = 3
                if placement != "local":
                    case.data["hosts"] = {"remote": {
                        "ssh": "unused", "platform": "macos",
                        "engine": {"sha": fixtures.TARGET, "ref": "main"},
                    }}
                    if placement == "remote":
                        case.data["repo"]["acme/widgets"]["host"] = "remote"
                    else:
                        remote_repo = case.root / "remote"
                        remote_repo.mkdir()
                        case.data["repo"]["acme/remote"] = {
                            "path": str(remote_repo), "host": "remote",
                        }
                runner, _, _ = case.timeout_remote("none")
                with mock.patch.object(host, "remote_runner", return_value=runner):
                    self.assertEqual(self.run_apply(case), 0)
                self.assertNotIn("drain", self.record())
                self.assertNotIn("drains", self.record())
                apply.postcheck.check.assert_not_called()
                self.assertFalse(case.installs())
                self.assertFalse(drain.path().exists())

    def test_postcheck_failure_does_not_mark_drain_applied(self):
        with self.case() as case:
            with mock.patch.object(apply.postcheck, "check", return_value=["factory watch --dry-run: no factory repo checkout"]):
                self.assertEqual(self.run_apply(case), 1)
            self.assertFalse(drain.path().exists())
            self.assertIsNone(self.record()["drain"]["applied_at"])
            self.assertIn("no factory repo checkout", case.output.getvalue())

    def test_remote_drain_writes_and_removes_flag_over_ssh(self):
        with self.case() as case:
            case.data["hosts"] = {
                "remote": {"ssh": "unused", "platform": "macos", "engine": {"sha": fixtures.OLD, "ref": "main"}}}
            case.data["repo"]["acme/widgets"]["host"] = "remote"
            case.backend.configuration.return_value = {"platform": "macos", "executable": "launchctl"}
            case.busy = 2
            runner, _, commands = case.timeout_remote("none")
            original = runner.transport

            def transport(argv, **kwargs):
                parts = shlex.split(remote_command(argv))
                if parts[:2] == ["python3", "-c"] and parts[2] == host.REMOTE_DEADLINE:
                    parts = shlex.split(parts[-1])
                if parts[:2] == ["python3", "-c"] and "XDG_STATE_HOME" in parts[2]:
                    return subprocess.run([sys.executable, *parts[1:]], capture_output=True, text=True,
                                          input=kwargs.get("input"), timeout=kwargs.get("timeout"))
                return original(argv, **kwargs)

            runner.transport = transport
            host.save(case.data)
            with mock.patch.object(host, "remote_runner", return_value=runner):
                self.assertEqual(apply.main(["--drain"]), 0)
            self.assertTrue(case.installs())
            self.assertFalse(drain.path().exists())
            self.assertEqual(host.engine(host.load(), "remote")["sha"], fixtures.TARGET)
            self.assertIsNotNone(self.record()["drains"]["remote"]["cleared_at"])
            self.assertTrue(any("os.replace" in command[2] and command[-1].endswith("/drain")
                                for command in commands if command[:2] == ["python3", "-c"]))
            self.assertTrue(any("unlink" in command[2] and command[-1].endswith("/drain")
                                for command in commands if command[:2] == ["python3", "-c"]))

    def test_failed_flag_write_does_not_register_a_live_drain(self):
        with self.case() as case:
            record = install_state.ApplyRun("", "", fixtures.OLD)
            with contextlib.ExitStack() as stack:
                record.drain_stack = stack
                with mock.patch.object(drain, "request", side_effect=OSError("state directory denied")):
                    self.assertEqual(case.reconcile(record=record), "failed")
            self.assertEqual(record.drains, {})
            self.assertFalse(drain.path().exists())
            self.assertFalse(case.installs())

    def test_other_host_drain_does_not_suspend_local_failure_cap(self):
        with self.case() as case:
            record = install_state.ApplyRun("", "", fixtures.OLD, drains={"remote": {}})
            original = case.run_cmd

            def runner(argv, **kwargs):
                if argv[1:] == ["dashboard", "--json"]:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "version": "0.4.7", "dispatcher": {"consecutive_failures": 10}}), "")
                return original(argv, **kwargs)

            host.save(case.data)
            row = apply.repo_pass("acme/widgets", case.data["repo"]["acme/widgets"],
                                  case.data, record, backend=case.backend, run_cmd=runner)
            self.assertIsNotNone(row["disabled"])
            case.backend.enable.assert_called_once()
            self.assertEqual(case.backend.enable.call_args.args, ("factory-widgets.timer", False))

    def test_incomplete_cleanup_does_not_mark_drain_applied(self):
        with self.case() as case:
            result = {"folders_before": 2, "folders_after": 2,
                      "scan_errors": ["unreadable unit drop-in"], "remove_errors": []}
            with mock.patch.object(apply, "cleanup", return_value=result):
                self.assertEqual(self.run_apply(case), 1)
            self.assertIsNone(self.record()["drain"]["applied_at"])
            self.assertFalse(drain.path().exists())
            self.assertIn("legacy engine cleanup incomplete", case.output.getvalue())

    def test_drained_recovery_completes_pending_install(self):
        with self.case() as case:
            case.install_receipt(fixtures.TARGET)
            case.data["defaults"]["engine"] = {
                "sha": fixtures.OLD, "ref": "main",
                "install_pending": {"target": fixtures.TARGET, "ref": "v0.4.7"}}
            self.assertEqual(self.run_apply(case), 0, case.output.getvalue())
            engine = host.engine(host.load(), "local")
            self.assertEqual(engine["sha"], fixtures.TARGET)
            self.assertFalse(engine.get("install_pending"))
            self.assertEqual(case.installs(), [])  # the TARGET receipt is verified, not reinstalled
            self.assertFalse(drain.path().exists())

if __name__ == "__main__":
    unittest.main()
