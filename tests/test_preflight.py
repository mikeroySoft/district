"""Onboarding refuses scheduler activation when its own PATH lacks a tool."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

from district import doctor, host
from tests.test_district import DistrictCase


class SchedulerPreflightTest(DistrictCase):
    def test_local_doctor_renders_the_same_normalized_path_as_onboarding(self) -> None:
        repo = self.repo(toml="")
        data = {"repo": {"acme/widgets": {"path": str(repo)}}}
        self.stub("gh", ("--version", "gh real-install\n"))
        wrapper = Path.home() / ".local/bin"
        wrapper.mkdir(parents=True)
        (wrapper / "gh").write_text("#!/bin/sh\necho wrapper-version\n")
        (wrapper / "gh").chmod(0o755)
        tools = Path.home() / ".cargo/bin"
        tools.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"PATH": f"{wrapper}:{os.environ['PATH']}:{tools}"}):
            rows = doctor.toolchain_rows(data)
        gh = next(item for item in rows if item["label"].endswith("scheduler tool gh"))
        self.assertEqual(gh["status"], "PASS", gh)
        self.assertIn(str(self.bin / "gh"), gh["detail"])
        self.assertIn("gh real-install", gh["detail"])

    def test_local_add_checks_local_host_when_triage_is_remote(self) -> None:
        repo = self.repo(toml="")
        host.save({
            "hosts": {"ultron": {"ssh": "factory@ultron.example", "platform": "macos"}},
            "repo": {"acme/widgets": {
                "path": str(repo), "hosts": ["local", "ultron"], "triage_host": "ultron",
            }},
        })
        with mock.patch.object(host, "remote_runner", side_effect=AssertionError("local onboarding contacted remote host")):
            code, output = self.district("add", str(repo), "--host", "local")
        self.assertEqual(code, 0, output)
        self.assertIn("install", self.calls("factory"))

    def test_local_add_preflights_rendered_systemd_path_before_install(self) -> None:
        repo = self.repo(toml='[[gate.check]]\nname = "site-tests"\nrun = ["node", "test.mjs"]\n')
        scheduler = self.tmp / "systemd-bin"
        scheduler.mkdir()
        for binary in ("factory", "gh", "git", "uv"):
            executable = scheduler / binary
            executable.write_text("#!/bin/sh\necho fixture-version\n")
            executable.chmod(0o755)
        (scheduler / "python3").symlink_to(sys.executable)
        rendered = (f"# factory-widgets.service\n[Service]\nEnvironment=PATH={scheduler}\n"
                    f"Environment=HOME={Path.home()}\nExecStart={sys.executable} -m factory dispatch\n")
        cases_path = self.bin / "factory.json"
        cases_path.write_text(json.dumps([
            ["install --print", rendered, 0], *json.loads(cases_path.read_text()),
        ]))

        code, output = self.district("add", str(repo))

        self.assertEqual(code, 1, output)
        for text in ("local", "node", "site-tests", str(scheduler)):
            self.assertIn(text, output)
        self.assertNotIn("install", self.calls("factory"))
        self.assertFalse(any(call.startswith(("enable ", "start ", "daemon-reload"))
                             for call in self.calls("systemctl")))

        node = scheduler / "node"
        node.write_text("#!/bin/sh\necho node-version\n")
        node.chmod(0o755)
        code, output = self.district("add", str(repo))
        self.assertEqual(code, 0, output)
        self.assertIn("install", self.calls("factory"))

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
                f'printf "%s|%s|%s|%s\\n" "{binary}" "$HOME" "$PATH" "${{DISTRICT_CONTROLLER_SECRET-unset}}" >> {str(self.tmp / "probe-env.log")!r}\n'
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

    def test_remote_add_reports_missing_review_and_manager_binary(self) -> None:
        repo, _, path = self.remote_factory(missing="omp")
        code, output = self.district("add", str(repo), "--host", "ultron")
        self.assertEqual(code, 1, output)
        for text in ("ultron", "omp", "defaults.review", "defaults.manager", path):
            self.assertIn(text, output)
        self.assertNotIn("install", self.calls("factory"))

    def test_remote_add_honors_host_command_overrides_and_unsets(self) -> None:
        repo, _, _ = self.remote_factory()
        data = host.load()
        data["defaults"]["review"]["command"] = ["controller-only-review"]
        data["defaults"]["manager"]["command"] = ["controller-only-manager"]
        data["hosts"]["ultron"]["defaults"] = {"review": {"command": ["omp", "review"]}}
        data["hosts"]["ultron"]["defaults_unset"] = ["manager.command"]
        host.save(data)
        code, output = self.district("add", str(repo), "--host", "ultron")
        self.assertEqual(code, 0, output)
        self.assertNotIn("controller-only", output)

    def test_remote_add_uses_owning_home_when_render_omits_home(self) -> None:
        repo, home, path = self.remote_factory()
        factory = self.bin / "factory"
        factory.write_text(factory.read_text().replace(f', "HOME": {str(home)!r}', ""))
        code, output = self.district("add", str(repo), "--host", "ultron")
        self.assertEqual(code, 0, output)
        observations = (self.tmp / "probe-env.log").read_text().splitlines()
        self.assertTrue(observations)
        self.assertTrue(all(line.split("|")[1:] == [str(home), path, "unset"] for line in observations), observations)

    def test_remote_add_version_probe_failure_warns_without_blocking(self) -> None:
        repo, _, path = self.remote_factory()
        (Path(path) / "xcodebuild").write_text("#!/bin/sh\necho unavailable-version >&2\nexit 7\n")
        code, output = self.district("add", str(repo), "--host", "ultron")
        self.assertEqual(code, 0, output)
        self.assertIn("WARN host ultron scheduler tool xcodebuild", output)
        self.assertIn("unavailable-version", output)

    def test_reset_missing_gate_tool_keeps_timer_disabled(self) -> None:
        _, _, path = self.remote_factory(missing="node")
        data = host.load()
        data["repo"]["acme/widgets"]["disabled_at"] = "2026-11-01T00:00:00Z"
        host.save(data)
        code, output = self.district("apply", "--reset", "acme/widgets")
        self.assertEqual(code, 1, output)
        self.assertIn("disabled_at", host.load()["repo"]["acme/widgets"])
        self.assertTrue(all(value in output for value in ("ultron", "node", "site-tests", path)), output)
        self.assertFalse(any(call.startswith(("bootstrap ", "kickstart ")) for call in self.calls("launchctl")))
        self.assertNotIn("install", self.calls("factory"))

    def test_remote_apply_complete_toolchain_uses_only_scheduler_environment(self) -> None:
        _, home, path = self.remote_factory()
        with mock.patch.dict(os.environ, {"DISTRICT_CONTROLLER_SECRET": "must-not-leak"}):
            code, output = self.district("apply", "acme/widgets")

        self.assertEqual(code, 0, output)
        self.assertIn("install", self.calls("factory"))
        self.assertTrue((home / "Library/LaunchAgents/factory-widgets.plist").exists())
        observations = (self.tmp / "probe-env.log").read_text().splitlines()
        # Existing post-install smoke also probes gh/factory, outside onboarding preflight.
        isolated = [line.split("|", 1)[1] for line in observations if not line.startswith(("gh|", "factory|"))]
        self.assertTrue(isolated)
        self.assertEqual(set(isolated), {f"{home}|{path}|unset"})

    def test_doctor_reruns_preflight_for_registered_remote_factory(self) -> None:
        _, _, path = self.remote_factory(missing="node")

        code, output = self.district("doctor", "--json")

        self.assertEqual(code, 1, output)
        report = json.loads(output)
        failures = [item for item in report["rows"] if item["status"] == "FAIL"]
        self.assertTrue(any(all(text in item["label"] + item["detail"]
                                for text in ("ultron", "node", "site-tests", path))
                            for item in failures), failures)
        self.assertNotIn("install", self.calls("factory"))
