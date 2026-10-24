"""Point every XDG base dir at a throwaway temp dir before any test runs.

District reads and writes `$XDG_CONFIG_HOME/factory/config.toml` and the unit
files beside it. A test that forgets to isolate those would hit the real host
registry (that is how #98's first rm test wiped it on 2026-10-23), so the
whole test process is isolated up front, whatever the caller's env says.

Imported by tests/__init__.py (`python -m unittest tests.test_x`) and by
tests/test_isolation.py (`unittest discover -s tests`, which skips __init__).
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

MARKER = "DISTRICT_TEST_XDG_ROOT"

if not os.environ.get(MARKER):
    ROOT = tempfile.mkdtemp(prefix="district-tests-")
    atexit.register(shutil.rmtree, ROOT, True)
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        os.environ[name] = os.path.join(ROOT, name.lower())
    os.environ[MARKER] = ROOT
ROOT = os.environ[MARKER]
