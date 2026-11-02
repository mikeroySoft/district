from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from district import engine_cleanup, host


class EngineCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.data_home = self.root / "data"
        self.tool_dir = self.root / "tools"
        self.bin_dir = self.root / "bin"
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_DATA_HOME": str(self.data_home),
            "UV_TOOL_DIR": str(self.tool_dir),
            "UV_TOOL_BIN_DIR": str(self.bin_dir),
        }))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def leftover(self, name: str, *, historical: bool = False) -> Path:
        root = self.home / ".local/share/district" if historical else self.data_home / "district"
        path = root / f"factory-{name}"
        path.mkdir(parents=True)
        return path

    def remote_runner(self) -> host.RemoteRunner:
        facts = {
            "home": str(self.home),
            "config_home": str(self.config_home),
            "data_home": str(self.data_home),
            "tool_dir": str(self.tool_dir),
            "tool_bin": str(self.bin_dir),
            "path": os.environ.get("PATH", ""),
            "uid": os.getuid(),
        }

        def transport(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["sh", "-c", argv[-1]], capture_output=True, text=True,
                input=kwargs.get("input"), check=False,
            )

        runner = host.RemoteRunner("test", "unused.example", transport=transport)
        runner._facts = facts
        return runner

    def test_scans_expanded_and_symlinked_references_before_removing(self) -> None:
        unit = self.leftover("unit")
        plist = self.leftover("plist", historical=True)
        config = self.leftover("config", historical=True)
        receipt = self.leftover("receipt")
        supplied = self.leftover("supplied")
        unused = self.leftover("unused", historical=True)
        symlink = self.data_home / "district/factory-link"
        symlink.symlink_to(unused, target_is_directory=True)

        alias = self.root / "engine-alias"
        alias.symlink_to(unit, target_is_directory=True)
        units = self.config_home / "systemd/user"
        units.mkdir(parents=True)
        (units / "factory-example.service").write_text(f"ExecStart={alias}/bin/python\n")
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.example.factory.worker.plist").write_bytes(plistlib.dumps({
            "Label": "com.example.factory.worker",
            "ProgramArguments": [str(plist / "bin/python")],
        }))
        config_file = self.config_home / "factory/config.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(f'legacy = \"$HOME/{config.relative_to(self.home)}\"\n')
        tool = self.tool_dir / "factory"
        tool.mkdir(parents=True)
        (tool / "uv-receipt.toml").write_text(f'legacy = "{receipt}"\n')

        engine_cleanup.cleanup({"defaults": {"archived_path": str(supplied)}})

        for path in (unit, plist, config, receipt, supplied, symlink):
            self.assertTrue(path.exists() or path.is_symlink(), str(path))
        self.assertFalse(unused.exists())
        self.assertIn(str(unit), self.output.getvalue())

    def test_service_drop_in_reference_is_retained_locally(self) -> None:
        referenced = self.leftover("service-drop-in")
        unused = self.leftover("unused")
        alias = self.root / "service-engine-alias"
        alias.symlink_to(referenced, target_is_directory=True)
        drop_in = self.config_home / "systemd/user/factory-example.service.d"
        drop_in.mkdir(parents=True)
        (drop_in / "override.conf").write_text(f"ExecStart={alias}/.venv/bin/python\n")

        engine_cleanup.cleanup({})

        self.assertTrue(referenced.exists())
        self.assertFalse(unused.exists())

    def test_timer_drop_in_reference_is_retained_through_remote_runner(self) -> None:
        referenced = self.leftover("timer-drop-in", historical=True)
        unused = self.leftover("unused")
        drop_in = self.config_home / "systemd/user/factory-example.timer.d"
        drop_in.mkdir(parents=True)
        relative = referenced.relative_to(self.home)
        (drop_in / "schedule.conf").write_text(f"Environment=LEGACY=$HOME/{relative}\n")

        engine_cleanup.cleanup({}, run_cmd=self.remote_runner())

        self.assertTrue(referenced.exists())
        self.assertFalse(unused.exists())

    def test_type_wide_and_prefix_service_drop_ins_expand_specifiers_locally(self) -> None:
        type_wide = self.leftover("type-wide%service", historical=True)
        prefixed = self.leftover("prefix-service")
        unused = self.leftover("unused")
        unit_dir = self.config_home / "systemd/user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "factory-example-worker.service").write_text(
            "Environment=PROGRESS=100%%\n")
        type_drop_in = unit_dir / "service.d"
        type_drop_in.mkdir()
        (type_drop_in / "legacy.conf").write_text(
            "ExecStart=%h/.local/share/district/factory-type-wide%%service/bin/python\n")
        prefix_drop_in = unit_dir / "factory-example-.service.d"
        prefix_drop_in.mkdir()
        (prefix_drop_in / "legacy.conf").write_text(
            f"Environment=LEGACY={prefixed}/bin/python\n")

        engine_cleanup.cleanup({})

        self.assertTrue(type_wide.exists())
        self.assertTrue(prefixed.exists())
        self.assertFalse(unused.exists())
        self.assertNotIn("reference scan incomplete", self.output.getvalue())

    def test_type_wide_and_prefix_timer_drop_ins_expand_specifiers_remotely(self) -> None:
        type_wide = self.leftover("type-wide-timer", historical=True)
        prefixed = self.leftover("prefix-timer")
        unused = self.leftover("unused")
        unit_dir = self.config_home / "systemd/user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "factory-nightly-daily.timer").write_text(
            "Description=Factory progress 100%%\n")
        type_drop_in = unit_dir / "timer.d"
        type_drop_in.mkdir()
        (type_drop_in / "legacy.conf").write_text(
            "Environment=LEGACY=%h/.local/share/district/factory-type-wide-timer\n")
        prefix_drop_in = unit_dir / "factory-nightly-.timer.d"
        prefix_drop_in.mkdir()
        (prefix_drop_in / "legacy.conf").write_text(
            f"Environment=LEGACY={prefixed}\n")

        engine_cleanup.cleanup({}, run_cmd=self.remote_runner())

        self.assertTrue(type_wide.exists())
        self.assertTrue(prefixed.exists())
        self.assertFalse(unused.exists())
        self.assertNotIn("reference scan incomplete", self.output.getvalue())

    def test_type_wide_drop_in_is_scanned_without_a_named_unit_fragment(self) -> None:
        candidate = self.leftover("type-wide-only")
        drop_in = self.config_home / "systemd/user/service.d"
        drop_in.mkdir(parents=True)
        (drop_in / "legacy.conf").write_text(f"WorkingDirectory={candidate}\n")

        engine_cleanup.cleanup({})

        self.assertTrue(candidate.exists())

    def test_unknown_systemd_specifier_blocks_cleanup(self) -> None:
        candidate = self.leftover("unknown-specifier")
        unit_dir = self.config_home / "systemd/user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "factory-example.service").write_text(
            "ExecStart=%Z/.local/share/district/factory-unknown-specifier/bin/python\n")

        engine_cleanup.cleanup({})

        self.assertTrue(candidate.exists())
        self.assertIn("reference scan incomplete", self.output.getvalue())

    def test_unreadable_drop_in_fragment_or_directory_removes_nothing(self) -> None:
        first = self.leftover("first")
        second = self.leftover("second")
        unit_dir = self.config_home / "systemd/user"
        fragment = unit_dir / "factory-example.service.d/override.conf"
        fragment.mkdir(parents=True)
        unreadable_dir = unit_dir / "factory-example.timer.d"
        unreadable_dir.write_text("not a directory")

        engine_cleanup.cleanup({})

        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        output = self.output.getvalue()
        self.assertIn("reference scan incomplete", output)
        self.assertIn(str(fragment), output)
        self.assertIn(str(unreadable_dir), output)

    def test_incomplete_reference_scan_removes_nothing(self) -> None:
        unused = self.leftover("unused")
        config_file = self.config_home / "factory/config.toml"
        config_file.mkdir(parents=True)

        engine_cleanup.cleanup({})

        self.assertTrue(unused.exists())
        self.assertIn("reference scan incomplete", self.output.getvalue())

    def test_remote_runner_inspects_and_removes_on_target(self) -> None:
        unused = self.leftover("remote")
        runner = self.remote_runner()
        engine_cleanup.cleanup({}, run_cmd=runner)

        self.assertFalse(unused.exists())
        self.assertIn(str(unused), self.output.getvalue())

    def test_toml_temporal_settings_cleanup_locally_and_remotely(self) -> None:
        for remote in (False, True):
            with self.subTest(remote=remote):
                referenced = self.leftover(f"temporal-reference-{remote}")
                unused = self.leftover(f"temporal-unused-{remote}")
                data = tomllib.loads(
                    "[extensions]\nupdated = 2026-11-01T12:00:00Z\n"
                    "local_updated = 2026-11-01T12:00:00\n"
                    "day = 2026-11-01\nclock = 12:34:56.123\n")
                data["extensions"]["archived_path"] = str(referenced)
                expected = {
                    "updated": "2026-11-01T12:00:00+00:00",
                    "local_updated": "2026-11-01T12:00:00",
                    "day": "2026-11-01", "clock": "12:34:56.123000",
                    "archived_path": str(referenced),
                }
                received = []
                runner = self.remote_runner() if remote else None
                if runner:
                    transport = runner.transport

                    def capture(argv, **kwargs):
                        received.append(json.loads(kwargs["input"])["data"]["extensions"])
                        return transport(argv, **kwargs)

                    runner.transport = capture
                engine_cleanup.cleanup(data, run_cmd=runner)
                self.assertTrue(referenced.exists())
                self.assertFalse(unused.exists())
                self.assertEqual(data["extensions"]["updated"].isoformat(), expected["updated"])
                if remote:
                    self.assertEqual(received, [expected])

    def test_unknown_cleanup_scalar_is_rejected_without_removing_anything(self) -> None:
        for remote in (False, True):
            with self.subTest(remote=remote):
                unused = self.leftover(f"unknown-scalar-{remote}")
                runner = self.remote_runner() if remote else None
                with self.assertRaises(TypeError):
                    engine_cleanup.cleanup({"extensions": {"unknown": object()}}, run_cmd=runner)
                self.assertTrue(unused.exists())


if __name__ == "__main__":
    unittest.main()
