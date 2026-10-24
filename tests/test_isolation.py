from __future__ import annotations

import os
import unittest
from pathlib import Path

try:
    from . import isolation
except ImportError:  # `unittest discover -s tests` imports test modules top-level
    import isolation

from district import host


class HostIsolationTest(unittest.TestCase):
    def test_host_paths_never_resolve_to_the_real_home(self) -> None:
        root = Path(isolation.ROOT).resolve()
        for path in (host.path(), host.unit_dir(), host.operation_lock_path()):
            with self.subTest(path=path):
                resolved = path.resolve()
                self.assertTrue(resolved.is_relative_to(root), f"{resolved} escapes {root}")
                self.assertFalse(resolved.is_relative_to((Path.home() / ".config").resolve()))
                self.assertFalse(resolved.is_relative_to((Path.home() / ".cache").resolve()))

    def test_isolation_overrides_the_callers_env(self) -> None:
        for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            self.assertTrue(Path(os.environ[name]).is_relative_to(isolation.ROOT), name)


if __name__ == "__main__":
    unittest.main()
