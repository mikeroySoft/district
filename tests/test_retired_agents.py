"""Scheduler adoption boundary checks using District's isolated host harness."""
import json
import os
import plistlib
from pathlib import Path
from unittest import mock

from district import apply, host, install_state
from tests.test_district import DistrictCase


class WrappedSchedulerChecks:
    def wrapped_launchd(self):
        home, repo, old = self.foreign_launchd()
        alias = self.tmp / "checkout-alias"
        alias.symlink_to(repo, target_is_directory=True)
        document = plistlib.loads(old.read_bytes())
        document["WorkingDirectory"] = str(alias)
        document["ProgramArguments"] = self.arguments
        old.write_bytes(plistlib.dumps(document))
        return home, repo, old

    def test_apply_retires_wrapped_scheduler_before_install(self):
        home, _, old = self.wrapped_launchd()
        original = old.read_bytes()

        code, out = self.district("apply")

        self.assertEqual(code, 0, out)
        calls = self.calls("launchctl")
        bootout = f"bootout gui/{os.getuid()}/{old.stem}"
        bootstrap = f"bootstrap gui/{os.getuid()} {old.parent / 'factory-widgets.plist'}"
        self.assertIn(bootout, calls)
        self.assertLess(calls.index(bootout), calls.index(bootstrap))
        self.assertFalse(old.exists())
        self.assertEqual([path.read_bytes() for path in home.glob(
            f".local/state/district/retired-agents/*/{old.name}")], [original])
        states = [json.loads(line) for line in (self.bin / "loaded.jsonl").read_text().splitlines()]
        self.assertTrue(all(len(state) <= 1 for state in states), states)
        self.assertEqual(states[-1], ["factory-widgets"])

    def test_dry_run_lists_wrapped_scheduler_without_mutation(self):
        home, _, old = self.wrapped_launchd()
        registry = host.path().read_bytes()
        before = {path.relative_to(home): path.read_bytes()
                  for path in home.rglob("*") if path.is_file()}

        code, out = self.district("apply", "--dry-run")

        self.assertEqual(code, 0, out)
        for text in ("would", "ultron", old.stem, str(old), "loaded", "factory-widgets"):
            self.assertIn(text, out)
        self.assertEqual(host.path().read_bytes(), registry)
        self.assertEqual({path.relative_to(home): path.read_bytes()
                          for path in home.rglob("*") if path.is_file()}, before)
        self.assertFalse(install_state.state_path().exists())
        self.assertFalse(any(call.startswith(("bootout ", "bootstrap ", "kill ", "disable ", "enable "))
                             for call in self.calls("launchctl")))
        self.assertFalse(any(call.startswith(("disable ", "enable ", "stop ", "start ", "daemon-reload"))
                             for call in self.calls("systemctl")))
        self.assertNotIn("install", self.calls("factory"))

    def test_doctor_reports_wrapped_scheduler_conflict_without_mutation(self):
        home, repo, old = self.wrapped_launchd()
        (old.parent / "factory-widgets.plist").write_bytes(plistlib.dumps({
            "Label": "factory-widgets", "WorkingDirectory": str(repo),
            "ProgramArguments": ["factory", "dispatch"], "StartInterval": 600,
        }))
        before = {path.relative_to(home): path.read_bytes()
                  for path in home.rglob("*") if path.is_file()}

        code, out = self.district("doctor", "--json")

        rows = json.loads(out)["rows"]
        conflict = [row for row in rows
                    if row["label"] == "host ultron repo acme/widgets scheduler conflict"]
        self.assertEqual(code, 1, rows)
        self.assertEqual(len(conflict), 1, rows)
        self.assertEqual(conflict[0]["status"], "FAIL")
        for text in (str(repo.resolve()), old.stem, "factory-widgets"):
            self.assertIn(text, conflict[0]["detail"])
        self.assertEqual({path.relative_to(home): path.read_bytes()
                          for path in home.rglob("*") if path.is_file()}, before)
        self.assertFalse(any(call.startswith(("bootout ", "bootstrap ", "kill ", "disable ", "enable "))
                             for call in self.calls("launchctl")))


class ShellWrappedSchedulerTest(WrappedSchedulerChecks, DistrictCase):
    arguments = ["/bin/zsh", "-lc", "source ~/.zprofile; factory dispatch"]


class UvWrappedSchedulerTest(WrappedSchedulerChecks, DistrictCase):
    arguments = ["uv", "run", "--project", ".", "factory", "dispatch"]


