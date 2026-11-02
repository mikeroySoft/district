"""Focused scheduler toolchain preflight checks."""

from __future__ import annotations

import importlib
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from district import host


def module():
    return importlib.import_module("district.preflight")


def rendered(path: str, home: str = "/scheduler/home") -> str:
    return (
        "# factory-widgets-dashboard.service\n"
        f"[Service]\nEnvironment=PATH=/dashboard\nEnvironment=HOME={home}\n\n"
        "# factory-widgets-triage-probe.service\n[Service]\nEnvironment=PATH=/probe\n\n"
        "# factory-widgets-eval.service\n[Service]\nEnvironment=PATH=/eval\n\n"
        "# factory-widgets.service\n"
        f'[Service]\nEnvironment="PATH={path}" "HOME={home}"\n\n'
        "# factory-widgets.timer\n[Timer]\nOnUnitActiveSec=10min\n"
    )


class SchedulerToolPreflightTest(unittest.TestCase):
    def test_rendered_units_uses_factory_header_format(self) -> None:
        output = "preamble\n# one.service\na\n# two.timer\nb\n"
        self.assertEqual(module().rendered_units(output), {"one.service": "a", "two.timer": "b"})

    def test_check_uses_only_dispatch_environment_and_derives_all_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary_dir, home = root / "scheduler bin", root / "home"
            binary_dir.mkdir()
            home.mkdir()
            observed = root / "observed"
            known = ("factory", "gh", "git", "uv", "python3", "cargo", "xcodegen", "swift", "xcodebuild")
            for name in known:
                executable = binary_dir / name
                flag = "-version" if name == "xcodebuild" else "--version"
                executable.write_text(
                    "#!/bin/sh\n"
                    f'test "$1" = "{flag}" || exit 91\n'
                    f'printf "%s|%s|%s|%s\\n" "$HOME" "$PATH" "${{CONTROLLER_SECRET-unset}}" "$1" >> {str(observed)!r}\n'
                    f"echo {name}-version\n"
                )
                executable.chmod(0o755)
            for name in ("node", "omp"):
                executable = binary_dir / name
                executable.write_text("#!/bin/sh\nexit 88\n")
                executable.chmod(0o755)
            gate_script = root / "scripts" / "gate"
            gate_script.parent.mkdir()
            gate_script.write_text("#!/bin/sh\nexit 89\n")
            gate_script.chmod(0o755)
            (root / ".factory.toml").write_text(
                '[[gate.check]]\nname="site"\nrun="node test.mjs"\n'
                '[[gate.check]]\nname="script"\nrun=["./scripts/gate"]\n'
            )
            data = {
                "defaults": {"review": {"command": ["omp", "review"]},
                             "manager": {"command": ["omp", "manage"]}},
                "repo": {"acme/widgets": {"path": str(root)}},
            }
            table = {"path": str(root), "toolchain": "macos-xcode",
                     "manager": {"command": ["node", "manage"]}}
            calls = []

            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                return host.run(argv, **kwargs)

            with mock.patch.dict(os.environ, {"CONTROLLER_SECRET": "do-not-leak"}), mock.patch("sys.platform", "darwin"):
                rows = module().check(
                    data, "local", "acme/widgets", table,
                    rendered(str(binary_dir), str(home)), run_cmd=run,
                )

            self.assertFalse([row for row in rows if row["status"] == "FAIL"], rows)
            text = "\n".join(row["label"] + " " + row["detail"] for row in rows)
            for value in ("site", "script", "review", "manager", "macos-xcode", "node", "omp"):
                self.assertIn(value, text)
            node = next(row for row in rows if row["label"].endswith(" node"))
            self.assertIn("site", node["detail"])
            self.assertIn("manager", node["detail"])  # one node lookup retains both sources
            self.assertEqual(
                set(observed.read_text().splitlines()),
                {f"{home}|{binary_dir}|unset|{'-version' if name == 'xcodebuild' else '--version'}"
                 for name in known},
            )
            probe_calls = [call for call in calls if call[0][:2] == ["/usr/bin/env", "-i"]]
            self.assertTrue(probe_calls)
            self.assertTrue(all(call[1].get("cwd") == root for call in probe_calls))
            self.assertTrue(all("env" not in call[1] for call in probe_calls))

    def test_missing_path_and_missing_binary_fail_closed(self) -> None:
        data, table = {}, {"path": "/repo"}
        rows = module().check(
            data, "local", "acme/widgets", table,
            "# factory-widgets.service\n[Service]\nEnvironment=HOME=/home/factory\n",
            run_cmd=mock.Mock(side_effect=AssertionError("missing PATH must not probe")),
        )
        self.assertEqual([row["status"] for row in rows], ["FAIL"])
        self.assertIn("PATH", rows[0]["detail"])

        def missing(argv, **kwargs):
            if argv[-1] == ".factory.toml":
                return subprocess.CompletedProcess(argv, 44, "", "")
            binary = argv[-1]
            return subprocess.CompletedProcess(argv, 1 if binary == "uv" else 0,
                                               "" if binary == "uv" else f"/bin/{binary}\n", "")

        rows = module().check(data, "local", "acme/widgets", table, rendered("/scheduler"), run_cmd=missing)
        failures = [row for row in rows if row["status"] == "FAIL"]
        self.assertEqual(len(failures), 1, rows)
        self.assertIn("uv", failures[0]["label"] + failures[0]["detail"])
        self.assertIn("/scheduler", failures[0]["detail"])

    def test_invalid_render_metadata_and_probe_errors_become_rows(self) -> None:
        plist = plistlib.dumps({"Label": "factory-widgets", "EnvironmentVariables": {"PATH": "/bin"}}).decode()
        bad_render = "# factory-widgets.service\n[Service]\nEnvironment=\"PATH=/bin\n"
        rows = module().check({}, "local", "acme/widgets", {"path": "/repo"}, bad_render)
        self.assertEqual(rows[0]["status"], "FAIL")
        self.assertIn("invalid", rows[0]["detail"].lower())
        malformed = '# factory-widgets.plist\n<?xml version="1.0"?><plist><dict>'
        rows = module().check({}, "local", "acme/widgets", {"path": "/repo"}, malformed)
        self.assertEqual(rows[0]["status"], "FAIL")
        self.assertIn("invalid factory-widgets.plist", rows[0]["detail"])

        def metadata_error(argv, **kwargs):
            if argv[-1] == ".factory.toml":
                raise OSError("read refused")
            raise subprocess.TimeoutExpired(argv, 2)

        rows = module().check({}, "local", "acme/widgets", {"path": "/repo"},
                              f"# factory-widgets.plist\n{plist}\n", run_cmd=metadata_error)
        self.assertTrue(any(row["status"] == "FAIL" and "read refused" in row["detail"] for row in rows), rows)
        self.assertTrue(any(row["status"] == "FAIL" and "timed out" in row["detail"] for row in rows), rows)

    def test_version_failure_warns_and_unknown_gate_is_not_executed(self) -> None:
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == ".factory.toml":
                return subprocess.CompletedProcess(argv, 0,
                    '[[gate.check]]\nname="custom"\nrun=["custom-gate"]\n', "")
            if argv[-1] == "custom-gate":
                return subprocess.CompletedProcess(argv, 0, "/tools/custom-gate\n", "")
            if len(argv) >= 2 and argv[-2] == "/tools/gh":
                return subprocess.CompletedProcess(argv, 7, "", "version broke")
            if "command -v" in argv[-3]:
                return subprocess.CompletedProcess(argv, 0, f"/tools/{argv[-1]}\n", "")
            return subprocess.CompletedProcess(argv, 0, "version\n", "")

        rows = module().check({}, "local", "acme/widgets", {"path": "/repo"}, rendered("/tools"), run_cmd=run)
        self.assertFalse([row for row in rows if row["status"] == "FAIL"], rows)
        self.assertTrue(any(row["status"] == "WARN" and "gh" in row["label"] for row in rows), rows)
        self.assertTrue(any(row["status"] == "WARN" and "custom-gate" in row["label"]
                            and "skipped" in row["detail"] for row in rows), rows)
        self.assertFalse(any(len(argv) >= 2 and argv[-2] == "/tools/custom-gate" for argv in calls))



if __name__ == "__main__":
    unittest.main()
