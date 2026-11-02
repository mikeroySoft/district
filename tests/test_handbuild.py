"""Hand gates refuse impossible bug-repro packaging before starting a step."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ID = "tests/test_x.py::XTest::test_y"
TEST_SOURCE = "import unittest\nclass XTest(unittest.TestCase):\n    def test_y(self):\n        self.assertEqual(1, 2)\n"


def factory_python():
    if importlib.util.find_spec("factory"):
        return [sys.executable]
    executable = shutil.which("factory")
    if executable:
        shebang = Path(executable).read_text().splitlines()[0]
        if shebang.startswith("#!"):
            return shlex.split(shebang[2:])
    return None


FACTORY_PYTHON = factory_python()


@unittest.skipUnless(FACTORY_PYTHON, "hand-build integration requires an installed Factory")
class HandbuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("config", "commit.gpgsign", "false")
        (self.wt / "x.py").write_text("value = 1\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.issue = {"labels": [{"name": "bug"}], "body": "", "comments": []}
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, sys\n"
            "if sys.argv[1:3] == ['api', 'user']:\n"
            "    print('triage-bot')\n"
            "else:\n"
            "    print(pathlib.Path(os.environ['ISSUE_FIXTURE']).read_text())\n"
        )
        gh.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        PYTHONPATH=str(ROOT), ISSUE_FIXTURE=str(self.tmp / "issue.json"))
        self.handoff = self.wt / ".factory" / "handoff-532.md"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.wt, check=True,
                              text=True, capture_output=True).stdout

    def commit(self, message):
        self.git("add", "x.py", *(["tests"] if (self.wt / "tests").exists() else []))
        self.git("commit", "-qm", message)

    def add_test(self):
        (self.wt / "tests").mkdir(exist_ok=True)
        (self.wt / "tests" / "test_x.py").write_text(TEST_SOURCE)

    def declare(self, test_id=TEST_ID):
        self.handoff.parent.mkdir(exist_ok=True)
        self.handoff.write_text(f"Repro test: `{test_id}`\n")

    def packaged(self):
        self.add_test()
        self.commit("reproducing test")
        (self.wt / "x.py").write_text("value = 2\n")
        self.commit("fix")
        self.declare()

    def invoke(self, mode="preflight", *extra, launcher=None):
        (self.tmp / "issue.json").write_text(json.dumps(self.issue))
        return subprocess.run(
            [*FACTORY_PYTHON, *(["-c", launcher] if launcher else ["-m", "district.handbuild"]), mode,
             "--wt", str(self.wt), "--ticket", "532", "--repo", "acme/widgets",
             "--base", self.base, *extra], env=self.env, text=True, capture_output=True,
        )

    def assert_refused(self, result, reason):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PREFLIGHT REFUSED:", result.stdout)
        self.assertIn(reason, result.stdout)

    def test_incident_combined_commit_without_handoff_is_refused(self):
        self.add_test()
        (self.wt / "x.py").write_text("value = 2\n")
        self.commit("test and fix together")
        self.assert_refused(self.invoke(), "Repro test:")
        self.assert_refused(self.invoke(), "No repro test:")

    def test_declared_test_and_fix_in_one_commit_is_refused(self):
        self.add_test()
        self.commit("combined test and fix")
        self.declare()
        self.assert_refused(self.invoke(), "test commit must come before the fix commit")

    def test_split_test_and_fix_passes(self):
        self.packaged()
        before = self.git("status", "--porcelain", "--untracked-files=all")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PREFLIGHT PASS", result.stdout)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), before)

    def test_dotted_test_id_passes(self):
        self.packaged()
        self.declare("tests.test_x.XTest.test_y")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PREFLIGHT PASS", result.stdout)

    def test_test_added_only_in_last_commit_is_refused(self):
        (self.wt / "x.py").write_text("value = 2\n")
        self.commit("fix first")
        self.add_test()
        self.commit("test too late")
        self.declare()
        self.assert_refused(self.invoke(), "test commit must come before the fix commit")

    def test_declared_test_must_be_added_not_already_in_base(self):
        self.add_test()
        self.commit("existing test")
        self.base = self.git("rev-parse", "HEAD").strip()
        for value in (2, 3):
            (self.wt / "x.py").write_text(f"value = {value}\n")
            self.commit("source change")
        self.declare()
        self.assert_refused(self.invoke(), "no commit before the fix adds")

    def test_no_repro_exemption_requires_ticket_or_authenticated_triage(self):
        cases = [
            ("No repro test: hardware required", [], True),
            ("No repro test:   \n", [], False),
            ("", [{"body": "Triage: accepted\nNo repro test: hardware required",
                    "author": {"login": "TRIAGE-BOT"}}], True),
            ("", [{"body": "Triage: accepted\nNo repro test: hardware required",
                    "author": {"login": "someone-else"}}], False),
            ("", [{"body": "No repro test: hardware required",
                    "author": {"login": "triage-bot"}}], False),
        ]
        self.handoff.parent.mkdir()
        self.handoff.write_text("No repro test: worker cannot grant an exemption\n")
        for body, comments, passes in cases:
            with self.subTest(body=body, comments=comments):
                self.issue.update(body=body, comments=comments)
                result = self.invoke()
                if passes:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("PREFLIGHT PASS", result.stdout)
                else:
                    self.assert_refused(result, "No repro test:")

    def test_non_bug_ticket_passes_without_packaging(self):
        self.issue["labels"] = [{"name": "enhancement"}]
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PREFLIGHT PASS", result.stdout)

    def test_refusal_prevents_timer_pause_and_gate_step(self):
        marker = self.tmp / "timer-paused"
        result = self.invoke("step", "--", sys.executable, "-c",
                             f"from pathlib import Path; Path({str(marker)!r}).touch()")
        self.assert_refused(result, "Repro test:")
        self.assertFalse(marker.exists())

    def test_passing_step_runs_command_and_propagates_failure(self):
        self.packaged()
        marker = self.tmp / "step-started"
        result = self.invoke("step", "--", sys.executable, "-c",
                             f"from pathlib import Path; Path({str(marker)!r}).touch(); raise SystemExit(7)")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertTrue(marker.exists())

    def test_every_gate_launch_is_checked_including_review(self):
        self.packaged()
        # Only the gate launch is faked; declarations and test inventory use
        # Factory's real parsers against throwaway git commits.
        launched = self.tmp / "launched"
        launcher = (
            "import sys\nfrom pathlib import Path\nfrom factory import dispatch\n"
            "from district.handbuild import main\n"
            f"def launch(*a, **kw):\n    with Path({str(launched)!r}).open('a') as f: f.write('gate\\n')\n"
            "dispatch.run_gate = launch\nraise SystemExit(main(sys.argv[1:]))\n"
        )
        runner = self.tmp / "runner.py"
        for remove_before in (0, 1, 2):
            with self.subTest(remove_before=remove_before):
                self.declare()
                launched.unlink(missing_ok=True)
                runner.write_text(
                    "from pathlib import Path\nfrom factory import dispatch\n"
                    "for i in range(3):\n"
                    f"    if i == {remove_before}: Path({str(self.handoff)!r}).unlink()\n"
                    f"    dispatch.run_gate(Path({str(self.wt)!r}), 532)\n"
                )
                result = self.invoke("run", str(runner), launcher=launcher)
                self.assert_refused(result, "Repro test:")
                self.assertEqual(launched.read_text() if launched.exists() else "",
                                 "gate\n" * remove_before)


if __name__ == "__main__":
    unittest.main()
