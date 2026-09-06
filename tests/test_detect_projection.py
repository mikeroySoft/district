"""Authorized detection preserves CLI decisions without exposing private configuration."""

import json

from test_district import DistrictCase
from district import host
from district.dashboard_read import detect


class DetectProjectionTest(DistrictCase):
    def test_existing_url_clone_and_port_agree_with_cli_preview(self):
        repo = self.repo(toml='[dashboard]\nport=8771\n[[gate.check]]\nname="tests"\nrun=["secret-command"]\nexclusive=true\n')
        host.save({"defaults": {"clone_dir": str(self.tmp)}, "repo": {
            "acme/other": {"path": "/private/other", "dashboard": {"port": 8771}},
        }})
        target = "https://github.com/Acme/widgets.git"
        code, cli = self.district("add", "--dry-run", target)
        http_code, public = detect(target)
        self.assertEqual((code, http_code, public["ok"]), (0, 200, True))
        for value in ("acme/widgets (adopt)", "dashboard port: 8772", "tests"):
            self.assertIn(value, cli)
            self.assertIn(value, public["output"])
        self.assertIn("exclusive", public["output"])
        self.assertNotIn(str(repo), public["output"])
        self.assertNotIn("secret-command", public["output"])
        self.assertFalse(any("clone" in call for call in self.calls("gh")))

    def test_registered_port_precedence_and_new_clone_preview(self):
        repo = self.repo(toml='[dashboard]\nport=8771\n')
        host.save({"defaults": {"clone_dir": str(self.tmp)}, "repo": {
            "acme/widgets": {"path": str(repo), "dashboard": {"port": 8780}},
        }})
        for target, expected in ((str(repo), "dashboard port: 8780"),
                                 ("https://github.com/acme/new", "dashboard port: 8781")):
            code, public = detect(target)
            self.assertEqual(code, 200, public)
            self.assertIn(expected, public["output"])
        self.assertFalse((self.tmp / "new").exists())
        self.assertEqual(self.calls("factory"), [])

    def test_bounded_markers_and_remote_metadata_cannot_disclose_secrets(self):
        repo = self.repo()
        (repo / "package.json").write_text(json.dumps({"scripts": {"test": "password=DO_NOT_PUBLISH"}}))
        self.stub("gh", ("repo view *", json.dumps({"isFork": True, "parent": {"nameWithOwner": "ghp_DO_NOT_PUBLISH/repo"}})))
        code, public = detect(str(repo))
        self.assertEqual(code, 200, public)
        self.assertIn("tests", public["output"])
        self.assertNotIn("DO_NOT_PUBLISH", public["output"])
        (repo / ".factory.toml").symlink_to(repo / "package.json")
        self.assertEqual(detect(str(repo))[0], 400)
        self.assertEqual(self.calls("factory"), [])
        self.assertLess(len(public["output"]), 8193)
