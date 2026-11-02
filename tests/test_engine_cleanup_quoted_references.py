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


class QuotedEngineCleanupReferencesTest(unittest.TestCase):
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

    def alias(self, name: str, target: Path, *, under_home: bool = False) -> Path:
        parent = (self.home / "aliases" / name if under_home
                  else self.root / "aliases" / name)
        parent.mkdir(parents=True, exist_ok=True)
        alias = parent / "Engine Alias"
        alias.symlink_to(target, target_is_directory=True)
        return alias

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

    def test_complete_reference_values_retain_space_aliases_locally_and_remotely(self) -> None:
        for remote in (False, True):
            with self.subTest(remote=remote):
                suffix = "remote" if remote else "local"
                referenced = {
                    name: self.leftover(f"{suffix}-{name}", historical=index % 2 == 0)
                    for index, name in enumerate((
                        "exec-double", "exec-single", "exec-space", "environment",
                        "working-directory", "specifier", "plist", "config", "receipt",
                        "path-list", "unquoted-working",
                    ))
                }
                aliases = {
                    name: self.alias(f"{suffix}-{name}", target)
                    for name, target in referenced.items()
                    if name != "specifier"
                }
                specifier_alias = self.alias(
                    f"{suffix}-specifier", referenced["specifier"], under_home=True)
                unused = self.leftover(f"{suffix}-unused")

                unit_dir = self.config_home / "systemd/user"
                unit_dir.mkdir(parents=True, exist_ok=True)
                escaped_space = str(aliases["exec-space"]).replace(
                    "Engine Alias", r"Engine\sAlias")
                escaped_hex = str(aliases["working-directory"]).replace(
                    "Engine Alias", r"Engine\x20Alias")
                specifier_value = str(specifier_alias.relative_to(self.home)).replace(
                    " ", r"\s")
                (unit_dir / f"factory-{suffix}.service").write_text(
                    "[Service]\n"
                    f'ExecStart="{aliases["exec-double"]}/.venv/bin/python" \\\n'
                    "  -m factory dispatch\n"
                    f"ExecStart={escaped_space}/.venv/bin/python -m factory gate\n"
                    f'Environment="LEGACY={aliases["environment"]}/.venv"\n'
                    f"WorkingDirectory={escaped_hex}\n"
                    f'Environment="PATH=/usr/bin:{aliases["path-list"]}/bin"\n'
                    f'WorkingDirectory={aliases["unquoted-working"]}\n'
                )
                drop_in = unit_dir / f"factory-{suffix}.service.d"
                drop_in.mkdir()
                (drop_in / "legacy.conf").write_text(
                    "[Service]\n"
                    f"ExecStart='{aliases['exec-single']}/.venv/bin/python' -m factory dispatch\n"
                    f"Environment='LEGACY=%h/{specifier_value}/.venv'\n"
                )

                agents = self.home / "Library/LaunchAgents"
                agents.mkdir(parents=True, exist_ok=True)
                (agents / f"com.example.factory.{suffix}.plist").write_bytes(plistlib.dumps({
                    "Label": f"com.example.factory.{suffix}",
                    "ProgramArguments": [str(aliases["plist"] / ".venv/bin/python")],
                }))
                config = self.config_home / "factory/config.toml"
                config.parent.mkdir(parents=True, exist_ok=True)
                with config.open("a") as stream:
                    stream.write(f'{suffix}_legacy = "{aliases["config"]}/.venv"\n')
                self.tool_dir.mkdir(parents=True, exist_ok=True)
                receipt = self.tool_dir / "factory/uv-receipt.toml"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                with receipt.open("a") as stream:
                    stream.write(f'{suffix}_legacy = "{aliases["receipt"]}/.venv"\n')

                engine_cleanup.cleanup(
                    {}, run_cmd=self.remote_runner() if remote else None)

                for name, path in referenced.items():
                    self.assertTrue(path.exists(), name)
                    self.assertIn(str(path), self.output.getvalue())
                self.assertFalse(unused.exists())
                self.assertNotIn("reference scan incomplete", self.output.getvalue())

    def test_complete_plist_path_preserves_literal_quote_and_closing_parenthesis(self):
        target = self.leftover("literal-plist")
        alias = self.root / "Engine Alias O'Brien)"
        alias.symlink_to(target, target_is_directory=True)
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "factory-literal.plist").write_bytes(plistlib.dumps({
            "Label": "factory-literal", "WorkingDirectory": str(alias),
        }))
        engine_cleanup.cleanup({})
        self.assertTrue(target.exists())
        self.assertIn(str(target), self.output.getvalue())

    def test_utf8_byte_escapes_resolve_complete_systemd_aliases(self):
        unit_dir = self.config_home / "systemd/user"
        unit_dir.mkdir(parents=True)
        for index, escape in enumerate((r"\xc3\xa9", r"\303\251")):
            target = self.leftover(f"utf8-{index}")
            alias = self.root / f"Engine Café Alias {index}"
            alias.symlink_to(target, target_is_directory=True)
            encoded = str(alias).replace("é", escape)
            unit = unit_dir / f"factory-utf8-{index}.service"
            unit.write_text(f'[Service]\nExecStart="{encoded}/bin/python" -m factory dispatch\n')
            engine_cleanup.cleanup({})
            self.assertTrue(target.exists(), escape)
            self.assertIn(str(target), self.output.getvalue())

    def test_literal_home_markers_in_quoted_complete_paths_are_not_lost(self):
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        for remote in (False, True):
            for index, parent in enumerate(('$HOME', '${HOME}', '~')):
                with self.subTest(remote=remote, parent=parent):
                    target = self.leftover(f"home-marker-{remote}-{index}")
                    directory = self.root / str(remote) / parent
                    directory.mkdir(parents=True, exist_ok=True)
                    alias = directory / 'Engine "Quoted" Alias'
                    alias.symlink_to(target, target_is_directory=True)
                    (agents / f"factory-marker-{remote}-{index}.plist").write_bytes(plistlib.dumps({
                        "Label": f"factory-marker-{remote}-{index}",
                        "WorkingDirectory": str(alias),
                    }))
                    engine_cleanup.cleanup({}, run_cmd=self.remote_runner() if remote else None)
                    self.assertTrue(target.exists())
                    self.assertIn(str(target), self.output.getvalue())

    def test_malformed_systemd_quoting_and_escapes_block_all_deletion(self) -> None:
        for remote in (False, True):
            with self.subTest(remote=remote):
                suffix = "remote" if remote else "local"
                first = self.leftover(f"{suffix}-first")
                second = self.leftover(f"{suffix}-second", historical=True)
                alias = self.alias(f"{suffix}-broken", first)
                unit_dir = self.config_home / "systemd/user"
                unit_dir.mkdir(parents=True, exist_ok=True)
                unit = unit_dir / f"factory-{suffix}-broken.service"
                unit.write_text(
                    "[Unit]\n"
                    "Description=broken quoting\n"
                    "[Service]\n"
                    f'ExecStart="{alias}/.venv/bin/python -m factory dispatch\n'
                )
                drop_in = unit_dir / f"factory-{suffix}-broken.service.d"
                drop_in.mkdir()
                fragment = drop_in / "override.conf"
                bad_escape = str(alias).replace("Engine Alias", r"Engine\qAlias")
                fragment.write_text(
                    "[Service]\n"
                    f"WorkingDirectory={bad_escape}\n"
                )
                nul_unit = unit_dir / f"factory-{suffix}-nul.service"
                nul_unit.write_text(
                    "[Service]\n"
                    r"WorkingDirectory=/tmp/Engine\x00Alias" "\n"
                )

                engine_cleanup.cleanup(
                    {}, run_cmd=self.remote_runner() if remote else None)

                self.assertTrue(first.exists())
                self.assertTrue(second.exists())
                output = self.output.getvalue()
                self.assertIn("reference scan incomplete", output)
                self.assertIn(f"{unit}:4", output)
                self.assertIn("unterminated", output)
                self.assertIn(f"{fragment}:2", output)
                self.assertIn("invalid escape", output)
                self.assertIn(f"{nul_unit}:2", output)
                self.assertIn("NUL is not allowed", output)


if __name__ == "__main__":
    unittest.main()
