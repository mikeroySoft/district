"""Engine unit roles come from the registry, never a dashboard-looking suffix."""
from __future__ import annotations

import contextlib
import json
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from district import apply, host, install_state, service
from tests import test_one_install as fixtures


class EngineUnitRolesTest(unittest.TestCase):
    @contextlib.contextmanager
    def fresh_case(self):
        case = fixtures.OneInstallTest()
        case.setUp()
        try:
            yield case
        finally:
            case.doCleanups()

    def role_case(self, *, remote: bool, platform: str, unregistered_owner: bool = False) -> None:
        with self.fresh_case() as case:
            slug = "acme/ops-dashboard"
            main = "factory-ops-dashboard.service"
            timer = "factory-ops-dashboard.timer"
            dashboard = "factory-ops-dashboard-dashboard.service"
            idle_timer = "factory-idle.timer"
            owner = "factory-retired-dashboard.service" if unregistered_owner else main
            case.data["repo"] = {slug: {"path": str(case.repo)}}
            case.data["defaults"]["engine"] = {"sha": fixtures.OLD, "ref": "main"}
            case.data["defaults"]["engine_idle_timeout"] = "5s"
            case.active = {timer, idle_timer}
            case.calls.clear()
            case.events.clear()
            case.backend.factory_units.return_value = {
                main, timer, dashboard, owner, idle_timer, "factory-stopped.timer"
            }
            case.backend.configuration.return_value = {
                "platform": platform,
                "uid": 501,
                "executable": "systemctl" if platform == "linux" else "launchctl",
            }
            case.backend.status.side_effect = lambda name, **_: service.ServiceStatus(
                "loaded" if name in {main, dashboard} else "not-found",
                "active" if name in {main, dashboard} else "inactive",
                "enabled" if name in {main, dashboard} else "disabled",
                42 if name in {main, dashboard} else 0,
            )

            name = "remote" if remote else "local"
            runner = None
            if remote:
                case.data["hosts"] = {
                    name: {"ssh": "unused", "platform": platform,
                           "engine": {"sha": fixtures.OLD, "ref": "main"}}
                }
                case.data["repo"][slug]["host"] = name
                runner, _, _ = case.timeout_remote("none")

            proc_root = case.root / f"proc-{platform}-{name}"
            post_pause_observations = [0]
            proc_reader_executions = [0]
            original = case.run_cmd

            def paused() -> bool:
                return ("pause", timer) in case.events

            def process_live() -> bool:
                return not paused() or post_pause_observations[0] == 0

            def write_fake_proc(live: bool) -> None:
                for pid, parent, start in (("42", "1", "90"), ("123", "42", "100")):
                    directory = proc_root / pid
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / "stat").write_text(
                        f"{pid} (fake worker) S {parent} " + "0 " * 17 + f"{start} 0\n"
                    )
                    (directory / "cgroup").write_text(
                        f"0::/user/{owner}/child\n" if pid == "123" else "0::/user/session.scope\n"
                    )
                    argv = ([str(case.tool / "bin/python"), "-m", "factory", "dispatch"]
                            if pid == "123" and live else [])
                    (directory / "cmdline").write_bytes(
                        b"\0".join(part.encode() for part in argv) + (b"\0" if argv else b"")
                    )

            def execute_proc_reader(argv, kwargs, live):
                write_fake_proc(live)
                script = argv[2]
                replacement = repr(str(proc_root) + "/")
                for literal in ("'/proc/'", '"/proc/"'):
                    script = script.replace(literal, replacement)
                for literal in ("'/proc'", '"/proc"'):
                    script = script.replace(literal, repr(str(proc_root)))
                self.assertNotIn("pathlib.Path('/proc", script)
                self.assertNotIn('pathlib.Path("/proc', script)
                proc_reader_executions[0] += 1
                return subprocess.run(
                    [sys.executable, "-c", script, *argv[3:]],
                    capture_output=True,
                    text=True,
                    input=kwargs.get("input"),
                    timeout=kwargs["timeout"],
                    check=False,
                )

            def commands(argv, **kwargs):
                raw = list(argv)
                if argv[:3] == ["env", "LC_ALL=C", "ps"]:
                    argv = argv[2:]
                if argv[0] == "ps":
                    live = process_live()
                    if "args=" in argv[-1]:
                        command = (f"{case.tool}/bin/python -m factory dispatch" if live else "")
                        if platform == "macos":
                            rows = [f"123 42 Thu Nov 12 00:00:00 2026 {command}".rstrip(),
                                    "42 1 Thu Nov 12 00:00:00 2026"]
                        else:
                            rows = [f"123 {command}".rstrip()] if command else []
                        if paused():
                            post_pause_observations[0] += 1
                        return subprocess.CompletedProcess(raw, 0, "\n".join(rows) + ("\n" if rows else ""), "")
                    return subprocess.CompletedProcess(
                        raw, 0,
                        "123 42 Thu Nov 12 00:00:00 2026\n42 1 Thu Nov 12 00:00:00 2026\n", "")
                if argv[:2] == ["python3", "-c"] and "/proc" in argv[2]:
                    live = process_live()
                    if "cmdline" in argv[2]:
                        result = execute_proc_reader(argv, kwargs, live)
                        if paused():
                            post_pause_observations[0] += 1
                        return result
                    if len(argv) == 3:
                        return subprocess.CompletedProcess(
                            raw, 0, json.dumps({"123": ["100", "42"], "42": ["90", "1"]}), "")
                    return subprocess.CompletedProcess(
                        raw, 0,
                        json.dumps({"123": ["100", f"0::/user/{owner}/child\n"]}), "")
                if argv[:3] == ["systemctl", "--user", "show"]:
                    return subprocess.CompletedProcess(
                        raw, 0, f"Id={owner}\nControlGroup=/user/{owner}\n", "")
                if argv[:2] == ["launchctl", "list"]:
                    return subprocess.CompletedProcess(raw, 0, f"42 0 {owner.removesuffix('.service')}\n", "")
                return original(argv, **kwargs)

            if runner:
                transport = runner.transport

                def remote_transport(argv, **kwargs):
                    parts = shlex.split(argv[-1])
                    if parts[:2] == ["python3", "-c"] and parts[2] == host.REMOTE_DEADLINE:
                        parts = shlex.split(parts[-1])
                    if (parts[0] == "ps" or parts[:3] == ["env", "LC_ALL=C", "ps"]
                            or parts[:2] in (["systemctl", "--user"], ["launchctl", "list"])
                            or parts[:2] == ["python3", "-c"] and "/proc" in parts[2]):
                        return commands(parts, **kwargs)
                    return transport(argv, **kwargs)

                runner.transport = remote_transport

            def is_active(unit, **_):
                case.events.append(("state", unit))
                if unit == main:
                    return "active" if not paused() or post_pause_observations[0] == 0 else "inactive"
                return "active" if unit in case.active else "inactive"

            case.backend.is_active.side_effect = is_active
            record = install_state.ApplyRun("", "", fixtures.OLD)
            with mock.patch.object(case, "run_cmd", side_effect=commands), \
                    mock.patch.object(apply, "run", side_effect=commands):
                result = apply.reconcile_engine(
                    case.data, record, backend=case.backend, run_cmd=runner or apply.run,
                    host_name=name,
                )

            if unregistered_owner:
                self.assertEqual(result, "deferred", case.output.getvalue())
                self.assertEqual(case.installs(), [])
                self.assertEqual(case.active, {timer, idle_timer})
                reason = record.deferred["engine:remote" if remote else "engine"]["reason"]
                self.assertIn("pid 123", reason)
                self.assertEqual(case.backend.start.call_args_list, [])
                return
            self.assertEqual(result, "applied", case.output.getvalue())
            self.assertEqual(len(case.installs()), 1)
            self.assertEqual(case.active, {timer, idle_timer})
            self.assertEqual(record.disabled, {timer, idle_timer})
            self.assertEqual(record.reenabled, record.disabled)
            self.assertGreaterEqual(post_pause_observations[0], 1)
            started = [call.args[0] for call in case.backend.start.call_args_list]
            self.assertEqual(started, [dashboard])
            self.assertNotIn(main, started)
            if platform == "linux":
                self.assertGreater(proc_reader_executions[0], 0, "Linux argv must come from the fake /proc tree")

    def test_ops_dashboard_main_drains_locally_and_remotely_on_linux_and_macos(self):
        for platform in ("linux", "macos"):
            for remote in (False, True):
                with self.subTest(platform=platform, remote=remote):
                    self.role_case(remote=remote, platform=platform)

    def test_unregistered_dashboard_owner_cannot_exempt_work(self):
        for platform in ("linux", "macos"):
            for remote in (False, True):
                with self.subTest(platform=platform, remote=remote):
                    self.role_case(remote=remote, platform=platform, unregistered_owner=True)

    def test_unregistered_dashboard_suffix_is_drained_and_not_restarted(self):
        with self.fresh_case() as case:
            repos = {"acme/ops-dashboard": {"path": str(case.repo)}}
            main = "factory-ops-dashboard.service"
            dashboard = "factory-ops-dashboard-dashboard.service"
            unregistered = "factory-retired-dashboard.service"
            case.backend.factory_units.return_value = {
                main, "factory-ops-dashboard.timer", dashboard, unregistered
            }
            services, _ = apply._engine_units(repos, case.backend, timeout=1)
            self.assertIn(main, services)
            self.assertIn(unregistered, services)
            self.assertNotIn(dashboard, services)

            case.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
            apply.restart_dashboards(repos, case.backend)
            self.assertEqual([call.args[0] for call in case.backend.start.call_args_list], [dashboard])

    def test_registered_main_precedes_a_colliding_dashboard_role(self):
        with self.fresh_case() as case:
            repos = {
                "acme/ops": {"path": str(case.repo)},
                "acme/ops-dashboard": {"path": str(case.repo)},
            }
            collision = "factory-ops-dashboard.service"
            dashboard = "factory-ops-dashboard-dashboard.service"
            case.backend.factory_units.return_value = {
                "factory-ops.service", collision, dashboard,
                "factory-ops.timer", "factory-ops-dashboard.timer",
            }
            services, _ = apply._engine_units(repos, case.backend, timeout=1)
            self.assertIn(collision, services)
            self.assertNotIn(dashboard, services)

            case.backend.status.return_value = service.ServiceStatus("loaded", "active", "enabled", 42)
            apply.restart_dashboards(repos, case.backend)
            self.assertEqual([call.args[0] for call in case.backend.start.call_args_list], [dashboard])
