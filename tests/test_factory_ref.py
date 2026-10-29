"""Factory ref reconciliation stays pinned, CI-gated, and optional."""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import apply, host


SHA = "a" * 40
OLD = "b" * 40
ORIGIN = "https://github.com/mikeroySoft/factory.git"


class FactoryRefTest(unittest.TestCase):
    def data(self, *, ref: str | None = "main", sha: str | None = OLD) -> dict:
        defaults = {"factory_source": "/factory"}
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
              active_timers: list[str] | None = None, backend: mock.Mock | None = None):
        calls: list[list[str]] = []
        if backend is None:
            # Never let the default backend reach the real systemctl/launchctl.
            backend = mock.Mock()
            backend.status.return_value.loaded = False

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:4] == ["git", "remote", "get-url", "origin"]:
                return self.proc(argv, ORIGIN + "\n")
            if argv[:3] == ["git", "ls-remote", "--tags"] and remote_refs is not None:
                return self.proc(argv, remote_refs)
            if argv[:2] == ["git", "ls-remote"] and pinned_refs is not None:
                return self.proc(argv, pinned_refs)
            if argv[:2] == ["git", "ls-remote"]:
                code = 2 if remote_refs is not None else resolve_code
                return self.proc(argv, f"{SHA}\trefs/heads/main\n", code, "network down")
            if argv[:2] == ["gh", "api"]:
                return self.proc(argv, checks)
            return self.proc(argv)

        document = data or self.data()
        active = set(active_timers or [])
        backend.is_active.side_effect = lambda unit: "active" if unit in active else "inactive"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            with mock.patch.dict("os.environ", env), \
                    mock.patch.object(Path, "is_dir", return_value=True), \
                    mock.patch.object(apply, "run", side_effect=run), \
                    mock.patch.object(host, "save"):
                self.assertTrue(host.path().is_relative_to(root), host.path())
                result = apply.reconcile_engine(document, backend=backend)
        return result, calls, document

    def test_same_sha_does_not_install(self) -> None:
        result, calls, _ = self.calls("", data=self.data(sha=SHA))
        self.assertEqual(result, "unchanged")
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))
        self.assertIn(f"refs/heads/{self.data()['defaults']['factory_ref']}", calls[1])

    def test_non_green_ci_skips_install_with_reason(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"failure"}]}'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result, calls, document = self.calls(checks)
        self.assertEqual(result, "unchanged")
        self.assertIn(f"engine {SHA[:12]} CI failed", out.getvalue())
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

    def test_latest_odd_same_sha_skips_checks_and_install(self) -> None:
        peeled = "9" * 40
        tags = (
            f"{'4' * 40}\trefs/tags/v0.4.9-rc.1\n"
            f"{peeled}\trefs/tags/v0.4.9-rc.1^{{}}\n"
        )

        result, calls, _ = self.calls(
            "", data=self.data(ref="latest-odd", sha=peeled), remote_refs=tags)

        self.assertEqual(result, "unchanged")
        self.assertFalse(any(call[:2] == ["gh", "api"] for call in calls))
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))

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

    def test_install_restarts_installed_dashboard(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'
        backend = mock.Mock()
        backend.status.return_value.loaded = True
        self.calls(checks, backend=backend)
        backend.start.assert_called_once_with(
            f"{host.unit_name('acme/widgets')}-dashboard.service", restart=True, check=False)

    def test_partial_timer_disable_failure_still_restores(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'
        timers = ["factory-one.timer", "factory-two.timer"]
        data = self.data()
        data["repo"] = {"acme/one": {"path": "/one"}, "acme/two": {"path": "/two"}}
        backend = mock.Mock()
        backend.enable.side_effect = [self.proc([], returncode=0), host.DistrictError("disable failed")]
        with mock.patch.object(apply, "restore") as restore:
            with self.assertRaises(host.DistrictError):
                self.calls(checks, data=data, active_timers=timers, backend=backend)
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args[0], timers)

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