class ForeignAgentEdgeTest(DistrictCase):
    def test_apply_follows_checkout_change_after_shell_initialization(self):
        _, repo, old = self.foreign_launchd()
        other = self.repo("other", toml="")
        document = plistlib.loads(old.read_bytes())
        document["WorkingDirectory"] = str(other)
        document["ProgramArguments"] = [
            "/bin/zsh", "-lc", f"source ~/.zprofile;\ncd '{repo}' && exec factory pass",
        ]
        old.write_bytes(plistlib.dumps(document))

        code, out = self.district("apply")

        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        states = [json.loads(line) for line in (self.bin / "loaded.jsonl").read_text().splitlines()]
        self.assertTrue(all(len(state) <= 1 for state in states), states)
        self.assertEqual(states[-1], ["factory-widgets"])

    def test_apply_follows_uv_directory_without_confusing_option_values(self):
        _, repo, old = self.foreign_launchd()
        other = self.repo("other", toml="")
        document = plistlib.loads(old.read_bytes())
        document["WorkingDirectory"] = str(other)
        document["ProgramArguments"] = [
            "uv", "run", "--frozen", f"--directory={repo}", "--with", "factory", "--", "factory", "pass",
        ]
        old.write_bytes(plistlib.dumps(document))
        unrelated = old.parent / "maintenance.plist"
        maintenance = plistlib.dumps({
            "Label": unrelated.stem, "WorkingDirectory": str(repo),
            "ProgramArguments": ["uv", "run", "--with", "factory", "dispatch"],
        })
        unrelated.write_bytes(maintenance)

        code, out = self.district("apply")

        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_bytes(), maintenance)
        self.assertFalse(any(call.startswith(("bootout ", "disable "))
                             and call.endswith("/maintenance") for call in self.calls("launchctl")))

    def test_apply_ignores_quoted_and_echoed_factory_commands(self):
        _, repo, old = self.foreign_launchd()
        commands = [
            "echo factory dispatch",
            "source ~/.zprofile; echo 'factory dispatch'",
            "source ~/.zprofile; 'factory dispatch'",
            "echo ';' factory dispatch",
            'printf "%s" "source ~/.zprofile; factory dispatch"',
        ]
        originals = {}
        for index, command in enumerate(commands):
            path = old.parent / f"maintenance-{index}.plist"
            originals[path] = plistlib.dumps({
                "Label": path.stem, "WorkingDirectory": str(repo),
                "ProgramArguments": ["/bin/zsh", "-lc", command],
            })
            path.write_bytes(originals[path])

        code, out = self.district("apply")

        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(any(call.startswith(("bootout ", "disable "))
                                 and call.endswith(f"/{path.stem}") for call in self.calls("launchctl")))

    def test_apply_ignores_non_utf8_unrelated_systemd_timer(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        directory = host.unit_dir()
        directory.mkdir(parents=True)
        timer = directory / "maintenance.timer"
        original = b"[Unit]\nDescription=Maintenance \xff\n[Timer]\nOnBootSec=1h\n"
        timer.write_bytes(original)

        code, out = self.district("apply")

        self.assertEqual(code, 0, out)
        self.assertEqual(timer.read_bytes(), original)
        self.assertIn("install", self.calls("factory"))

    def test_apply_reports_non_utf8_relevant_systemd_definitions(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        directory = host.unit_dir()
        directory.mkdir(parents=True)
        service = directory / "legacy.service"
        service.write_text(f"[Service]\nWorkingDirectory={repo}\nExecStart=factory dispatch\n")
        timer = directory / "custom.timer"
        timer.write_bytes(b"[Unit]\nDescription=Factory \xff\n[Timer]\nUnit=legacy.service\n")

        code, out = self.district("apply")

        self.assertEqual(code, 1, out)
        self.assertIn("cannot inspect scheduler agents", out)
        self.assertTrue(service.exists())
        self.assertTrue(timer.exists())
        self.assertNotIn("install", self.calls("factory"))

    def test_apply_propagates_install_oserror_without_foreign_agents(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        original_run = apply.run

        def run(argv, **kwargs):
            if argv[1:] == ["install"]:
                raise OSError("installer unavailable")
            return original_run(argv, **kwargs)

        with mock.patch.object(apply, "run", side_effect=run):
            with self.assertRaisesRegex(OSError, "installer unavailable"):
                self.district("apply")

    def test_apply_propagates_drain_oserror_without_foreign_agents(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})

        with mock.patch.object(apply, "drain", side_effect=OSError("cannot drain scheduler")):
            with self.assertRaisesRegex(OSError, "cannot drain scheduler"):
                self.district("apply")

    def test_apply_finds_dispatch_before_later_oneshot_commands(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        directory = host.unit_dir()
        directory.mkdir(parents=True)
        old = directory / "legacy.service"
        old.write_text(
            f"[Service]\nType=oneshot\nWorkingDirectory={repo}\n"
            "ExecStart=factory dispatch\nExecStart=/usr/bin/true\n")
        reset = directory / "maintenance.service"
        reset.write_text(
            f"[Service]\nType=oneshot\nWorkingDirectory={repo}\n"
            "ExecStart=factory dispatch\nExecStart=\nExecStart=/usr/bin/true\n")

        code, out = self.district("apply")

        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        self.assertTrue(reset.exists())
        backups = list(Path.home().glob(
            ".local/state/district/retired-agents/*/legacy.service"))
        self.assertEqual([path.read_text() for path in backups], [
            f"[Service]\nType=oneshot\nWorkingDirectory={repo}\n"
            "ExecStart=factory dispatch\nExecStart=/usr/bin/true\n"])

    def test_apply_restores_systemd_agent_after_install_fails_before_writing_units(self):
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        directory = host.unit_dir()
        directory.mkdir(parents=True)
        originals = {
            "legacy.service": f"[Service]\nWorkingDirectory={repo}\nExecStart=factory dispatch\n",
            "legacy.timer": "[Timer]\nOnUnitActiveSec=10min\n",
        }
        for name, body in originals.items():
            (directory / name).write_text(body)
        self.stub("systemctl",
                  ("disable --now factory-widgets.*", "no such unit\n", 1),
                  ("show legacy.timer --property=ActiveState --value --no-pager", "active\n"),
                  ("show * --property=UnitFileState --value --no-pager", "enabled\n"),
                  ("show * --property=MainPID --value --no-pager", "0\n"))
        self.stub("factory", ("install", "installer failed early\n", 1))

        code, out = self.district("apply")

        self.assertEqual(code, 1, out)
        self.assertIn("old agent restored", out)
        self.assertEqual({name: (directory / name).read_text() for name in originals}, originals)
        self.assertIn("enable --now legacy.timer", self.calls("systemctl"))

    def test_apply_rollback_removes_partial_launchd_scheduler_before_retry(self):
        home, _, old = self.foreign_launchd()
        factory = self.bin / "factory"
        working = factory.read_text()
        factory.write_text(working.replace(
            '    sys.exit(0)\nfor pat, out, rc in',
            '    sys.exit(1 if args == "install" else 0)\nfor pat, out, rc in', 1))

        code, out = self.district("apply")

        self.assertEqual(code, 1, out)
        self.assertTrue(old.exists())
        self.assertFalse((old.parent / "factory-widgets.plist").exists())
        self.assertIn(f"enable gui/{os.getuid()}/factory-widgets", self.calls("launchctl"))
        factory.write_text(working)
        code, out = self.district("apply")
        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        states = [json.loads(line) for line in (self.bin / "loaded.jsonl").read_text().splitlines()]
        self.assertTrue(all(len(state) <= 1 for state in states), states)
        self.assertEqual(states[-1], ["factory-widgets"])

    def test_apply_ignores_unrelated_agent_with_invalid_shell_command(self):
        _, repo, old = self.foreign_launchd()
        unrelated = old.parent / "maintenance.plist"
        original = plistlib.dumps({
            "Label": "maintenance", "WorkingDirectory": str(repo),
            "ProgramArguments": ["/bin/sh", "-c", "echo 'unterminated"],
        })
        unrelated.write_bytes(original)

        code, out = self.district("apply")

        self.assertEqual(code, 0, out)
        self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_bytes(), original)
        self.assertFalse(any(call.startswith(("bootout ", "disable "))
                             and call.endswith("/maintenance") for call in self.calls("launchctl")))

    def test_apply_reset_adopts_before_starting_new_service(self):
        home, _, old = self.foreign_launchd()
        data = host.load()
        data["repo"]["acme/widgets"]["disabled_at"] = "2026-11-01T00:00:00Z"
        host.save(data)
        launchctl = self.bin / "launchctl"
        launchctl.write_text(launchctl.read_text().replace(
            'if args[:1] == ["bootstrap"]:\n',
            'if args[:1] == ["bootstrap"] and not pathlib.Path(args[2]).exists():\n'
            '    raise SystemExit(5)\n'
            'if args[:1] == ["bootstrap"]:\n',
        ).replace('print("state = exited")', 'print("state = exited\\nlast exit code = 0")'))

        code, out = self.district("apply", "--reset", "acme/widgets")

        self.assertEqual(code, 0, out)
        self.assertNotIn("disabled_at", host.repos(host.load())["acme/widgets"])
        self.assertFalse(old.exists())
        self.assertTrue((home / "Library/LaunchAgents/factory-widgets.plist").exists())

    def test_apply_restores_old_agent_even_if_replacement_enable_fails(self):
        home, _, old = self.foreign_launchd()
        original = old.read_bytes()
        factory = self.bin / "factory"
        factory.write_text(factory.read_text().replace(
            '    sys.exit(0)\nfor pat, out, rc in',
            '    sys.exit(1 if args == "install" else 0)\nfor pat, out, rc in', 1))
        launchctl = self.bin / "launchctl"
        launchctl.write_text(launchctl.read_text().replace(
            'if args[:1] == ["bootout"]:',
            'if args == ["enable", f"gui/{os.getuid()}/factory-widgets"]:\n'
            '    raise SystemExit(7)\n'
            'if args[:1] == ["bootout"]:', 1))

        code, out = self.district("apply")

        self.assertEqual(code, 1, out)
        self.assertEqual(old.read_bytes(), original)
        self.assertFalse((home / f"{old.stem}.unloaded").exists())
        states = [json.loads(line) for line in (self.bin / "loaded.jsonl").read_text().splitlines()]
        self.assertTrue(all(len(state) <= 1 for state in states), states)
        self.assertEqual(states[-1], [old.stem])
