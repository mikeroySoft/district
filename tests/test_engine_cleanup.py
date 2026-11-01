from __future__ import annotations

import contextlib
import io
import os
import plistlib
import subprocess
import tempfile
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

    def test_incomplete_reference_scan_removes_nothing(self) -> None:
        unused = self.leftover("unused")
        config_file = self.config_home / "factory/config.toml"
        config_file.mkdir(parents=True)

        engine_cleanup.cleanup({})

        self.assertTrue(unused.exists())
        self.assertIn("reference scan incomplete", self.output.getvalue())

    def test_remote_runner_inspects_and_removes_on_target(self) -> None:
        unused = self.leftover("remote")
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
        engine_cleanup.cleanup({}, run_cmd=runner)

        self.assertFalse(unused.exists())
        self.assertIn(str(unused), self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
