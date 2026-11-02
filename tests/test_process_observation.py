"""Owning-host process format boundaries, with no live process inspection."""
import os
import shlex
import subprocess
import unittest
from unittest import mock

from district import apply, host, install_state
import test_one_install as fixtures


class ProcessObservationTest(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.OneInstallTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def observation_case(self, *, remote, platform="macos", mode="localized",
                         phase="before", command="dispatch", rollback=False):
        case = self.case
        case.install_receipt(fixtures.OLD)
        case.calls.clear()
        case.events.clear()
        case.output.seek(0)
        case.output.truncate()
        case.active = {"factory-widgets.timer", "factory-idle.timer"}
        case.data["defaults"]["engine"] = {"sha": fixtures.OLD, "ref": "main"}
        case.data["defaults"]["engine_idle_timeout"] = "5s"
        case.backend.factory_units.return_value = {"factory-idle.timer", "factory-stopped.timer"}
        case.backend.configuration.return_value = {"platform": platform, "uid": 501}
        name = "remote" if remote else "local"
        runner = None
        if remote:
            case.data["hosts"] = {name: {"ssh": "unused", "platform": platform,
                                       "engine": {"sha": fixtures.OLD, "ref": "main"}}}
            case.data["repo"]["acme/widgets"]["host"] = name
            runner, _, _ = case.timeout_remote("none")
        else:
            case.data.pop("hosts", None)
            case.data["repo"]["acme/widgets"].pop("host", None)
        fail = [rollback]
        late = [False]
        probes = []
        original = case.run_cmd
        launcher = case.tool / "bin" / "python"
        argv_text = f"{launcher} -m factory {command}"

        def commands(argv, **kwargs):
            raw = list(argv)
            if argv[:2] == ["env", "LC_ALL=C"]:
                argv = argv[2:]
            if argv[0] == "ps":
                self.assertGreater(kwargs.get("timeout", 0), 0)
                self.assertLessEqual(kwargs["timeout"], 5)
                probes.append(raw)
                live = late[0] if rollback else phase == "before" or ("pause", "factory-widgets.timer") in case.events
                if not live:
                    return subprocess.CompletedProcess(raw, 0, "", "")
                text = argv_text
                if platform == "macos":
                    start = ("木 11/12 00:00:00 2026" if mode == "malformed" or
                             mode == "identity-malformed" and "args=" not in argv[-1] or
                             mode == "localized" and raw[:2] != ["env", "LC_ALL=C"]
                             else "Thu Nov 12 00:00:00 2026")
                    output = f"321 1 {start}" + (f" {text}" if "args=" in argv[-1] else "")
                else:
                    output = f"321 {text}"
                return subprocess.CompletedProcess(raw, 0, output + "\n", "")
            if argv[:2] == ["launchctl", "list"]:
                return subprocess.CompletedProcess(raw, 0, "", "")
            if argv[:3] == ["uv", "tool", "install"] and fail[0]:
                case.calls.append((list(argv), kwargs))
                late[0] = True
                return subprocess.CompletedProcess(raw, 1, "", "fake target install failure")
            return original(argv, **kwargs)

        if runner:
            transport = runner.transport

            def remote_transport(argv, **kwargs):
                parts = shlex.split(argv[-1])
                if parts[:2] == ["python3", "-c"] and parts[2] == host.REMOTE_DEADLINE:
                    parts = shlex.split(parts[-1])
                if parts[0] == "ps" or parts[:3] == ["env", "LC_ALL=C", "ps"]:
                    return commands(parts, **kwargs)
                return transport(argv, **kwargs)

            runner.transport = remote_transport
        record = install_state.ApplyRun("", "", fixtures.OLD)
        with mock.patch.dict(os.environ, {"LC_ALL": "ja_JP.UTF-8", "COLUMNS": "24"}), \
             mock.patch.object(case, "run_cmd", side_effect=commands), \
             mock.patch.object(apply, "run", side_effect=commands):
            result = apply.reconcile_engine(case.data, record, run_cmd=runner or apply.run,
                                            backend=case.backend, host_name=name)
        self.assertEqual(result, "failed" if rollback else "deferred", case.output.getvalue())
        self.assertEqual(len(case.installs()), int(rollback), "no uv replacement while work is live")
        reason = case.output.getvalue() if rollback else record.deferred[
            "engine:remote" if remote else "engine"]["reason"]
        self.assertIn("invalid C-locale ps row" if mode in ("malformed", "identity-malformed")
                      else f"pid 321 factory {command}", reason)
        if rollback:
            self.assertIn("rollback FAIL", reason)
            self.assertEqual(case.active, set())
            self.assertEqual(set(host.engine(case.data, name)["paused_timers"]),
                             {"factory-widgets.timer", "factory-idle.timer"})
            # Once work exits, recovery verifies the target before restoring the exact active set.
            late[0] = fail[0] = False
            with mock.patch.object(case, "run_cmd", side_effect=commands), \
                 mock.patch.object(apply, "run", side_effect=commands):
                self.assertEqual(apply.reconcile_engine(
                    case.data, record, run_cmd=runner or apply.run,
                    backend=case.backend, host_name=name), "applied", case.output.getvalue())
        self.assertEqual(case.active, {"factory-widgets.timer", "factory-idle.timer"})
        self.assertEqual(apply._receipt_sha(case.tool, case.run_cmd),
                         fixtures.TARGET if rollback else fixtures.OLD)
        self.assertTrue(probes)
        if platform == "macos":
            self.assertTrue(all(argv[:3] == ["env", "LC_ALL=C", "ps"] for argv in probes), probes)
            if mode != "malformed":
                self.assertTrue(any("args=" not in argv[-1] for argv in probes), probes)

    def test_macos_locale_and_malformed_rows_never_hide_work(self):
        for remote in (False, True):
            for mode in ("localized", "malformed"):
                for phase in ("before", "after"):
                    for command in ("gate", "dispatch"):
                        with self.subTest(remote=remote, mode=mode, phase=phase, command=command):
                            self.observation_case(remote=remote, mode=mode, phase=phase, command=command)

    def test_macos_locale_and_malformed_rows_block_automatic_rollback(self):
        for remote in (False, True):
            for mode in ("localized", "malformed"):
                for command in ("gate", "dispatch"):
                    with self.subTest(remote=remote, mode=mode, command=command):
                        self.observation_case(remote=remote, mode=mode, command=command, rollback=True)


    def test_localized_fleet_rows_block_replacement_and_outer_rollback(self):
        for mode in ("localized", "malformed"):
            for phase in ("before", "after", "install", "rollback"):
                for blocked in ("local", "remote") if phase == "before" else ("local",) if phase in ("after", "rollback") else ("remote",):
                    for command in ("gate", "dispatch"):
                        with self.subTest(mode=mode, phase=phase, blocked=blocked, command=command):
                            self.case.hand_work_fleet(f"dashboard-{command}", phase=phase,
                                                      blocked=blocked, platform="macos", lstart=mode)

    def test_malformed_identity_observation_defers_named_work(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.observation_case(remote=remote, mode="identity-malformed")