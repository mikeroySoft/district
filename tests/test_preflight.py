"""Onboarding refuses scheduler activation when its own PATH lacks a tool."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

from district import host
from test_district import DistrictCase


class SchedulerPreflightTest(DistrictCase):
    def remote_factory(self, *, missing: str | None = None) -> tuple[Path, Path, str]:
        target = "factory@ultron.example"
        home = self.fake_ssh(target)[target]
        scheduler = self.tmp / "scheduler bin"
        scheduler.mkdir()
        for binary in ("factory", "gh", "git", "uv", "cargo", "xcodegen", "swift", "xcodebuild", "omp", "node"):
            if binary == missing:
                continue
            executable = scheduler / binary
            flag = "-version" if binary == "xcodebuild" else "--version"
            executable.write_text(
                '#!/bin/sh\n'
                f'test "$1" = "{flag}" || exit 9\n'
                f'printf "%s|%s|%s\\n" "$HOME" "$PATH" "${{DISTRICT_CONTROLLER_SECRET-unset}}" >> {str(self.tmp / "probe-env.log")!r}\n'
                f'echo "{binary} fixture version"\n'
            )
            executable.chmod(0o755)
        (scheduler / "python3").symlink_to(sys.executable)
        # The rendered plist deliberately differs from both controller and SSH PATH.
        factory = self.bin / "factory"
        factory.write_text(factory.read_text().replace(
            '"EnvironmentVariables": {"PATH": os.environ["PATH"]}',
            f'"EnvironmentVariables": {{"PATH": {str(scheduler)!r}, "HOME": {str(home)!r}}}',
        ))
        repo = self.repo(toml='[[gate.check]]\nname = "site-tests"\nrun = ["node", "site/tests/join.test.mjs"]\n')
        host.save({
            "defaults": {"review": {"command": ["omp", "review"]}, "manager": {"command": ["omp", "manage"]}},
            "hosts": {"ultron": {"ssh": target, "platform": "macos"}},
            "repo": {"acme/widgets": {
                "path": str(repo), "host": "ultron", "toolchain": "macos-xcode", "dashboard": {"port": 8765},
            }},
        })
        return repo, home, str(scheduler)

    def test_remote_add_missing_xcodegen_fails_before_loading(self) -> None:
        repo, home, path = self.remote_factory(missing="xcodegen")
        self.stub("xcodegen", ("--version", "controller-only xcodegen"))

        code, output = self.district("add", str(repo), "--host", "ultron")

        self.assertEqual(code, 1, output)
        for text in ("ultron", "xcodegen", "macos-xcode", path):
            self.assertIn(text, output)
        self.assertNotIn("install", self.calls("factory"))
        self.assertFalse(any("bootstrap" in call for call in self.calls("launchctl")))
        self.assertFalse((home / "Library/LaunchAgents/factory-widgets.plist").exists())

    def test_remote_apply_missing_gate_tool_fails_before_loading(self) -> None:
        _, home, path = self.remote_factory(missing="node")

        code, output = self.district("apply", "acme/widgets")

        self.assertEqual(code, 1, output)
        for text in ("ultron", "node", "site-tests", path):
            self.assertIn(text, output)
        self.assertNotIn("install", self.calls("factory"))
        self.assertFalse(any("bootstrap" in call for call in self.calls("launchctl")))
        self.assertFalse((home / "Library/LaunchAgents/factory-widgets.plist").exists())

    def test_remote_apply_complete_toolchain_uses_only_scheduler_environment(self) -> None:
        _, home, path = self.remote_factory()
        with mock.patch.dict(os.environ, {"DISTRICT_CONTROLLER_SECRET": "must-not-leak"}):
            code, output = self.district("apply", "acme/widgets")

        self.assertEqual(code, 0, output)
        self.assertIn("install", self.calls("factory"))
        self.assertTrue((home / "Library/LaunchAgents/factory-widgets.plist").exists())
        observations = (self.tmp / "probe-env.log").read_text().splitlines()
        self.assertTrue(observations)
        self.assertEqual(set(observations), {f"{home}|{path}|unset"})

    def test_doctor_reruns_preflight_for_registered_remote_factory(self) -> None:
        _, _, path = self.remote_factory(missing="node")

        code, output = self.district("doctor", "--json")

        self.assertEqual(code, 1, output)
        # Commands may be logged before the JSON report.
        report = json.loads(output[output.index('{"ok":'):])
        failures = [item for item in report["rows"] if item["status"] == "FAIL"]
        self.assertTrue(any(all(text in item["label"] + item["detail"]
                                for text in ("ultron", "node", "site-tests", path))
                            for item in failures), failures)
        self.assertNotIn("install", self.calls("factory"))
