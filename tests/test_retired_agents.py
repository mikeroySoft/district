"""Scheduler adoption boundary checks using District's isolated host harness."""
import json
import os
import plistlib
from pathlib import Path

from district import host
from tests.test_district import DistrictCase


class ForeignAgentEdgeTest(DistrictCase):
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
