"""Every test module must import when loaded by file path from the repo root.

Factory's bug-repro gate loads one test file with importlib from the repo
root (PYTHONPATH=root, no tests/ on sys.path), unlike `unittest discover -s
tests`. A bare `from test_district import ...` passes the full suite but makes
a repro test unloadable by the gate (#114).
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOAD_BY_PATH = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('_by_path', sys.argv[1])\n"
    "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
)


class SuiteLayoutTest(unittest.TestCase):
    def test_every_test_module_imports_when_loaded_by_path_from_repo_root(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(module=path.name):
                proc = subprocess.run(
                    [sys.executable, "-c", LOAD_BY_PATH, str(path)],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr[-600:])


if __name__ == "__main__":
    unittest.main()
