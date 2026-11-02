"""Hand-run safety checks: temporary HOME, fake units, no live systemd calls."""
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import venv


SYSTEMCTL = '''#!/usr/bin/env python3
import json, os, pathlib, sys
state = json.loads(pathlib.Path(os.environ["FAKE_UNITS"]).read_text())
if "list-units" in sys.argv:
    for unit, props in state.items():
        if props.get("ActiveState") in ("active", "activating", "deactivating", "reloading"):
            print(unit, "loaded", props["ActiveState"], "running Fake unit")
elif "show" in sys.argv:
    for key, value in state.get(sys.argv[sys.argv.index("show") + 1], {}).items():
        print(key + "=" + value)
else:
    sys.exit(2)
'''
SYSTEMD_RUN = '''#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path(os.environ["FAKE_LAUNCH"]).write_text(json.dumps(sys.argv[1:]))
'''


class HandRunTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.home / ".local/share/district"
        self.source = self.data / "handland-532"
        self.source.mkdir(parents=True)
        self.script = self.source / "chain.sh"
        self.script.write_text("#!/bin/bash\nprintf 'approved\\n'\n")
        self.script.chmod(0o755)
        self.replacement = self.home / "replacement.sh"
        self.replacement.write_text("#!/bin/bash\nprintf 'replacement\\n'\n")
        self.units = self.home / "units.json"
        self.units.write_text("{}")
        self.launch_log = self.home / "launch.json"
        binary = self.home / "tools"
        binary.mkdir()
        for name, text in (("systemctl", SYSTEMCTL), ("systemd-run", SYSTEMD_RUN)):
            path = binary / name
            path.write_text(text)
            path.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": f"{binary}:{os.environ['PATH']}",
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "FAKE_UNITS": str(self.units), "FAKE_LAUNCH": str(self.launch_log),
        }))

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "district", "hand-run", *map(str, args)],
                              text=True, capture_output=True)

    def active(self, start, stop="", state="active"):
        executable = "/bin/bash" if Path(start).suffix == ".sh" else str(start)
        self.units.write_text(json.dumps({"handland-532-chain.service": {
            "ActiveState": state, "ExecStart": f"{{ path={executable} ; argv[]={executable} {start} ; }}",
            "ExecStopPost": f"{{ path={stop} ; argv[]={stop} ; }}" if stop else "",
        }}))

    def test_update_refuses_active_script_then_renames_after_stop(self):
        original = self.script.read_bytes()
        inode = self.script.stat().st_ino
        for state in ("active", "activating", "deactivating", "reloading"):
            self.active(self.script, state=state)
            result = self.command("update", self.script, self.replacement)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("handland-532-chain.service", result.stderr)
            self.assertEqual(self.script.read_bytes(), original)
            self.assertEqual(self.script.stat().st_ino, inode)
        self.units.write_text("{}")
        result = self.command("update", self.script, self.replacement)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.script.read_bytes(), self.replacement.read_bytes())
        self.assertNotEqual(self.script.stat().st_ino, inode)
        self.assertEqual(self.script.stat().st_mode & 0o777, 0o755)

    def test_update_refuses_open_script_even_without_unit(self):
        original = self.script.read_bytes()
        with self.script.open("rb"):
            result = self.command("update", self.script, self.replacement)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("open", result.stderr)
        self.assertIn(str(self.script), result.stderr)
        self.assertEqual(self.script.read_bytes(), original)

    def launch(self):
        stop = self.source / "stop.sh"
        stop.write_text("#!/bin/bash\nprintf 'stopped\\n'\n")
        result = self.command("launch", "532-chain", self.script, "--stop-post", stop)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.launch_log.read_text())
        start = Path(args[-1])
        stop = Path(next(arg.split("=", 2)[2] for arg in args
                         if arg.startswith("--property=ExecStopPost=")))
        return start, stop

    def test_frozen_shell_python_helpers_and_stop_survive_live_edits(self):
        other = self.data / "handland-489-r2"
        other.mkdir()
        helpers = other / "helpers"
        helpers.mkdir()
        (helpers / "sitecustomize.py").write_text("import builtins\nbuiltins.handrun_value = 'frozen'\n")
        (other / "work.py").write_text("print(handrun_value)\n")
        (self.source / "sourced.sh").write_text("printf 'sourced\\n'\n")
        self.script.write_text(f'#!/bin/bash\nset -eu\nsource "{self.source}/sourced.sh"\n'
                               f'PYTHONPATH="{helpers}" {sys.executable} "{other}/work.py"\n')
        start, stop = self.launch()
        run = start.parent
        self.assertTrue(run.is_relative_to(self.data / "runs"))
        for path in run.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o222, 0, path)
        self.assertTrue((run / "checksums.txt").is_file())
        for path in (self.script, self.source / "sourced.sh", other / "work.py", helpers / "sitecustomize.py"):
            path.write_text("exit 91\n")
        result = subprocess.run([str(start)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], ["sourced", "frozen"])
        result = subprocess.run([str(stop)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "stopped")
        self.active(start, stop)
        self.assertEqual(self.command("check").returncode, 0)
        frozen = next(run.rglob("chain.sh"))
        result = self.command("update", frozen, self.replacement)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("handland-532-chain.service", result.stderr)
        result = self.command("update", self.script, self.replacement)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("handland-532-chain.service", result.stderr)

    def test_cached_python_helper_remains_importable(self):
        helper = self.source / "helper.py"
        helper.write_text("value = 'approved cached helper'\n")
        py_compile.compile(str(helper), doraise=True)
        self.script.write_text(f'#!/bin/bash\nPYTHONPATH="{self.source}" '
                               f'{sys.executable} -c "import helper; print(helper.value)"\n')
        start, _ = self.launch()
        helper.write_text("value = 'live replacement'\n")
        result = subprocess.run([str(start)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "approved cached helper")

    def test_relative_helpers_execute_frozen_versions(self):
        helper = self.source / "step.sh"
        library = self.source / "lib"
        library.mkdir()
        python = library / "run.py"
        for invocation in ('./step.sh', 'dir=.; "$dir/step.sh"',
                           '"$(dirname "$0")/step.sh"', 'python3 lib/run.py'):
            with self.subTest(invocation=invocation):
                helper.write_text("#!/bin/bash\nprintf 'approved shell\\n'\n")
                helper.chmod(0o755)
                python.write_text("print('approved Python')\n")
                self.script.write_text(f"#!/bin/bash\nset -eu\n{invocation}\n")
                start, stop = self.launch()
                helper.write_text("#!/bin/bash\nprintf 'live shell\\n'\n")
                python.write_text("print('live Python')\n")
                result = subprocess.run([str(start)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = "approved Python" if invocation.startswith("python3") else "approved shell"
                self.assertEqual(result.stdout.strip(), expected)
                result = subprocess.run([str(stop)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "stopped")

    def test_unrelated_logs_and_notes_do_not_block_launch(self):
        unrelated = self.data / "handland-unrelated"
        unrelated.mkdir()
        for name in ("traceback.log", "notes.md", "output.txt"):
            (unrelated / name).write_text(
                'Traceback: File "/usr/lib/python3.13/subprocess.py", line 1\n'
                'Operator note: rerun /tmp/old-chain.sh after investigation.\n'
            )
        start, stop = self.launch()
        for wrapper, expected in ((start, "approved"), (stop, "stopped")):
            result = subprocess.run([str(wrapper)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)

    def test_external_live_script_references_are_refused(self):
        outside = self.home / "live.sh"
        outside.write_text("#!/bin/bash\nprintf 'live\\n'\n")
        outside.chmod(0o755)
        suffix = outside.relative_to(self.home)
        for reference in (str(outside), f"$HOME/{suffix}", f"${{HOME}}/{suffix}", f"~/{suffix}"):
            with self.subTest(reference=reference):
                self.script.write_text(f"#!/bin/bash\n{reference}\n")
                result = self.command("launch", "532-chain", self.script)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot relocate live script reference", result.stderr)
                self.assertIn(reference, result.stderr)
                self.assertFalse(self.launch_log.exists())

    def test_nonexecutable_sourced_script_rejects_external_live_dependency(self):
        sourced = self.source / "sourced.sh"
        sourced.write_text('# no executable permission or shebang\nbash /tmp/external-live.sh\n')
        sourced.chmod(0o644)
        self.script.write_text("#!/bin/bash\nsource ./sourced.sh\n")
        result = self.command("launch", "532-chain", self.script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/tmp/external-live.sh", result.stderr)
        self.assertIn("sourced.sh", result.stderr)
        self.assertFalse(self.launch_log.exists())

    def test_nonexecutable_source_fragments_reject_external_live_dependency(self):
        for name, command in (("settings", "source"), ("settings.txt", "."),
                              ("helper.bash", "source"), ("helper.inc", ".")):
            with self.subTest(name=name):
                sourced = self.source / name
                sourced.write_text('bash /tmp/external-live.sh\n')
                sourced.chmod(0o644)
                self.script.write_text(f'#!/bin/bash\n{command} "./{name}"\n')
                result = self.command("launch", "532-chain", self.script)
                sourced.unlink()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("/tmp/external-live.sh", result.stderr)
                self.assertIn(name, result.stderr)
                self.assertFalse(self.launch_log.exists())

    def test_external_script_shebang_interpreter_is_refused(self):
        interpreter = self.home / "interpreter.sh"
        interpreter.write_text('#!/bin/bash\nexec /bin/bash "$@"\n')
        interpreter.chmod(0o755)
        self.script.write_text(f"#!{interpreter}\nprintf 'approved\\n'\n")
        result = self.command("launch", "532-chain", self.script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(interpreter), result.stderr)
        self.assertFalse(self.launch_log.exists())

    def test_uncopied_engine_and_repository_paths_stay_live(self):
        engine = self.data / "engines/v1/bin"
        engine.mkdir(parents=True)
        (engine / "python").symlink_to(sys.executable)
        repository = self.data / "roots/project"
        repository.mkdir(parents=True)
        for prefix in (str(self.data), "$HOME/.local/share/district",
                       "${HOME}/.local/share/district", "~/.local/share/district"):
            with self.subTest(prefix=prefix):
                self.script.write_text(
                    f"#!/bin/bash\nset -eu\ncd {prefix}/roots/project\n"
                    f"{prefix}/engines/v1/bin/python -c 'import os; print(os.getcwd())'\n"
                )
                start, _ = self.launch()
                result = subprocess.run([str(start)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(repository))

    def test_python_start_and_stop_preserve_shebang_interpreter_and_options(self):
        environment = self.home / "orchestration-venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / "bin/python"
        entry = self.source / "entry.py"
        stop = self.source / "stop.py"
        for shebang in (f"#!{python} -I", f"#!/usr/bin/env -S {python} -I"):
            with self.subTest(shebang=shebang):
                for script, label in ((entry, "start"), (stop, "stop")):
                    script.write_text(f"{shebang}\nimport sys\n"
                                      f"print({label!r}, sys.prefix, sys.flags.isolated)\n")
                result = self.command("launch", "532-chain", entry, "--stop-post", stop)
                self.assertEqual(result.returncode, 0, result.stderr)
                run = Path(result.stdout.strip())
                for label in ("start", "stop"):
                    result = subprocess.run([str(run / label)], text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), f"{label} {environment} 1")

    def test_checksum_mismatch_prevents_start_and_stop_side_effects(self):
        marker = self.home / "side-effect"
        self.script.write_text(f"#!/bin/bash\ntouch {marker}\n")
        start, stop = self.launch()
        frozen = next(start.parent.rglob("chain.sh"))
        frozen.chmod(0o644)
        frozen.write_text(f"#!/bin/bash\ntouch {marker}\n")
        # Change bytes, not just permissions.
        with frozen.open("a") as stream:
            stream.write("# tampered\n")
        for wrapper in (start, stop):
            result = subprocess.run([str(wrapper)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_check_rejects_active_live_script_and_stop_hook(self):
        self.active(self.script)
        result = self.command("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("handland-532-chain.service", result.stderr)
        self.assertIn("ExecStart", result.stderr)
        self.units.write_text("{}")
        start, _ = self.launch()
        self.active(start, self.script)
        result = self.command("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ExecStopPost", result.stderr)

    def test_probe_failure_refuses_update(self):
        original = self.script.read_bytes()
        self.units.write_text("not json")
        result = self.command("update", self.script, self.replacement)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemctl", result.stderr)
        self.assertEqual(self.script.read_bytes(), original)

    def test_writable_frozen_helper_fails_active_unit_check(self):
        start, stop = self.launch()
        self.active(start, stop)
        helper = next(start.parent.rglob("chain.sh"))
        helper.chmod(0o644)
        result = self.command("check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writable", result.stderr)

    def test_symlink_to_live_dependency_is_refused_before_launch(self):
        outside = self.home / "live.py"
        outside.write_text("print('live')\n")
        (self.source / "helper.py").symlink_to(outside)
        result = self.command("launch", "532-chain", self.script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr.lower())
        self.assertFalse(self.launch_log.exists())

    def test_stop_hook_alone_blocks_update(self):
        other = self.source / "other.sh"
        other.write_text("#!/bin/bash\ntrue\n")
        self.active(other, self.script)
        original = self.script.read_bytes()
        result = self.command("update", self.script, self.replacement)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("handland-532-chain.service", result.stderr)
        self.assertEqual(self.script.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
