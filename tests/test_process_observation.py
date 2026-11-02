"""Owning-host process format boundaries, with no live process inspection."""
import os
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from district import apply, host, install_state
from tests import test_one_install as fixtures


class ProcessObservationTest(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.OneInstallTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def observation_case(self, *, remote, platform="macos", mode="localized",
                         phase="before", command="dispatch", rollback=False, form="module",
                         argv_override=None, cwd_override=None, busy=True, reason_contains=None):
        case = self.case
        if mode == "width":
            case.tool = case.root / ("long-launcher-" + "x" * 120) / "factory"
            os.environ["UV_TOOL_DIR"] = str(case.tool.parent)
        if mode == "spaces":
            case.tool = case.root / "Library/Application Support/FactoryTools/factory"
            case.cli = case.root / "Tool Bin's Space/factory"
            os.environ["UV_TOOL_DIR"] = str(case.tool.parent)
            os.environ["UV_TOOL_BIN_DIR"] = str(case.cli.parent)
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
        exact_argv = ([str(launcher), "-m", "factory", command] if form == "module" else
                      [str(case.cli), command] if form == "launcher" else
                      [str(launcher), "-u", str(case.cli), command])
        if form in ("option-X", "option-W"):
            flag, value = (("-X", "pycache_prefix=/tmp/Cache Files") if form == "option-X"
                           else ("-W", "ignore:message with spaces:UserWarning"))
            exact_argv = [str(launcher), flag, value, "-m", "factory", command]
        if form == "terminated-script":
            command_args = ["--base", "origin/main"] if command == "gate" else ["--ticket", "12"]
            exact_argv = [str(launcher), "--", str(case.cli), command, *command_args]
        if argv_override is not None:
            exact_argv = argv_override
        argv_text = " ".join(exact_argv)
        if mode == "mention":
            exact_argv = ["/usr/bin/python3", "-c", f"print({str(case.tool)!r})"]
            argv_text = " ".join(exact_argv)

        def commands(argv, **kwargs):
            raw = list(argv)
            macos_observer = argv == ["sh", "-c", apply._MACOS_PROCESSES]
            if macos_observer:
                argv = ["ps", "-ww", "-eo", "pid=,ppid=,lstart=,args="]
            if argv[:2] == ["env", "LC_ALL=C"]:
                argv = argv[2:]
            if argv[:2] == ["python3", "-c"] and "/proc/" in argv[2]:
                live = late[0] if rollback else phase == "before" or ("pause", "factory-widgets.timer") in case.events
                root = case.root / f"proc-{mode}"
                directory = root / "321"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "stat").write_text("321 (worker ( odd ) name)) S 1 " + "0 " * 17 + "100 0\n")
                (directory / "cgroup").write_text("0::/user/session.scope\n")
                if mode == "observer":
                    for pid, parent, start in (("901", "902", "101"), ("902", "1", "102")):
                        observer = root / pid
                        observer.mkdir(parents=True, exist_ok=True)
                        (observer / "stat").write_text(
                            f"{pid} (observer) S {parent} " + "0 " * 17 + f"{start} 0\n")
                        (observer / "cgroup").write_text("0::/user/session.scope\n")
                        (observer / "cmdline").write_bytes(
                            b"/usr/bin/python3\0-m\0factory.__main__\0gate\0")
                        (observer / "cwd").unlink(missing_ok=True)
                        (observer / "cwd").symlink_to("/tmp", target_is_directory=True)
                cwd = directory / "cwd"
                cwd.unlink(missing_ok=True)
                if cwd_override is not None:
                    target_cwd = cwd_override if live else case.root / "outside"
                    cwd.symlink_to(target_cwd, target_is_directory=True)
                cmdline = directory / "cmdline"
                if mode == "unreadable":
                    if cmdline.is_file():
                        cmdline.unlink()
                    cmdline.mkdir(exist_ok=True)
                else:
                    cmdline.write_bytes(b"\0".join(word.encode() for word in exact_argv) + b"\0"
                                        if live and mode not in ("empty", "observer") else b"")
                if mode == "vanished":
                    (directory / "stat").unlink(missing_ok=True)
                probes.append(raw)
                script = argv[2].replace("'/proc/'", repr(str(root) + "/"))
                if mode == "observer":
                    script = script.replace("str(os.getpid())", "'901'")
                return subprocess.run([sys.executable, "-c", script, *argv[3:]],
                                      capture_output=True, text=True, timeout=kwargs["timeout"])
            if argv[0] == "ps":
                self.assertGreater(kwargs.get("timeout", 0), 0)
                self.assertLessEqual(kwargs["timeout"], 5)
                probes.append(raw)
                live = late[0] if rollback else phase == "before" or ("pause", "factory-widgets.timer") in case.events
                if not live:
                    return subprocess.CompletedProcess(raw, 0, "900\n" if macos_observer else "", "")
                text = argv_text
                if mode == "width" and "-ww" not in argv:
                    text = text[:int(os.environ["COLUMNS"])]
                if platform == "macos":
                    start = ("木 11/12 00:00:00 2026" if mode == "malformed" or
                             mode == "identity-malformed" and "args=" not in argv[-1] or
                             mode == "localized" and raw[:2] != ["env", "LC_ALL=C"] and not macos_observer
                             else "Thu Nov 12 00:00:00 2026")
                    if mode == "observer":
                        suffix = f" {text}" if "args=" in argv[-1] else ""
                        output = f"900 901 {start}{suffix}\n901 1 {start}{suffix}"
                    else:
                        output = f"321 1 {start}" + (f" {text}" if "args=" in argv[-1] else "")
                else:
                    output = f"901 {text}\n902 {text}" if mode == "observer" else f"321 {text}"
                return subprocess.CompletedProcess(
                    raw, 0, ("900\n" if macos_observer else "") + output + "\n", "")
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
                if parts[:2] == ["python3", "-c"] and "/proc/" in parts[2]:
                    return commands(parts, **kwargs)
                if (parts[0] == "ps" or parts[:3] == ["env", "LC_ALL=C", "ps"]
                        or parts == ["sh", "-c", apply._MACOS_PROCESSES]):
                    return commands(parts, **kwargs)
                return transport(argv, **kwargs)

            runner.transport = remote_transport
        record = install_state.ApplyRun("", "", fixtures.OLD)
        with mock.patch.dict(os.environ, {"LC_ALL": "ja_JP.UTF-8", "COLUMNS": "24"}), \
             mock.patch.object(case, "run_cmd", side_effect=commands), \
             mock.patch.object(apply, "run", side_effect=commands):
            result = apply.reconcile_engine(case.data, record, run_cmd=runner or apply.run,
                                            backend=case.backend, host_name=name)
        if not busy:
            self.assertEqual(result, "applied", case.output.getvalue())
            self.assertEqual(len(case.installs()), 1)
            self.assertEqual(case.active, {"factory-widgets.timer", "factory-idle.timer"})
            self.assertEqual(record.deferred, {})
            self.assertEqual(apply._receipt_sha(case.tool, case.run_cmd), fixtures.TARGET)
            return
        self.assertEqual(result, "failed" if rollback else "deferred", case.output.getvalue())
        self.assertEqual(len(case.installs()), int(rollback), "no uv replacement while work is live")
        reason = case.output.getvalue() if rollback else record.deferred[
            "engine:remote" if remote else "engine"]["reason"]
        self.assertIn(reason_contains or (
            "invalid C-locale ps row" if mode in ("malformed", "identity-malformed")
            else "pid 321"), reason)
        if not rollback:
            paused = {name for event, name in case.events if event == "pause"}
            self.assertEqual(paused, set() if phase == "before" else
                             {"factory-widgets.timer", "factory-idle.timer"})
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
            self.assertTrue(all(
                argv[:3] == ["env", "LC_ALL=C", "ps"]
                or argv == ["sh", "-c", apply._MACOS_PROCESSES] for argv in probes), probes)
            if mode != "malformed":
                self.assertTrue(any(
                    argv == ["sh", "-c", apply._MACOS_PROCESSES]
                    or "args=" not in argv[-1] for argv in probes), probes)

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
                            self.case.hand_work_fleet(command, phase=phase,
                                                      blocked=blocked, platform="macos", lstart=mode)

    def test_malformed_identity_observation_defers_named_work(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.observation_case(remote=remote, mode="identity-malformed")

    def test_long_argv_under_narrow_columns_still_defers(self):
        for platform in ("linux", "macos"):
            for remote in (False, True):
                with self.subTest(platform=platform, remote=remote):
                    self.observation_case(remote=remote, platform=platform, mode="width")

    def test_space_paths_block_replacement_and_automatic_rollback(self):
        for platform in ("linux", "macos"):
            for remote in (False, True):
                for form in ("module", "launcher", "script"):
                    for command in ("gate", "dispatch"):
                        for phase in ("before", "after", "rollback"):
                            with self.subTest(platform=platform, remote=remote, form=form,
                                              command=command, phase=phase):
                                self.observation_case(remote=remote, platform=platform, mode="spaces",
                                                      form=form, command=command,
                                                      phase=phase, rollback=phase == "rollback")

    def test_space_paths_block_fleet_replacement_and_outer_rollback(self):
        for platform in ("linux", "macos"):
            for form in ("module", "launcher", "script"):
                for phase in ("before", "after", "install", "rollback"):
                    for blocked in ("local", "remote") if phase == "before" else ("local",) if phase in ("after", "rollback") else ("remote",):
                        for command in ("gate", "dispatch"):
                            with self.subTest(platform=platform, form=form, phase=phase,
                                              blocked=blocked, command=command):
                                self.case.hand_work_fleet(
                                    command, phase=phase, blocked=blocked,
                                    platform=platform, space_paths=True, form=form)

    def test_text_only_observations_fail_closed(self):
        for remote in (False, True):
            for mode in ("spaces", "mention"):
                for form in ("option-X", "option-W"):
                    for command in ("gate", "dispatch"):
                        with self.subTest(remote=remote, mode=mode, form=form, command=command):
                            self.observation_case(remote=remote, platform="macos", mode=mode,
                                                  form=form, command=command)

    def test_missing_exact_cmdline_is_named_busy(self):
        for remote in (False, True):
            for mode in ("empty", "unreadable"):
                with self.subTest(remote=remote, mode=mode):
                    self.observation_case(remote=remote, platform="linux", mode=mode)

    def test_multiword_python_options_block_replacement_and_automatic_rollback(self):
        for remote in (False, True):
            for form in ("option-X", "option-W"):
                for command in ("gate", "dispatch"):
                    for phase in ("before", "after", "rollback"):
                        with self.subTest(remote=remote, form=form, command=command, phase=phase):
                            self.observation_case(remote=remote, platform="linux", form=form,
                                                  command=command, phase=phase, rollback=phase == "rollback")

    def test_multiword_python_options_block_fleet_replacement_and_outer_rollback(self):
        for option in (("-X", "pycache_prefix=/tmp/Cache Files"),
                       ("-W", "ignore:message with spaces:UserWarning")):
            for phase, blocked in (("before", "local"), ("before", "remote"), ("after", "local"),
                                   ("install", "remote"), ("rollback", "local")):
                for command in ("gate", "dispatch"):
                    with self.subTest(option=option, phase=phase, blocked=blocked, command=command):
                        self.case.hand_work_fleet(command, phase=phase, blocked=blocked,
                                                  python_option=option)

    def test_python_terminated_launcher_blocks_replacement_and_automatic_rollback(self):
        for remote in (False, True):
            for command in ("gate", "dispatch"):
                for phase in ("before", "after", "rollback"):
                    with self.subTest(remote=remote, command=command, phase=phase):
                        self.observation_case(remote=remote, platform="linux", form="terminated-script",
                                              command=command, phase=phase, rollback=phase == "rollback")

    def test_python_terminated_launcher_blocks_fleet_replacement_and_outer_rollback(self):
        for phase, blocked in (("before", "local"), ("before", "remote"),
                               ("after", "local"), ("after", "remote"),
                               ("install", "remote"), ("rollback", "local")):
            for command in ("gate", "dispatch"):
                with self.subTest(phase=phase, blocked=blocked, command=command):
                    self.case.hand_work_fleet(command, phase=phase, blocked=blocked,
                                              form="terminated-script")

    def test_relative_and_equivalent_factory_entrypoints_block_replacement_and_automatic_rollback(self):
        outside = self.case.root / "outside"
        tool_bin = self.case.tool / "bin"
        cases = (
            (["python", "-m", "factory", "gate"], outside),
            (["./python", "-m", "factory", "dispatch"], tool_bin),
            ([str(tool_bin / "python"), "--", "./factory", "gate"], tool_bin),
            (["/usr/bin/python3", "-m", "factory.__main__", "gate"], outside),
        )
        for remote in (False, True):
            for argv, cwd in cases:
                for phase in ("before", "after", "rollback"):
                    with self.subTest(remote=remote, argv=argv, cwd=cwd, phase=phase):
                        self.observation_case(
                            remote=remote, platform="linux", argv_override=argv,
                            cwd_override=cwd, command=argv[-1], phase=phase,
                            rollback=phase == "rollback", reason_contains="pid 321",
                        )

    def test_relative_and_equivalent_factory_entrypoints_block_fleet_replacement_and_outer_rollback(self):
        outside = self.case.root / "outside"
        tool_bin = self.case.root / "tools/factory/bin"
        cases = (
            (["python", "-m", "factory", "gate"], outside, "gate"),
            (["./python", "-m", "factory", "dispatch"], tool_bin, "dispatch"),
            ([str(tool_bin / "python"), "--", "./factory", "gate"], tool_bin, "gate"),
            (["/usr/bin/python3", "-m", "factory.__main__", "gate"], outside, "gate"),
        )
        phases = (
            ("before", "local"), ("before", "remote"),
            ("after", "local"), ("after", "remote"),
            ("install", "remote"), ("rollback", "local"),
        )
        for argv, cwd, command in cases:
            for phase, blocked in phases:
                with self.subTest(argv=argv, cwd=cwd, phase=phase, blocked=blocked):
                    self.case.hand_work_fleet(
                        command, phase=phase, blocked=blocked, argv_override=argv,
                        cwd_override=cwd, reason_contains="pid 321",
                    )

    def test_factory_path_name_and_cwd_boundaries_with_exact_proc_argv(self):
        tool_bin = self.case.tool / "bin"
        tool_python = str(tool_bin / "python")
        launcher = str(self.case.cli)
        outside = self.case.root / "outside"
        cases = (
            (["/usr/bin/python3", "-m", "factory", "gate"], outside, True, "factory name"),
            (["/usr/bin/python3", "-mfactory", "gate"], outside, True, "factory name"),
            (["/usr/bin/python3", "--", "/tmp/factory", "gate"], outside, True, "factory name"),
            ([tool_python], outside, True, "factory path"),
            # Former Python-grammar negatives remain BUSY because the managed interpreter,
            # launcher, or Factory name is sufficient lexical evidence.
            ([tool_python, "-bBm", "factory", "gate"], outside, True, "factory path"),
            ([tool_python, "-mfactory", "gate"], outside, True, "factory path"),
            ([tool_python, "-Wignore", launcher, "dispatch"], outside, True, "factory path"),
            ([tool_python, "-X", "dev", "--", launcher, "gate"], outside, True, "factory path"),
            ([tool_python, "--check-hash-based-pycs", "always", "--", launcher, "gate"],
             outside, True, "factory path"),
            ([tool_python, "--help-env", "--unknown", "-bBdEiIOOPqRsSuvVx", launcher, "gate"],
             outside, True, "factory path"),
            ([tool_python, "--", "-m", "factory", "gate"], outside, True, "factory path"),
            ([tool_python, "-", launcher, "gate"], outside, True, "factory path"),
            ([tool_python, "-c", launcher, "gate"], outside, True, "factory path"),
            ([tool_python, "-Ic", launcher, "gate"], outside, True, "factory path"),
            ([tool_python, f"-c{launcher}", "gate"], outside, True, "factory path"),
            ([tool_python, "--", "/tmp/unrelated.py", "gate"], outside, True, "factory path"),
            (["/usr/bin/python3", f"--root={tool_python}"], outside, True, "factory path"),
            (["../bin/python"], tool_bin, True, "factory path"),
            (["./python", "--", "-"], None, True, "cwd unavailable for relative argv"),
            (["/usr/bin/python3", "--", "-"], tool_bin, True, "cwd in"),
            (["/usr/bin/python3", "--", "-"], "/tmp", False, None),
            (["/tmp/factory-other"], outside, False, None),
            (["/usr/bin/python3", "/tmp/unrelated.py", "gate"], outside, False, None),
            (["/usr/bin/python3", "-m", "http.server"], "/tmp", False, None),
            (["/usr/bin/python3"], None, False, None),
            (["/usr/bin/python3", "/tmp/unrelated.py"], None, False, None),
            (["/usr/bin/python3", "-c", "print('factory gate')"], outside, False, None),
        )
        for remote in (False, True):
            for argv, cwd, busy, reason in cases:
                with self.subTest(remote=remote, argv=argv, cwd=cwd):
                    self.observation_case(
                        remote=remote, platform="linux", busy=busy, argv_override=argv,
                        cwd_override=cwd, reason_contains=reason,
                    )

    def test_repo_and_district_cwds_are_busy_on_the_owning_host(self):
        for remote in (False, True):
            data_home = self.case.root / ("remote-data" if remote else "data")
            for cwd in (
                self.case.repo,
                self.case.repo / ".factory/wt-41",
                data_home / "district/roots",
                data_home / "district/roots/work",
            ):
                with self.subTest(remote=remote, cwd=cwd):
                    self.observation_case(
                        remote=remote, platform="linux", argv_override=["/usr/bin/sleep", "10"],
                        cwd_override=cwd, reason_contains="cwd in",
                    )

    def test_observer_and_ancestor_processes_do_not_block(self):
        for platform in ("linux", "macos"):
            for remote in (False, True):
                with self.subTest(platform=platform, remote=remote):
                    self.observation_case(
                        remote=remote, platform=platform, mode="observer", busy=False,
                    )

    def test_vanished_process_does_not_block(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.observation_case(remote=remote, platform="linux", mode="vanished", busy=False)

    def test_unowned_dashboard_command_is_busy(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.observation_case(
                    remote=remote, platform="linux", form="option-X",
                    command="dashboard", reason_contains="pid 321",
                )

    def test_factory_name_in_script_arguments_is_busy(self):
        cases = (
            (["/usr/bin/python3", "-c", "print('factory gate')"], False),
            (["/usr/bin/python3", "/tmp/unrelated.py", "-m", "factory", "dispatch"], True),
        )
        for platform in ("linux", "macos"):
            for remote in (False, True):
                for argv, busy in cases:
                    with self.subTest(platform=platform, remote=remote, argv=argv):
                        self.observation_case(
                            remote=remote, platform=platform, argv_override=argv, busy=busy,
                            reason_contains="pid 321",
                        )