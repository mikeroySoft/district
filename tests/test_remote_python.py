"""Remote helpers must ignore login PATH, including on a Python-3.9 macOS host."""
from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from district import engine_cleanup, host, postcheck, preflight, streak


class RemotePythonTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.root / "path-bin"
        self.bin.mkdir()
        self.old = self.root / "python39"
        self.old.write_text(
            f"#!{sys.executable}\n"
            "import builtins, sys\n"
            "sys.version_info = (3, 9, 6)\n"
            "original = builtins.__import__\n"
            "def imports(name, *args, **kwargs):\n"
            "    if name == 'tomllib': raise ModuleNotFoundError(name)\n"
            "    return original(name, *args, **kwargs)\n"
            "builtins.__import__ = imports\n"
            "code = sys.argv[2]\n"
            "sys.argv = ['-c', *sys.argv[3:]]\n"
            "exec(code, {'__name__': '__main__'})\n"
        )
        self.old.chmod(0o755)
        (self.bin / "python3").symlink_to(self.old)
        self.tools = self.root / "tools"
        self.modern = self.tools / "factory/bin/python"
        self.modern.parent.mkdir(parents=True)
        self.modern.symlink_to(sys.executable)
        self.env = {
            "HOME": str(self.root / "home"), "PATH": str(self.bin),
            "UV_TOOL_DIR": str(self.tools), "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
        }
        self.commands = []
        self.layout = {
            "/usr/bin/python3": str(self.old), "/bin/python3": str(self.old),
            "/opt/homebrew/bin/python3": str(self.root / "missing-homebrew"),
            "/usr/local/bin/python3": str(self.root / "missing-local"),
        }

    def runner(self):
        def transport(ssh, **kwargs):
            command = ssh[-1]
            # Fake absolute macOS paths only in discovery, never in helper payloads.
            if command.startswith("/bin/sh -c "):
                for source, target in self.layout.items():
                    command = command.replace(source, target)
            self.commands.append(command)
            return subprocess.run(
                ["/bin/sh", "-c", command], env=self.env, text=True, capture_output=True,
                timeout=kwargs.get("timeout"), input=kwargs.get("input"),
            )
        return host.RemoteRunner("mac", "unused", transport=transport)

    def test_helpers_cleanup_and_watchdog_ignore_python39_on_path(self):
        runner = self.runner()
        target = runner.path(str(self.root / "written ' file"))
        target.write_text("stdin survives discovery")
        self.assertEqual(target.read_text(), "stdin survives discovery")
        result = engine_cleanup.cleanup({}, run_cmd=runner)
        self.assertEqual(result["scan_errors"], [])
        proc = runner(["python3", "-c", "import sys,tomllib; print(sys.executable); print(sys.stdin.read())"],
                      input="payload", timeout=10, env={"PATH": str(self.bin)}, quiet=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [str(self.modern), "payload"])
        envelopes = [shlex.split(command) for command in self.commands if not command.startswith("/bin/sh -c ")]
        self.assertTrue(all(parts[0] == str(self.modern) for parts in envelopes))
        self.assertEqual(sum(command.startswith("/bin/sh -c ") for command in self.commands), 1)

    def test_python39_only_host_fails_clearly_before_running_helper(self):
        self.modern.unlink()
        runner = self.runner()
        marker = self.root / "should-not-run"
        with self.assertRaisesRegex(host.DistrictError, r"mac:.*Python 3\.11\+"):
            runner(["touch", str(marker)], quiet=True)
        self.assertFalse(marker.exists())

    def test_homebrew_fallback_is_verified_without_path_lookup(self):
        self.modern.unlink()
        brew = self.root / "homebrew-python"
        brew.symlink_to(sys.executable)
        self.layout["/opt/homebrew/bin/python3"] = str(brew)
        proc = self.runner()(["python3", "-c", "import sys,tomllib; print(sys.executable)"], quiet=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(brew))

    def test_preflight_reports_selected_version_and_rejects_python39_only_host(self):
        rendered = f"# factory-example.service\nEnvironment=PATH={self.bin}\nExecStart=factory\n"
        table = {"path": str(self.root)}
        rows = preflight.check({}, "mac", "example", table, rendered, run_cmd=self.runner())
        self.assertTrue(any(row["status"] == "PASS" and str(self.modern) in row["detail"]
                            and ".".join(map(str, sys.version_info[:3])) in row["detail"] for row in rows), rows)
        self.modern.unlink()
        rows = preflight.check({}, "mac", "example", table, rendered, run_cmd=self.runner())
        self.assertTrue(any(row["status"] == "FAIL" and "Python 3.11+" in row["detail"] for row in rows), rows)

    def test_remote_scripts_parse_with_python39_grammar(self):
        # Inventory literal helper assignments and script constants, not just cleanup.
        scripts = [engine_cleanup._CLEANUP_SCRIPT, host.REMOTE_DEADLINE,
                   postcheck._HTTP, postcheck._REMOTE_TEMP, postcheck._REMOTE_REMOVE, streak.LAUNCHD_LOGS]
        package = Path(host.__file__).parent
        for module in ("host", "drain", "doctor", "service", "apply", "postcheck", "streak"):
            tree = ast.parse((package / f"{module}.py").read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "script"
                                                        for target in node.targets):
                    scripts.append(ast.literal_eval(node.value))
        for script in scripts:
            with self.subTest(script=script[:80]):
                ast.parse(script, feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
