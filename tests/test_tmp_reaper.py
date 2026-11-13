"""scripts/tmp-reaper-temp.sh: the TEMPORARY hourly /tmp reaper deployed by hand on rocm-tank.

Factory review snapshots are left read-only (dr-x------, d-ws------). A bare `rm -rf` under
`set -e` then aborted every hourly run from 2026-11-07 04:00 PST before the log line was written.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tmp-reaper-temp.sh"
OLD = time.time() - 3 * 86400


def age(path: Path) -> None:
    """Backdate every entry under `path` (bottom-up) so the reaper sees it as stale."""
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in [*filenames, *dirnames]:
            os.utime(Path(dirpath) / name, (OLD, OLD), follow_symlinks=False)
    os.utime(path, (OLD, OLD))


class TmpReaperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="district-reaper-test-")).resolve()
        self.addCleanup(self.cleanup)
        self.root = self.base / "tmp"
        self.root.mkdir()
        self.log = self.base / "reaper.log"
        self.bin = self.base / "bin"
        self.bin.mkdir()

    def cleanup(self) -> None:
        for dirpath, dirnames, _ in os.walk(self.base):
            for name in dirnames:
                os.chmod(Path(dirpath) / name, 0o700)
        shutil.rmtree(self.base)

    def read_only_snapshot(self, name: str) -> Path:
        top = self.root / name
        snapshot = top / "factory-review-abc" / "snapshot"
        (snapshot / "tests").mkdir(parents=True)
        (snapshot / "change.txt").write_text("x\n")
        (snapshot / "tests" / "test_old.py").write_text("x\n")
        runner = top / "factory-runner-review-def"
        runner.mkdir()
        (runner / "review.diff").write_text("x\n")
        age(top)
        for path in (snapshot / "change.txt", snapshot / "tests" / "test_old.py", runner / "review.diff"):
            path.chmod(0o400)
        (snapshot / "tests").chmod(0o500)
        snapshot.chmod(0o500)
        runner.chmod(0o4300)  # d-ws------, as Factory leaves review runner dirs
        os.utime(top, (OLD, OLD))
        return top

    def reap(self) -> subprocess.CompletedProcess:
        env = {**os.environ, "REAPER_TMP": str(self.root), "REAPER_LOG": str(self.log),
               "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run([str(SCRIPT)], check=False, env=env, capture_output=True, text=True, timeout=120)

    def last_log(self) -> str:
        self.assertTrue(self.log.exists(), "the run must always write its summary line")
        return self.log.read_text().splitlines()[-1]

    def test_read_only_review_snapshot_is_removed(self) -> None:
        stale = self.read_only_snapshot("tmpstale1")
        fresh = self.root / "tmpfresh1"
        fresh.mkdir()
        proc = self.reap()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertIn("removed=1 skipped_recent=1 skipped_open=0 failed=0 ", self.last_log())

    def test_unremovable_dir_is_counted_and_fails_only_after_logging(self) -> None:
        stuck = self.read_only_snapshot("tmpstuck1")
        stale = self.read_only_snapshot("tmpstale2")
        rm = shutil.which("rm")
        fake = self.bin / "rm"
        fake.write_text(f'#!/bin/sh\ncase "$*" in *{stuck}) echo "rm: refusing" >&2; exit 1;; esac\nexec {rm} "$@"\n')
        fake.chmod(0o755)
        proc = self.reap()
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(stuck.exists())
        self.assertFalse(stale.exists(), "one failure must not stop the rest of the run")
        self.assertIn("removed=1 skipped_recent=0 skipped_open=0 failed=1 ", self.last_log())

    def test_dir_held_open_by_a_process_is_kept(self) -> None:
        held = self.read_only_snapshot("tmpheld1")
        holder = subprocess.Popen(["sleep", "60"], cwd=held / "factory-review-abc")
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        proc = self.reap()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(held.exists())
        self.assertIn("removed=0 skipped_recent=0 skipped_open=1 failed=0 ", self.last_log())


if __name__ == "__main__":
    unittest.main()
