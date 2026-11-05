from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import apply, doctor, host, status
from district.dashboard_read import safe_fleet
from tests.remote_transport import remote_command

OLD = "1" * 40
RECORDED = "2" * 40
LATEST = "9" * 40
ORIGIN = "https://github.com/acme/custom-factory"
TAG = "v9.1.0"


def receipt(sha: str) -> str:
    return f'[[tool.requirements]]\nname = "factory"\nrev = "{sha}"\n'


class ReleaseRunner(host.RemoteRunner):
    def __init__(self, name: str, installed: str | None):
        super().__init__(name, f"factory@{name}.example")
        self.installed = installed
        self.calls: list[tuple[list[str], dict]] = []

    def facts(self, *, timeout: float = 10) -> dict:
        return {"tool_dir": f"/srv/{self.name}/tools", "tool_bin": f"/srv/{self.name}/bin"}

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[:2] == ["gh", "api"] and any("repos/acme/custom-factory/releases" in arg for arg in argv):
            releases = [
                {"tag_name": "v8.0.0", "draft": False, "prerelease": False,
                 "published_at": "2026-09-01T00:00:00Z"},
                {"tag_name": TAG, "draft": False, "prerelease": False,
                 "published_at": "2026-10-01T00:00:00Z"},
                {"tag_name": "v9.2.0-rc1", "draft": False, "prerelease": True,
                 "published_at": "2026-11-01T00:00:00Z"},
                {"tag_name": "v10.0.0", "draft": True, "prerelease": False,
                 "published_at": "2026-11-02T00:00:00Z"},
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps([releases]), "")
        if argv[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(
                argv, 0, f"{LATEST}\trefs/tags/{TAG}\n{LATEST}\trefs/tags/{TAG}^{{}}\n", "")
        if argv and argv[0] == "python3" and argv[-1].endswith("uv-receipt.toml"):
            if self.installed is None:
                return subprocess.CompletedProcess(argv, 1, "", "receipt unavailable")
            return subprocess.CompletedProcess(argv, 0, receipt(self.installed), "")
        raise AssertionError(f"unexpected command for {self.name}: {argv}")


class StatusReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "UV_TOOL_DIR": str(self.root / "tools"),
            "UV_TOOL_BIN_DIR": str(self.root / "bin"),
        }))
        self.enterContext(mock.patch.object(
            doctor, "gh_auth", return_value=doctor.row("PASS", "gh auth", "authenticated")))
        self.enterContext(mock.patch.object(status, "fleet", return_value={}))
        self.enterContext(mock.patch.object(doctor, "parity_by_slug", return_value={}))
        self.runners: dict[str, ReleaseRunner] = {}
        self.enterContext(mock.patch.object(
            host, "remote_runner", side_effect=lambda name, _: self.runners[name]))

    def local_receipt(self, sha: str) -> None:
        tool, _ = host.factory_tool_paths()
        tool.mkdir(parents=True)
        (tool / "uv-receipt.toml").write_text(receipt(sha))

    def invoke(self, data: dict, *argv: str):
        output = io.StringIO()
        with mock.patch.object(host, "load", return_value=data), contextlib.redirect_stdout(output):
            code = status.main(list(argv))
        return code, output.getvalue()

    def test_host_release_metadata_does_not_hide_or_truncate_repository_projections(self) -> None:
        for count in (64, 65):
            with self.subTest(count=count):
                entries = {f"acme/repo-{index}": {} for index in range(count)}
                fleet = safe_fleet({"engines": {"ultron": {"assessment": "attention"}}, **entries})
                self.assertEqual(set(fleet), set(list(entries)[:64]))
                for entry in fleet.values():
                    self.assertEqual(entry["projection"]["omitted"]["factories"], count - 64)

    def test_every_registered_host_reports_release_drift_independent_of_repositories(self) -> None:
        data = {
            "defaults": {"factory_repo": ORIGIN,
                         "engine": {"sha": RECORDED, "ref": "v8.0.0"}},
            "hosts": {
                "ultron": {"ssh": "factory@ultron.example", "platform": "macos",
                            "engine": {"sha": OLD, "ref": "v8.0.0"}},
                "current-unassigned": {"ssh": "factory@current.example", "platform": "linux",
                                       "engine": {"sha": LATEST, "ref": TAG}},
                "empty-unassigned": {"ssh": "factory@empty.example", "platform": "linux"},
            },
        }
        self.local_receipt(LATEST)  # The receipt, not stale recorded state, is installed identity.
        self.runners = {
            "ultron": ReleaseRunner("ultron", OLD),
            "current-unassigned": ReleaseRunner("current-unassigned", LATEST),
            "empty-unassigned": ReleaseRunner("empty-unassigned", None),
        }
        local = ReleaseRunner("local", None)

        with mock.patch.object(status, "run", side_effect=local):
            code, output = self.invoke(data, "--json", "--unhealthy")

        self.assertEqual(code, 1, output)
        document = json.loads(output)
        self.assertEqual(set(document["engines"]), {
            "local", "ultron", "current-unassigned", "empty-unassigned"})
        self.assertEqual(document["engines"]["local"]["installed"]["sha"], LATEST)
        self.assertEqual(document["engines"]["local"]["assessment"], "normal")
        self.assertEqual(document["engines"]["ultron"]["installed"]["sha"], OLD)
        self.assertEqual(document["engines"]["ultron"]["latest"], {"tag": TAG, "sha": LATEST})
        self.assertEqual(document["engines"]["ultron"]["assessment"], "attention")
        self.assertEqual(document["engines"]["current-unassigned"]["assessment"], "normal")
        self.assertEqual(document["engines"]["empty-unassigned"]["assessment"], "unknown")

        with mock.patch.object(status, "run", side_effect=local):
            text_code, text = self.invoke(data, "--unhealthy")
        self.assertEqual(text_code, 1, text)
        for expected in ("ultron", OLD[:12], TAG, "empty-unassigned", "unknown"):
            self.assertIn(expected, text)

    def test_release_lookup_failure_is_unknown_and_uses_recorded_install_fallback(self) -> None:
        data = {"defaults": {"factory_repo": ORIGIN,
                             "engine": {"sha": RECORDED, "ref": "v8.0.0"}}}
        with mock.patch.object(apply, "latest_factory_release", side_effect=OSError("release API offline")):
            code, output = self.invoke(data, "--json")

        self.assertEqual(code, 2, output)
        engine = json.loads(output)["engines"]["local"]
        self.assertEqual(engine["installed"], {"sha": RECORDED, "ref": "v8.0.0"})
        self.assertEqual(engine["assessment"], "unknown")
        self.assertIn("release API offline", engine["error"])
        self.assertNotEqual(engine.get("assessment"), "normal")

    def test_local_repo_without_recorded_or_receipted_engine_is_unknown(self) -> None:
        data = {"defaults": {"factory_repo": ORIGIN},
                "repo": {"acme/widgets": {"path": str(self.root / "widgets")}}}
        local = ReleaseRunner("local", None)
        with mock.patch.object(status, "run", side_effect=local):
            code, output = self.invoke(data, "--json")

        self.assertEqual(code, 2, output)
        engine = json.loads(output)["engines"]["local"]
        self.assertEqual(engine["installed"], {"sha": None, "ref": None})
        self.assertEqual(engine["assessment"], "unknown")
        self.assertIn("installed", engine["error"])

    def test_recorded_tag_without_sha_can_report_release_drift(self) -> None:
        local = ReleaseRunner("local", None)
        for installed, expected in (("v8.0.0", "attention"), (TAG, "normal")):
            with self.subTest(installed=installed), mock.patch.object(status, "run", side_effect=local):
                code, output = self.invoke(
                    {"defaults": {"factory_repo": ORIGIN, "engine": {"ref": installed}}}, "--json")
                self.assertEqual(code, 1 if expected == "attention" else 0, output)
                self.assertEqual(json.loads(output)["engines"]["local"]["assessment"], expected)

    def test_remote_receipt_probes_keep_status_json_parseable(self) -> None:
        def transport(argv, **kwargs):
            command = remote_command(argv)
            value = (receipt(OLD) if "uv-receipt.toml" in command else
                     json.dumps({"tool_dir": "/remote/tools", "tool_bin": "/remote/bin"}))
            return subprocess.CompletedProcess(argv, 0, value, "")

        self.runners["ultron"] = host.RemoteRunner("ultron", "fake", transport=transport)
        data = {"defaults": {"factory_repo": ORIGIN},
                "hosts": {"ultron": {"ssh": "fake", "platform": "macos"}}}
        with mock.patch.object(status, "run", side_effect=ReleaseRunner("local", None)):
            code, output = self.invoke(data, "--json")
        self.assertEqual(code, 1, output)
        self.assertEqual(json.loads(output)["engines"]["ultron"]["installed"]["sha"], OLD)

    def test_unreachable_host_does_not_claim_recorded_current_engine_is_observed(self) -> None:
        runner = ReleaseRunner("ultron", LATEST)
        self.runners["ultron"] = runner
        data = {"defaults": {"factory_repo": ORIGIN},
                "hosts": {"ultron": {"ssh": "fake", "platform": "macos",
                                     "engine": {"sha": LATEST, "ref": TAG}}}}
        with mock.patch.object(runner, "facts", side_effect=host.RemoteHostError("ultron", "offline")), \
                mock.patch.object(status, "run", side_effect=ReleaseRunner("local", None)):
            code, output = self.invoke(data, "--json")
        self.assertEqual(code, 2, output)
        engine = json.loads(output)["engines"]["ultron"]
        self.assertEqual(engine["assessment"], "unknown")
        self.assertIn("offline", engine["error"])


if __name__ == "__main__":
    unittest.main()
