"""Host-side configuration and uv-tool paths for the one-install engine."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from district import host


class EngineHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }, clear=True))

    def test_defaults_have_no_checkout_and_use_canonical_engine_policy(self) -> None:
        self.assertEqual(host.DEFAULTS["clone_dir"], "~/dev")
        self.assertEqual(host.DEFAULTS["factory_repo"], "https://github.com/mikeroySoft/factory")
        self.assertEqual(host.DEFAULTS["engine_idle_timeout"], "30min")
        self.assertNotIn("factory_source", host.DEFAULTS)

    def test_factory_tool_paths_follow_uv_environment_precedence(self) -> None:
        home = self.root / "home"
        cases = (
            ({}, home / ".local/share/uv/tools/factory", home / ".local/bin/factory"),
            ({"XDG_DATA_HOME": str(self.root / "data"),
              "XDG_BIN_HOME": str(self.root / "xdg-bin")},
             self.root / "data/uv/tools/factory", self.root / "xdg-bin/factory"),
            ({"XDG_DATA_HOME": str(self.root / "data"),
              "XDG_BIN_HOME": str(self.root / "xdg-bin"),
              "UV_TOOL_DIR": str(self.root / "tools"),
              "UV_TOOL_BIN_DIR": str(self.root / "bin")},
             self.root / "tools/factory", self.root / "bin/factory"),
        )
        for extra, expected_tool, expected_cli in cases:
            with self.subTest(extra=extra), mock.patch.dict(
                os.environ,
                {"HOME": str(home), "XDG_CONFIG_HOME": str(self.root / "config"), **extra},
                clear=True,
            ):
                self.assertEqual(host.factory_tool_paths(), (expected_tool, expected_cli))

    def test_remote_facts_resolve_remote_uv_environment_without_controller_leaks(self) -> None:
        remote = self.root / "remote"
        remote_env = {
            "HOME": str(remote / "home"),
            "PATH": os.defpath,
            "XDG_CONFIG_HOME": str(remote / "config"),
            "XDG_DATA_HOME": str(remote / "data"),
            "XDG_BIN_HOME": str(remote / "xdg-bin"),
            "UV_TOOL_DIR": str(remote / "tools"),
            "UV_TOOL_BIN_DIR": str(remote / "bin"),
        }

        def transport(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["sh", "-c", argv[-1]], capture_output=True, text=True,
                env=remote_env, input=kwargs.get("input"), check=False,
            )

        runner = host.RemoteRunner("remote", "factory@example", transport=transport)
        facts = runner.facts()
        self.assertEqual(facts["data_home"], str(remote / "data"))
        self.assertEqual(facts["tool_dir"], str(remote / "tools"))
        self.assertEqual(facts["tool_bin"], str(remote / "bin"))
        self.assertEqual(host.factory_tool_paths(runner), (
            remote / "tools/factory", remote / "bin/factory",
        ))
        self.assertNotIn(str(self.root / "config"), json.dumps(facts))

    def test_remote_tool_paths_preserve_configured_alias_for_process_matching(self) -> None:
        real = self.root / "real-tools"
        real.mkdir()
        alias = self.root / "tool-link"
        alias.symlink_to(real, target_is_directory=True)
        remote_env = {"HOME": str(self.root / "remote-home"), "PATH": os.defpath,
                      "UV_TOOL_DIR": str(alias), "UV_TOOL_BIN_DIR": str(self.root / "bin")}

        def transport(argv, **kwargs):
            return subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True,
                                  env=remote_env, input=kwargs.get("input"), timeout=kwargs.get("timeout"))

        runner = host.RemoteRunner("remote", "unused", transport=transport)
        self.assertEqual(host.factory_tool_paths(runner),
                         (alias / "factory", self.root / "bin/factory"))

    def test_save_migrates_every_legacy_source_and_backs_up_disk_bytes(self) -> None:
        config = host.path()
        config.parent.mkdir(parents=True)
        original = """\
[defaults]
factory_source = "/old/default"

[hosts.ultron]
ssh = "factory@example"
platform = "linux"
factory_source = "/old/host"

[hosts.ultron.defaults]
factory_source = "/old/nested"

[hosts.ultron.defaults.engine]
target = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""
        config.write_text(original)
        data = host.load()

        host.save(data)

        backups = list(config.parent.glob("config.toml.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertNotIn("factory_source", data["defaults"])
        self.assertNotIn("factory_source", data["hosts"]["ultron"])
        self.assertNotIn("factory_source", data["hosts"]["ultron"]["defaults"])
        self.assertNotIn("factory_source", config.read_text())
        self.assertEqual(host.load()["hosts"]["ultron"]["defaults"]["engine"]["target"], "a" * 40)

    def test_initial_save_strips_legacy_input_without_inventing_a_backup(self) -> None:
        data = {
            "defaults": {"factory_source": "/old/default"},
            "hosts": {"ultron": {
                "factory_source": "/old/host",
                "defaults": {"factory_source": "/old/nested"},
            }},
        }

        host.save(data)

        self.assertEqual(data, {"defaults": {}, "hosts": {"ultron": {"defaults": {}}}})
        self.assertEqual(list(host.path().parent.glob("config.toml.*.bak")), [])
        self.assertNotIn("factory_source", host.path().read_text())

    def test_remote_sync_backs_up_legacy_config_and_preserves_target_metadata(self) -> None:
        remote = self.root / "remote"
        config = remote / "config/factory/config.toml"
        config.parent.mkdir(parents=True)
        original = '[defaults]\nfactory_source = "/old/remote"\n'
        config.write_text(original)
        runner = mock.Mock()
        runner.facts.return_value = {"config_home": str(remote / "config")}
        def remote_path(value):
            path = Path(value)
            proxy = mock.Mock()
            proxy.read_bytes.side_effect = lambda **kw: path.read_bytes()
            proxy.write_bytes.side_effect = lambda body, **kw: path.write_bytes(body)
            proxy.write_text.side_effect = lambda body, **kw: path.write_text(body)
            return proxy
        runner.path.side_effect = remote_path
        data = {
            "defaults": {"factory_source": "/old/controller"},
            "hosts": {"ultron": {"engine": {
                "sha": "a" * 40,
                "target": "b" * 40,
                "target_ref": "main",
                "resolved_at": "2026-11-01T12:00:00+00:00",
            }, "defaults": {"factory_source": "/old/nested"}}},
        }

        host.sync_remote_config(data, "ultron", runner)

        backups = list(config.parent.glob("config.toml.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        synced = tomllib.loads(config.read_text())
        self.assertNotIn("factory_source", config.read_text())
        self.assertEqual(synced["defaults"]["engine"]["target"], "b" * 40)
        self.assertEqual(synced["defaults"]["engine"]["target_ref"], "main")


if __name__ == "__main__":
    unittest.main()
