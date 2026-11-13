"""Issue #174: upgrades follow published releases unless branch tracking is explicit."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from district import apply, host, install_state
from tests import test_one_install as engine


RELEASES = [
    {"tag_name": "v0.5.0-rc.1", "prerelease": True, "draft": False,
     "published_at": "2026-11-04T20:00:00Z"},
    {"tag_name": "v0.5.0", "prerelease": False, "draft": True,
     "published_at": None},
    {"tag_name": "v0.4.6", "prerelease": False, "draft": False,
     "published_at": "2026-10-01T00:00:00Z"},
    {"tag_name": "v0.4.7", "prerelease": False, "draft": False,
     "published_at": "2026-11-04T00:00:00Z"},
]


class FactoryReleaseTest(unittest.TestCase):
    def setUp(self):
        self.engine = engine.OneInstallTest()
        self.engine.setUp()
        self.addCleanup(self.engine.doCleanups)
        self.data = self.engine.data
        self.data["defaults"]["factory_repo"] = "https://github.com/acme/engine.git"
        self.releases = RELEASES
        self.calls = []
        self.enterContext(mock.patch.object(apply, "run", side_effect=self.run_cmd))
        self.enterContext(mock.patch.object(install_state, "load_releases",
                                           return_value=install_state.unknown_releases()))
        self.enterContext(mock.patch.object(install_state, "tool_versions",
                                           return_value={"district": "test", "factory": "0.4.7"}))

    def run_cmd(self, argv, **kwargs):
        self.calls.append(argv)
        endpoint = next((arg for arg in argv if arg.startswith("repos/")), "")
        if argv[:2] == ["gh", "api"] and "/releases" in endpoint:
            self.assertIn("repos/acme/engine/releases", endpoint)
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.releases]), "")
        if argv[:2] == ["git", "ls-remote"]:
            if "refs/tags/v0.4.7" in argv:
                # Annotated release: the tag object is not the installed commit.
                output = f"{'d' * 40}\trefs/tags/v0.4.7\n{engine.TARGET}\trefs/tags/v0.4.7^{{}}\n"
            else:
                branch = next((arg for arg in argv if arg.startswith("refs/heads/")), "refs/heads/main")
                output = f"{engine.TIP}\t{branch}\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        return self.engine.run_cmd(argv, **kwargs)

    def test_bare_upgrade_uses_latest_published_release_not_configured_branch(self):
        for track_branch in (False, True):
            with self.subTest(track_branch=track_branch):
                self.data["defaults"]["factory_track_branch"] = track_branch
                self.engine.install_receipt(engine.OLD)
                self.assertEqual(apply.upgrade(self.data, backend=self.engine.backend), "applied")
                installed = host.engine(host.load(), "local")
                self.assertEqual((installed["ref"], installed["sha"]), ("v0.4.7", engine.TARGET))
                self.assertFalse(any("refs/heads/main" in argv for argv in self.calls))

    def test_cli_bare_upgrade_without_policy_uses_release(self):
        self.data["defaults"].pop("factory_ref")
        host.save(self.data)
        self.assertEqual(apply.main(["--upgrade"]), 0)
        self.assertEqual(host.engine(host.load(), "local")["ref"], "v0.4.7")

    def test_configured_branch_requires_tracking_opt_in(self):
        for branch in ("main", "topic"):
            for track_branch in (False, True):
                with self.subTest(branch=branch, track_branch=track_branch):
                    self.data["defaults"]["engine"] = {"sha": engine.OLD, "ref": "main"}
                    self.data["defaults"].update(factory_ref=branch, factory_track_branch=track_branch)
                    self.engine.install_receipt(engine.OLD)
                    self.engine.calls.clear()
                    result = apply.reconcile_engine(self.data, backend=self.engine.backend)
                    installed = host.engine(self.data, "local")
                    if track_branch:
                        self.assertEqual(result, "applied")
                        self.assertEqual(installed["ref"], branch)
                        self.assertEqual(installed["sha"], engine.TARGET)  # newest green, not TIP
                    else:
                        self.assertEqual(result, "failed")
                        self.assertEqual(installed["sha"], engine.OLD)
                        self.assertEqual(self.engine.installs(), [])
                        self.assertIn("factory_track_branch", self.engine.output.getvalue())

    def test_explicit_branch_is_opt_in_without_config_marker(self):
        for branch in ("main", "topic"):
            with self.subTest(branch=branch):
                self.data["defaults"]["factory_track_branch"] = False
                self.engine.install_receipt(engine.OLD)
                self.assertEqual(apply.upgrade(self.data, branch, backend=self.engine.backend), "applied")
                self.assertEqual(host.engine(self.data, "local")["ref"], branch)
                self.assertEqual(host.engine(self.data, "local")["sha"], engine.TARGET)

    def test_configured_tag_needs_no_branch_opt_in(self):
        self.data["defaults"].update(factory_ref="v0.4.7", factory_track_branch=False)
        self.assertEqual(apply.reconcile_engine(self.data, backend=self.engine.backend), "applied")
        self.assertEqual(host.engine(self.data, "local")["ref"], "v0.4.7")

    def test_no_stable_release_fails_without_following_main(self):
        self.releases = RELEASES[:2]
        self.assertEqual(apply.upgrade(self.data, backend=self.engine.backend), "failed")
        self.assertEqual(self.engine.installs(), [])
        self.engine.backend.pause_timer.assert_not_called()
        self.assertEqual(host.engine(self.data, "local")["sha"], engine.OLD)

    def test_fleet_bare_upgrade_uses_release(self):
        context = (self.run_cmd, self.engine.backend, self.data["repo"])
        self.assertEqual(apply.reconcile_fleet_engine(self.data, None, {"local": context}, ref="HEAD"), "applied")
        self.assertEqual(host.engine(self.data, "local")["ref"], "v0.4.7")

    def test_drain_preflight_and_upgrade_share_release_target(self):
        host.save(self.data)
        with mock.patch.object(apply.postcheck, "check", return_value=[]):
            self.assertEqual(apply.main(["--upgrade", "--drain"]), 0)
        self.assertEqual(host.engine(host.load(), "local")["ref"], "v0.4.7")
        self.assertFalse(any("refs/heads/main" in argv for argv in self.calls))


if __name__ == "__main__":
    unittest.main()
