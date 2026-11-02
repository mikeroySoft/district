"""Factory ref reconciliation stays pinned, CI-gated, and optional."""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import apply, host, install_state


SHA = "a" * 40
OLD = "b" * 40
ORIGIN = "https://github.com/mikeroySoft/factory.git"


class FactoryRefTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict("os.environ", {
            "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"), "UV_TOOL_DIR": str(root / "tools"),
            "UV_TOOL_BIN_DIR": str(root / "bin"),
        }))

    def data(self, *, ref: str | None = "main", sha: str | None = OLD) -> dict:
        defaults = {"factory_repo": ORIGIN}
        if ref is not None:
            defaults["factory_ref"] = ref
        if sha is not None:
            defaults["engine"] = {"sha": sha, "ref": "old"}
        return {"defaults": defaults, "repo": {"acme/widgets": {"path": "/repo"}}}

    @staticmethod
    def proc(argv: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def calls(self, checks: str, *, data: dict | None = None, resolve_code: int = 0,
              remote_refs: str | None = None, pinned_refs: str | None = None,
              active_timers: list[str] | None = None, backend: mock.Mock | None = None,
              record: install_state.ApplyRun | None = None):
        calls: list[list[str]] = []
        if backend is None:
            # Never let the default backend reach the real systemctl/launchctl.
            backend = mock.Mock()
            backend.status.return_value.loaded = False

        backend.configuration.return_value = {"platform": "linux"}
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["python3", "-c"] and "/proc/" in argv[2]:
                return self.proc(argv, "{}")
            if argv[:3] == ["git", "ls-remote", "--tags"] and remote_refs is not None:
                return self.proc(argv, remote_refs)
            if argv[:2] == ["git", "ls-remote"] and pinned_refs is not None:
                return self.proc(argv, pinned_refs)
            if argv[:2] == ["git", "ls-remote"]:
                code = 2 if remote_refs is not None else resolve_code
                return self.proc(argv, f"{SHA}\trefs/heads/main\n", code, "network down")
            if argv[:2] == ["gh", "api"]:
                endpoint = argv[-1]
                if "/compare/" in endpoint:
                    return self.proc(argv, '{"status":"ahead","ahead_by":1,"behind_by":0}')
                if "/commits?" in endpoint:
                    return self.proc(argv, "[]")
                return self.proc(argv, checks)
            if argv[:3] == ["uv", "tool", "install"]:
                source = argv[argv.index("--from") + 1]
                receipt(source.rsplit("@", 1)[1].split("[", 1)[0])
            return self.proc(argv)

        document = data or self.data()
        active = set(active_timers or [])
        backend.is_active.side_effect = lambda unit, **kwargs: "active" if unit in active else "inactive"
        backend.factory_units.return_value = set()
        backend.pause_timer.side_effect = backend.enable
        backend.resume_timer.return_value = self.proc([])
        backend.start.return_value = self.proc([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": str(root / "data"),
                "UV_TOOL_DIR": str(root / "tools"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
            }
            tool = root / "tools/factory"
            cli = root / "bin/factory"
            (tool / "bin").mkdir(parents=True)
            (tool / "bin/factory").touch()
            cli.parent.mkdir(parents=True)
            cli.symlink_to(tool / "bin/factory")

            def receipt(sha):
                (tool / "uv-receipt.toml").write_text(
                    f'[tool]\nrequirements = [{{name = "factory", git = "{ORIGIN}?rev={sha}"}}]\n')

            receipt(document.get("defaults", {}).get("engine", {}).get("sha", OLD))
            with mock.patch.dict("os.environ", env), \
                    mock.patch.object(apply, "run", side_effect=run), \
                    mock.patch.object(host, "save"):
                self.assertTrue(host.path().is_relative_to(root), host.path())
                result = apply.reconcile_engine(document, record, backend=backend)
        return result, calls, document

    def test_same_sha_does_not_install(self) -> None:
        result, calls, _ = self.calls("", data=self.data(sha=SHA))
        self.assertEqual(result, "unchanged")
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))
        self.assertTrue(any("refs/heads/main" in call for call in calls))

    def test_non_green_ci_skips_install_with_reason(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"failure"}]}'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result, calls, document = self.calls(checks)
        self.assertEqual(result, "unchanged")
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))
        self.assertEqual(document["defaults"]["engine"]["sha"], OLD)

    def test_green_sha_installs_once_and_records_sha(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'
        result, calls, document = self.calls(checks)
        installs = [call for call in calls if call[:3] == ["uv", "tool", "install"]]
        self.assertEqual(result, "applied")
        self.assertEqual(len(installs), 1)
        self.assertIn(f"git+{ORIGIN}@{SHA}", installs[0])
        self.assertEqual(document["defaults"]["engine"]["sha"], SHA)


    def test_latest_odd_installs_highest_odd_patch_tag(self) -> None:
        peeled = "9" * 40
        tags = "\n".join([
            f"{'6' * 40}\trefs/tags/v0.4.6",
            f"{'1' * 40}\trefs/tags/v0.4.6^{{}}",
            f"{'7' * 40}\trefs/tags/v0.4.7",
            f"{'2' * 40}\trefs/tags/v0.4.7^{{}}",
            f"{'8' * 40}\trefs/tags/v0.4.8",
            f"{'3' * 40}\trefs/tags/v0.4.8^{{}}",
            f"{'4' * 40}\trefs/tags/v0.4.9-rc.1",
            f"{peeled}\trefs/tags/v0.4.9-rc.1^{{}}",
        ]) + "\n"
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'

        result, calls, document = self.calls(
            checks, data=self.data(ref="latest-odd"), remote_refs=tags)

        installs = [call for call in calls if call[:3] == ["uv", "tool", "install"]]
        self.assertEqual(result, "applied")
        self.assertEqual(len(installs), 1)
        self.assertIn(f"git+{ORIGIN}@{peeled}", installs[0])
        self.assertEqual(document["defaults"]["engine"]["ref"], "v0.4.9-rc.1")


    def test_latest_odd_without_odd_tag_keeps_installed_engine(self) -> None:
        tags = (
            f"{'6' * 40}\trefs/tags/v0.4.6\n"
            f"{'8' * 40}\trefs/tags/v0.4.8\n"
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result, calls, document = self.calls(
                "", data=self.data(ref="latest-odd"), remote_refs=tags)

        self.assertEqual(result, "unchanged")
        self.assertIn(
            f"engine latest-odd unresolved: no odd-patch tag on {ORIGIN}; keeping installed engine",
            out.getvalue(),
        )
        self.assertFalse(any(call[:2] == ["gh", "api"] for call in calls))
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))
        self.assertEqual(document["defaults"]["engine"]["sha"], OLD)

    def test_pinned_tag_installs_peeled_commit(self) -> None:
        peeled = "6" * 40
        refs = (
            f"{'5' * 40}\trefs/tags/v0.4.6\n"
            f"{peeled}\trefs/tags/v0.4.6^{{}}\n"
        )
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'

        result, calls, document = self.calls(
            checks, data=self.data(ref="v0.4.6"), pinned_refs=refs)

        installs = [call for call in calls if call[:3] == ["uv", "tool", "install"]]
        self.assertEqual(result, "applied")
        self.assertIn(f"git+{ORIGIN}@{peeled}", installs[0])
        self.assertEqual(document["defaults"]["engine"]["ref"], "v0.4.6")

    def test_latest_odd_prefers_final_release_at_same_version(self) -> None:
        final = "7" * 40
        refs = (
            f"{'6' * 40}\trefs/tags/v1.2.7-rc.2\n"
            f"{final}\trefs/tags/v1.2.7\n"
        )

        self.assertEqual(apply.latest_odd_tag(refs), ("v1.2.7", final))

    def test_engine_reconciliation_records_every_local_repo_timer(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'
        record = install_state.ApplyRun("", "", "")
        with mock.patch.object(install_state.host, "run", return_value=self.proc([])):
            self.calls(checks, record=record)

        self.assertIn("factory-widgets.timer", record.timers_before)

    def test_partial_timer_disable_failure_still_restores(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'
        timers = ["factory-one.timer", "factory-two.timer"]
        data = self.data()
        data["repo"] = {"acme/one": {"path": "/one"}, "acme/two": {"path": "/two"}}
        backend = mock.Mock()
        backend.enable.side_effect = [self.proc([], returncode=0), host.DistrictError("disable failed")]
        with self.assertRaises(host.DistrictError):
            self.calls(checks, data=data, active_timers=timers, backend=backend)
        self.assertEqual([call.args[0] for call in backend.resume_timer.call_args_list], timers)

    def test_unresolvable_ref_keeps_installed_engine(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result, calls, document = self.calls("", resolve_code=2)
        self.assertEqual(result, "unchanged")
        self.assertIn("engine main unresolved: network down; keeping installed engine", out.getvalue())
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))
        self.assertEqual(document["defaults"]["engine"]["sha"], OLD)

    def test_skipped_and_neutral_checks_pass_but_pending_does_not(self) -> None:
        passing = ('{"total_count":2,"check_runs":['
                   '{"status":"completed","conclusion":"skipped"},'
                   '{"status":"completed","conclusion":"neutral"}]}')
        self.assertTrue(any(call[:3] == ["uv", "tool", "install"]
                            for call in self.calls(passing)[1]))
        pending = '{"total_count":1,"check_runs":[{"status":"in_progress","conclusion":null}]}'
        result, calls, _ = self.calls(pending)
        self.assertEqual(result, "unchanged")
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))

    def test_absent_factory_ref_does_not_reconcile(self) -> None:
        document = self.data(ref=None)
        with mock.patch.object(apply, "run") as run:
            self.assertEqual(apply.reconcile_engine(document), "unchanged")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
