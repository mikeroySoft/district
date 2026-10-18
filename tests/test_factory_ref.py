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

    def calls(self, checks: str, *, data: dict | None = None):
        calls: list[list[str]] = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:4] == ["git", "remote", "get-url", "origin"]:
                return self.proc(argv, ORIGIN + "\n")
            if argv[:2] == ["git", "ls-remote"]:
                return self.proc(argv, f"{SHA}\trefs/heads/main\n")
            if argv[:2] == ["gh", "api"]:
                return self.proc(argv, checks)
            return self.proc(argv)

        document = data or self.data()
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}), \
                mock.patch.object(apply, "run", side_effect=run), \
                mock.patch.object(apply, "wait_inactive"):
            result = apply.reconcile_engine(document, document["repo"], [])
        return result, calls, document

    def test_same_sha_does_not_install(self) -> None:
        result, calls, _ = self.calls("", data=self.data(sha=SHA))
        self.assertTrue(result)
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))

    def test_non_green_ci_skips_install_with_reason(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"failure"}]}'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result, calls, document = self.calls(checks)
        self.assertTrue(result)
        self.assertIn(f"engine {SHA[:12]} CI failed", out.getvalue())
        self.assertFalse(any(call[:3] == ["uv", "tool", "install"] for call in calls))
        self.assertEqual(document["defaults"]["engine"]["sha"], OLD)

    def test_green_sha_installs_once_and_records_sha(self) -> None:
        checks = '{"total_count":1,"check_runs":[{"status":"completed","conclusion":"success"}]}'
        result, calls, document = self.calls(checks)
        installs = [call for call in calls if call[:3] == ["uv", "tool", "install"]
        self.assertTrue(result)
        self.assertEqual(len(installs), 1)
        self.assertIn(f"git+{ORIGIN}@{SHA}", installs[0])
        self.assertEqual(document["defaults"]["engine"]["sha"], SHA)

    def test_absent_factory_ref_does_not_reconcile(self) -> None:
        document = self.data(ref=None)
        with mock.patch.object(apply, "run") as run:
            self.assertTrue(apply.reconcile_engine(document, document["repo"], []))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
