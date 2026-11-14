"""External recovery interpreter selection checks.

Run: uv run python -m unittest discover -s tests -p 'test_external_python.py'
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from district import update


class ExternalPythonCase(unittest.TestCase):
    @contextmanager
    def layout(
        self,
        versions: dict[Path, tuple[int, int]],
        *,
        platform: str,
        resolved: dict[Path, Path] | None = None,
        failures: dict[Path, Exception] | None = None,
    ):
        resolved = resolved or {}
        failures = failures or {}
        existing = versions.keys() | failures.keys()
        calls: list[Path] = []

        def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            candidate = Path(argv[0])
            calls.append(candidate)
            if candidate in failures:
                raise failures[candidate]
            return subprocess.CompletedProcess(argv, versions[candidate] < (3, 11))

        with mock.patch("district.update.sys.platform", platform), \
                mock.patch.object(Path, "is_file", lambda path: path in existing), \
                mock.patch("district.update.os.access", lambda path, mode: Path(path) in existing and mode == os.X_OK), \
                mock.patch.object(Path, "resolve", lambda path: resolved.get(path, path)), \
                mock.patch("district.update.subprocess.run", side_effect=run):
            yield calls

    def test_darwin_uses_verified_homebrew_python(self) -> None:
        system = {Path("/usr/bin/python3"): (3, 9), Path("/bin/python3"): (3, 9)}
        for candidate in (Path("/opt/homebrew/bin/python3"), Path("/usr/local/bin/python3")):
            with self.subTest(candidate=candidate):
                with self.layout({**system, candidate: (3, 11)}, platform="darwin"):
                    selected = update.external_python(Path("/tool"))
                self.assertEqual(selected, candidate)

    def test_linux_still_prefers_usr_bin(self) -> None:
        versions = {
            Path("/usr/bin/python3"): (3, 11),
            Path("/bin/python3"): (3, 12),
            Path("/opt/homebrew/bin/python3"): (3, 13),
        }
        with self.layout(versions, platform="linux") as calls:
            selected = update.external_python(Path("/tool"))
        self.assertEqual(selected, Path("/usr/bin/python3"))
        self.assertEqual(calls, [selected])

    def test_rejects_venv_python_by_lexical_and_resolved_paths(self) -> None:
        versions = {
            Path("/usr/bin/python3"): (3, 9),
            Path("/bin/python3"): (3, 9),
            Path("/opt/homebrew/bin/python3"): (3, 11),
            Path("/usr/local/bin/python3"): (3, 11),
        }
        resolved = {
            Path("/opt/homebrew/bin/python3"): Path("/outside/python3"),
            Path("/usr/local/bin/python3"): Path("/opt/bin/python3"),
        }
        with self.layout(versions, platform="darwin", resolved=resolved) as calls:
            with self.assertRaises(update.UpdateError):
                update.external_python(Path("/opt"))
        self.assertEqual(calls, [Path("/usr/bin/python3"), Path("/bin/python3")])

    def test_skips_an_unlaunchable_candidate(self) -> None:
        versions = {Path("/bin/python3"): (3, 11)}
        failures = {Path("/usr/bin/python3"): OSError("cannot execute")}
        with self.layout(versions, platform="linux", failures=failures):
            selected = update.external_python(Path("/tool"))
        self.assertEqual(selected, Path("/bin/python3"))

    def test_recovery_diagnostic_names_selected_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "transaction.json").touch()
            selected = Path("/opt/homebrew/bin/python3")
            with mock.patch.object(update, "state_root", return_value=root), \
                    mock.patch.object(update, "external_python", return_value=selected):
                with self.assertRaises(update.UpdateError) as caught:
                    update.recover_pending(SimpleNamespace(dry_run=True))
        self.assertIn(f"`{selected} {root / 'transaction.py'} recover {root}`", str(caught.exception))
        self.assertNotIn("/usr/bin/python3", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
