"""Authorized detection preserves CLI decisions without exposing private configuration."""

import json
import os
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

    def test_workflow_proposals_match_cli_without_disclosing_commands(self):
        repo = self.repo()
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (repo / "Cargo.toml").write_text("[package]\nname='w'\n")
        (workflows / "ci.yaml").write_text(
            "jobs:\n  check:\n    steps:\n"
            "      - run: cargo test --token=DO_NOT_PUBLISH\n"
            "        name: Tests\n"
            "      - run: cargo build --workspace\n"
            "        name: Build\n"
        )
        code, cli = self.district("add", "--dry-run", str(repo))
        calls_before_detect = self.calls("factory")
        http_code, public = detect(str(repo))
        self.assertEqual((code, http_code, public["ok"]), (0, 200, True))
        for source in (".github/workflows/ci.yaml:4", ".github/workflows/ci.yaml:6", "Cargo.toml"):
            self.assertIn(source, cli)
            self.assertIn(source, public["output"])
        self.assertIn("tests from .github/workflows/ci.yaml:4", public["output"])
        self.assertIn("build from .github/workflows/ci.yaml:6", public["output"])
        self.assertNotIn("tests from Cargo.toml", public["output"])
        for private in ("DO_NOT_PUBLISH", "cargo test", "cargo build", str(repo)):
            self.assertNotIn(private, public["output"])
        self.assertLess(public["output"].index("build from"), public["output"].index("fmt from"))
        self.assertFalse((repo / ".factory.toml").exists())
        self.assertEqual(self.calls("factory"), calls_before_detect)

    def test_workflow_files_and_parent_directories_must_be_bounded_and_regular(self):
        for unsafe in ("file-link", "github-link", "workflows-link", "fifo", "oversized", "too-many"):
            with self.subTest(unsafe=unsafe):
                repo = self.repo(name=unsafe)
                workflows = repo / ".github" / "workflows"
                workflows.mkdir(parents=True)
                external = self.tmp / f"{unsafe}-external"
                if unsafe == "file-link":
                    external.write_text("steps:\n  - run: cargo test\n")
                    (workflows / "ci.yml").symlink_to(external)
                elif unsafe == "github-link":
                    (repo / ".github").rename(external)
                    (repo / ".github").symlink_to(external, target_is_directory=True)
                elif unsafe == "workflows-link":
                    workflows.rename(external)
                    workflows.symlink_to(external, target_is_directory=True)
                elif unsafe == "fifo":
                    os.mkfifo(workflows / "ci.yml")
                elif unsafe == "oversized":
                    (workflows / "ci.yml").write_text("#" * 65537)
                else:
                    for index in range(65):
                        (workflows / f"{index}.yml").write_text("")
                code, public = detect(str(repo))
                self.assertEqual((code, public["ok"]), (400, False))
                self.assertNotIn(str(repo), public["output"])
                self.assertNotIn(str(external), public["output"])

    def test_workflow_source_filename_is_not_a_disclosure_channel(self):
        repo = self.repo()
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ghp_DO_NOT_PUBLISH.yml").write_text("steps:\n  - name: Tests\n    run: cargo test\n")
        code, public = detect(str(repo))
        self.assertEqual(code, 200, public)
        self.assertIn("tests from workflow source withheld", public["output"])
        self.assertNotIn("DO_NOT_PUBLISH", public["output"])
