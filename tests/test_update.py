"""Behavioral self-update checks with isolated uv/systemd/GitHub command doubles.

Run: uv run python -m unittest discover -s tests -p 'test_update.py'
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = "f6250949983087f3fef7ec8a603ffab5c9ff7e43"
NEW = "1aeea0609216aabbccddeeff0011223344556677"


MAIN = '''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse, json, sys
from district import SHA, VERSION
if sys.argv[1:] == ["--version"]:
    print(VERSION)
    raise SystemExit(0)
if sys.argv[1:3] != ["dashboard", "--host"] and sys.argv[1:2] != ["dashboard"]:
    raise SystemExit(2)
parser = argparse.ArgumentParser()
parser.add_argument("command")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8760)
parser.add_argument("--no-open", action="store_true")
args = parser.parse_args()
class Handler(BaseHTTPRequestHandler):
    revision = 0
    def do_GET(self):
        type(self).revision += 1
        body = json.dumps({"revision": type(self).revision, "fleet": {"fixture": {}}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
'''


UV = r'''#!/usr/bin/python3
import json, os, pathlib, re, shutil, sys
cfg_path = pathlib.Path(os.environ["UPDATE_FIXTURE"])
cfg = json.loads(cfg_path.read_text())
args = sys.argv[1:]
with pathlib.Path(cfg["uv_log"]).open("a") as log: log.write(" ".join(args) + "\n")
if args[:2] == ["tool", "dir"]:
    print(cfg["bin_dir"] if "--bin" in args else cfg["tool_dir"])
    raise SystemExit(0)
if args[:2] != ["tool", "install"]: raise SystemExit(2)
source = args[-1]
match = re.search(r"@([0-9a-f]{40})$", source)
if not match: raise SystemExit(3)
sha = match.group(1)
tool_dir = pathlib.Path(os.environ.get("UV_TOOL_DIR", cfg["tool_dir"]))
bin_dir = pathlib.Path(os.environ.get("UV_TOOL_BIN_DIR", cfg["bin_dir"]))
live = tool_dir == pathlib.Path(cfg["tool_dir"])
if live and cfg.get("fail_live"):
    shutil.rmtree(tool_dir / "district", ignore_errors=True)
    cfg["fail_live"] = False; cfg_path.write_text(json.dumps(cfg))
    print("simulated live install failure", file=sys.stderr)
    raise SystemExit(9)
version = cfg["target_version"]
tool = tool_dir / "district"
shutil.rmtree(tool, ignore_errors=True)
pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
site = tool / "lib" / pyver / "site-packages"
info = site / f"district-{version}.dist-info"
package = site / "district"
(tool / "bin").mkdir(parents=True); info.mkdir(parents=True); package.mkdir()
(tool / "bin" / "python3").symlink_to("/usr/bin/python3")
(tool / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion_info = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\ninclude-system-site-packages = false\n")
(package / "__init__.py").write_text(f"VERSION = {version!r}\nSHA = {sha!r}\n")
(package / "__main__.py").write_text(cfg["main_source"])
(info / "METADATA").write_text(f"Metadata-Version: 2.5\nName: district\nVersion: {version}\nRequires-Python: >=3.11\n")
(info / "direct_url.json").write_text(json.dumps({"url": "https://github.com/mikeroySoft/district.git", "vcs_info": {"vcs": "git", "commit_id": sha, "requested_revision": sha}}))
receipt_source = f"https://github.com/mikeroySoft/district.git?rev={sha}"
(tool / "uv-receipt.toml").write_text(f"""[tool]\nrequirements = [{{ name = "district", git = "{receipt_source}" }}]\nentrypoints = [{{ name = "district", install-path = "{bin_dir / 'district'}", from = "district" }}]\n""")
bin_dir.mkdir(parents=True, exist_ok=True)
inner = tool / "bin" / "district"
inner.write_text(f"#!{tool / 'bin' / 'python3'}\nfrom district.__main__ import *\n")
inner.chmod(0o755)
entry = bin_dir / "district"
entry.unlink(missing_ok=True)
entry.symlink_to(inner)
'''

GH = r'''#!/usr/bin/python3
import base64, json, os, pathlib, re, sys
cfg = json.loads(pathlib.Path(os.environ["UPDATE_FIXTURE"]).read_text())
args = sys.argv[1:]
with pathlib.Path(cfg["gh_log"]).open("a") as log: log.write(" ".join(args) + "\n")
endpoint = args[args.index("GET") + 1]
sha, ci = cfg["target_sha"], cfg["ci"]
if "/commits/" in endpoint:
    requested = endpoint.rsplit("/", 1)[-1]
    out = {"sha": requested if re.fullmatch(r"[0-9a-f]{40}", requested) else sha}
elif "/contents/pyproject.toml" in endpoint:
    text = f'[project]\nname = "district"\nversion = "{cfg["target_version"]}"\n'
    out = {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
elif "/actions/workflows/ci.yml/runs" in endpoint:
    runs = [] if ci == "missing" else [{"id": 71, "run_attempt": 1, "head_sha": sha, "status": "queued" if ci == "pending" else "completed", "conclusion": "failure" if ci == "failed" else "success"}]
    out = {"workflow_runs": runs}
elif "/actions/runs/71/jobs" in endpoint:
    out = {"jobs": [] if ci == "job-missing" else [{"id": 81, "name": "test", "status": "queued" if ci == "job-pending" else "completed", "conclusion": "failure" if ci == "job-failed" else "success"}]}
else:
    raise SystemExit(4)
print(json.dumps(out))
'''


SYSTEMCTL = r'''#!/usr/bin/python3
import json, os, pathlib, signal, subprocess, sys, time
cfg_path = pathlib.Path(os.environ["UPDATE_FIXTURE"])
cfg = json.loads(cfg_path.read_text())
args = sys.argv[1:]
if args[:1] == ["--user"]: args = args[1:]
with pathlib.Path(cfg["systemctl_log"]).open("a") as log: log.write(" ".join(args) + "\n")
units = cfg["units"]
def save(): cfg_path.write_text(json.dumps(cfg))
def stop(name):
    row = units[name]
    pid = int(row.get("MainPID", "0"))
    if pid:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
        for _ in range(50):
            try: os.kill(pid, 0)
            except ProcessLookupError: break
            time.sleep(.01)
    row["MainPID"] = "0"; row["ActiveState"] = "inactive"
def start(name):
    row = units[name]
    if name == "district-dashboard.service":
        if cfg.get("fail_starts", 0):
            cfg["fail_starts"] -= 1; save(); return False
        argv = [str(pathlib.Path(cfg["tool_dir"]) / "district/bin/python3"), "-m", "district", "dashboard", "--host", "0.0.0.0", "--port", str(cfg["port"]), "--no-open"]
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        child = subprocess.Popen(argv, cwd=cfg["tool_dir"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        row["MainPID"] = str(child.pid)
    row["ActiveState"] = "active"
    return True
if args[:1] == ["show"]:
    unit = args[1]; prop = next(value.split("=", 1)[1] for value in args if value.startswith("--property="))
    row = units.get(unit, {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": "not-found", "MainPID": "0"})
    if prop == "ExecStart":
        python = pathlib.Path(cfg["tool_dir"]) / "district/bin/python3"
        print(f"{{ path={python} ; argv[]={python} -m district dashboard --host 0.0.0.0 --port {cfg['port']} --no-open ; ignore_errors=no ; }}")
    else: print(row.get(prop, ""))
    raise SystemExit(0)
if args[:1] == ["stop"]:
    for name in args[1:]: stop(name)
    save(); raise SystemExit(0)
if args[:1] == ["start"]:
    ok = all(start(name) for name in args[1:])
    save(); raise SystemExit(0 if ok else 7)
raise SystemExit(2)
'''


class UpdateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.tools = self.tmp / "share" / "uv" / "tools"
        self.tool_bin = self.tmp / "local-bin"
        self.state_home = self.tmp / "state"
        self.cache_home = self.tmp / "cache"
        self.config_home = self.tmp / "config"
        for path in (self.bin, self.tools, self.tool_bin):
            path.mkdir(parents=True)
        self.port = self.free_port()
        units = {
            name: {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": "not-found", "MainPID": "0"}
            for name in (
                "district-dashboard.service", "district-metrics.service", "district-metrics.timer",
                "district-apply.service", "district-apply.timer",
            )
        }
        self.config = {
            "tool_dir": str(self.tools), "bin_dir": str(self.tool_bin), "target_sha": NEW,
            "target_version": "0.2.0", "ci": "success", "main_source": textwrap.dedent(MAIN),
            "uv_log": str(self.tmp / "uv.log"), "gh_log": str(self.tmp / "gh.log"),
            "systemctl_log": str(self.tmp / "systemctl.log"), "units": units, "port": self.port,
        }
        self.config_path = self.tmp / "fixture.json"
        self.write_config()
        self.script("uv", UV)
        self.script("gh", GH)
        self.script("systemctl", SYSTEMCTL)
        self.write_tool(OLD, "0.1.0")
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT),
            "UPDATE_FIXTURE": str(self.config_path),
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_CACHE_HOME": str(self.cache_home),
            "XDG_CONFIG_HOME": str(self.config_home),
        }
        self.addCleanup(self.stop_dashboard)

    @staticmethod
    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def write_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config))

    def reload_config(self) -> None:
        self.config = json.loads(self.config_path.read_text())

    def script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def write_tool(self, sha: str, version: str) -> None:
        config = dict(self.config)
        config.update(target_sha=sha, target_version=version)
        self.config_path.write_text(json.dumps(config))
        subprocess.run(
            [str(self.bin / "uv"), "tool", "install", f"git+https://github.com/mikeroySoft/district.git@{sha}"],
            env={**os.environ, "UPDATE_FIXTURE": str(self.config_path)}, check=True, capture_output=True, text=True,
        )
        self.write_config()
        for log in (self.tmp / "uv.log", self.tmp / "gh.log", self.tmp / "systemctl.log"):
            log.unlink(missing_ok=True)

    def use_local_snapshot(self) -> tuple[Path, str]:
        tool = self.tools / "district"
        site = next(tool.glob("lib/python*/site-packages"))
        source = self.tmp / "district-source"
        shutil.copytree(site / "district", source / "district")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "District Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "district@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(source), "remote", "add", "origin", "git@github.com:mikeroySoft/district.git"], check=True)
        subprocess.run(["git", "-C", str(source), "add", "district"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "snapshot"], check=True)
        sha = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        info = next(site.glob("district-*.dist-info"))
        (info / "direct_url.json").write_text(json.dumps({"url": source.as_uri(), "dir_info": {}}))
        (tool / "uv-receipt.toml").write_text(
            f"""[tool]\nrequirements = [{{ name = "district", directory = "{source}" }}]\n"""
            f"""entrypoints = [{{ name = "district", install-path = "{self.tool_bin / 'district'}", from = "district" }}]\n"""
        )
        return source, sha

    def run_update(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "district", "update", *args], cwd=ROOT, env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
        )

    def data(self, proc: subprocess.CompletedProcess) -> dict:
        self.assertTrue(proc.stdout.strip(), proc.stderr)
        return json.loads(proc.stdout)

    def identity(self) -> dict:
        code = (
            "import json,sys; from pathlib import Path; "
            "p=next(Path(sys.argv[1]).glob('lib/python*/site-packages/district-*.dist-info/direct_url.json')); "
            "d=json.loads(p.read_text()); print(d['vcs_info']['commit_id'])"
        )
        proc = subprocess.run([sys.executable, "-c", code, str(self.tools / "district")], capture_output=True, text=True, check=True)
        return {"sha": proc.stdout.strip()}

    def set_unit(self, name: str, active: bool, enabled: bool = True) -> None:
        self.config["units"][name] = {
            "LoadState": "loaded", "ActiveState": "active" if active else "inactive",
            "UnitFileState": "enabled" if enabled else "disabled", "MainPID": "0",
        }
        self.write_config()

    def start_dashboard(self) -> None:
        self.set_unit("district-dashboard.service", False)
        subprocess.run([str(self.bin / "systemctl"), "--user", "start", "district-dashboard.service"], env=self.env, check=True)
        self.reload_config()

    def stop_dashboard(self) -> None:
        if not self.config_path.exists():
            return
        self.reload_config()
        row = self.config["units"].get("district-dashboard.service", {})
        if row.get("MainPID") not in (None, "0"):
            subprocess.run([str(self.bin / "systemctl"), "--user", "stop", "district-dashboard.service"], env=self.env, check=False)

    def log(self, name: str) -> list[str]:
        path = self.tmp / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_ci_selection_fails_closed_and_accepts_only_ci_test(self) -> None:
        for status, expected in (("missing", "missing"), ("pending", "pending"), ("failed", "failed"),
                                 ("job-missing", "missing"), ("job-pending", "pending"), ("job-failed", "failed")):
            with self.subTest(status=status):
                self.config["ci"] = status; self.write_config()
                proc = self.run_update("--dry-run", "--json")
                result = self.data(proc)
                self.assertEqual((proc.returncode, result["status"], result["ci"]["status"]), (1, "ineligible", expected))
        self.config["ci"] = "success"; self.write_config()
        proc = self.run_update("--dry-run", "--json")
        result = self.data(proc)
        self.assertEqual((proc.returncode, result["status"], result["ci"]["eligible"]), (0, "plan", True))
        self.assertEqual(result["ci"]["job"], "test")

    def test_dry_run_and_same_sha_create_no_update_state_or_lock(self) -> None:
        proc = self.run_update("--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.state_home / "district" / "update").exists())
        self.assertFalse((self.cache_home / "district" / "apply.lock").exists())
        self.assertFalse(any("install" in line for line in self.log("uv")))
        self.config["target_sha"] = OLD
        self.config["target_version"] = "0.1.0"
        self.config["ci"] = "pending"
        self.write_config()
        proc = self.run_update("--yes", "--json")
        self.assertEqual((proc.returncode, self.data(proc)["status"]), (0, "up-to-date"))
        self.assertTrue(self.data(proc)["ok"])
        self.assertFalse((self.state_home / "district" / "update").exists())

    def test_clean_official_local_snapshot_is_bootstrap_compatible(self) -> None:
        _, sha = self.use_local_snapshot()
        proc = self.run_update("--dry-run", "--json")
        result = self.data(proc)
        self.assertEqual((proc.returncode, result["installed"]["sha"], result["ok"]), (0, sha, True))
        self.assertFalse((self.state_home / "district" / "update").exists())

    def test_local_snapshot_or_installed_package_changes_are_rejected(self) -> None:
        source, _ = self.use_local_snapshot()
        (source / "district" / "__init__.py").write_text("changed = True\n")
        changed_source = self.run_update("--dry-run", "--json")
        self.assertIn("local changes", self.data(changed_source)["error"])

        subprocess.run(["git", "-C", str(source), "checkout", "--", "district/__init__.py"], check=True)
        site = next((self.tools / "district").glob("lib/python*/site-packages"))
        (site / "district" / "__init__.py").write_text("changed = True\n")
        changed_install = self.run_update("--dry-run", "--json")
        self.assertIn("does not match", self.data(changed_install)["error"])

    def test_foreign_or_non_git_local_snapshot_is_rejected(self) -> None:
        source, _ = self.use_local_snapshot()
        subprocess.run(
            ["git", "-C", str(source), "remote", "set-url", "origin", "git@github.com:someone-else/district.git"],
            check=True,
        )
        foreign = self.run_update("--dry-run", "--json")
        self.assertIn("foreign Git origin", self.data(foreign)["error"])

        subprocess.run(
            ["git", "-C", str(source), "remote", "set-url", "origin", "git@github.com:mikeroySoft/district.git"],
            check=True,
        )
        shutil.rmtree(source / ".git")
        non_git = self.run_update("--dry-run", "--json")
        self.assertIn("not a valid Git snapshot", self.data(non_git)["error"])


    def test_invalid_and_option_shaped_refs_do_not_reach_github(self) -> None:
        for ref in ("", "../main", "bad//ref", ".hidden", "topic.lock"):
            with self.subTest(ref=ref):
                before = list(self.log("gh"))
                proc = self.run_update("--to", ref, "--dry-run", "--json")
                self.assertEqual(proc.returncode, 1)
                self.assertIn("invalid official repository ref", self.data(proc)["error"])
                self.assertEqual(self.log("gh"), before)
        proc = self.run_update("--to=-main", "--dry-run", "--json")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("invalid official repository ref", self.data(proc)["error"])

    def test_install_failure_restores_provenance_and_only_active_timer(self) -> None:
        self.set_unit("district-metrics.timer", True)
        self.set_unit("district-apply.timer", False, enabled=False)
        self.config["fail_live"] = True; self.write_config()
        proc = self.run_update("--yes", "--json")
        result = self.data(proc)
        self.assertEqual((proc.returncode, result["status"], result["rollback"]["ok"]), (1, "rolled-back", True))
        self.assertEqual(self.identity()["sha"], OLD)
        state = json.loads((self.state_home / "district" / "update" / "state.json").read_text())
        self.assertEqual(state["current"]["sha"], OLD)
        calls = self.log("systemctl")
        self.assertFalse(any("enable" in call or "disable" in call for call in calls))
        self.assertTrue(any(call == "start district-metrics.timer" for call in calls))
        self.assertFalse(any("district-apply.timer" in call and call.startswith(("start", "stop")) for call in calls))

    def test_startup_failure_rolls_back_then_proves_old_collector_publication(self) -> None:
        self.start_dashboard()
        self.reload_config(); self.config["fail_starts"] = 1; self.write_config()
        proc = self.run_update("--yes", "--json")
        result = self.data(proc)
        self.assertEqual((proc.returncode, result["status"], result["rollback"]["ok"]), (1, "rolled-back", True))
        self.assertEqual(self.identity()["sha"], OLD)
        self.assertGreater(result["dashboard"]["revision"], 1)
        self.assertEqual(result["dashboard"]["factories"], 1)

    def test_successful_update_then_offline_rollback_swaps_retained_artifacts(self) -> None:
        updated = self.run_update("--yes", "--json")
        self.assertEqual((updated.returncode, self.data(updated)["status"], self.identity()["sha"]), (0, "updated", NEW))
        (self.bin / "gh").unlink()
        rolled = self.run_update("--rollback", "--yes", "--json")
        result = self.data(rolled)
        self.assertEqual((rolled.returncode, result["status"], self.identity()["sha"]), (0, "rolled-back", OLD))
        state = json.loads((self.state_home / "district" / "update" / "state.json").read_text())
        self.assertEqual((state["current"]["sha"], state["previous"]["sha"]), (OLD, NEW))

    def test_empty_registry_dashboard_needs_no_fabricated_publication(self) -> None:
        self.config["main_source"] = textwrap.dedent(MAIN).replace(
            'type(self).revision += 1', 'pass').replace('{"fixture": {}}', '{}')
        self.write_tool(OLD, "0.1.0")
        self.start_dashboard()
        proc = self.run_update("--yes", "--json")
        result = self.data(proc)
        self.assertEqual((proc.returncode, result["status"]), (0, "updated"), result)
        self.assertEqual((result["dashboard"]["revision"], result["dashboard"]["factories"]), (0, 0))

    def test_offline_local_snapshot_rollback_survives_deleted_source(self) -> None:
        source, sha = self.use_local_snapshot()
        updated = self.run_update("--yes", "--json")
        self.assertEqual(updated.returncode, 0, self.data(updated))
        shutil.rmtree(source)
        (self.bin / "gh").unlink()
        rolled = self.run_update("--rollback", "--yes", "--json")
        self.assertEqual((rolled.returncode, self.data(rolled)["current"]["sha"]), (0, sha))
        returned = self.run_update("--rollback", "--yes", "--json")
        self.assertEqual((returned.returncode, self.data(returned)["current"]["sha"]), (0, NEW))

    def test_missing_old_artifact_does_not_block_safe_update_planning(self) -> None:
        updated = self.run_update("--yes", "--json")
        self.assertEqual(updated.returncode, 0, self.data(updated))
        shutil.rmtree(self.data(updated)["previous"]["artifact"])
        planned = self.run_update("--dry-run", "--json")
        self.assertEqual((planned.returncode, self.data(planned)["status"]), (0, "up-to-date"))
        self.assertIn("unavailable", self.data(planned)["warnings"][0])
        rolled = self.run_update("--rollback", "--yes", "--json")
        self.assertEqual(rolled.returncode, 1)

    def test_apply_lock_excludes_update_before_preparation(self) -> None:
        lock = self.cache_home / "district" / "apply.lock"
        lock.parent.mkdir(parents=True)
        with lock.open("w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = self.run_update("--yes", "--json")
        result = self.data(proc)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("another District apply, metrics, or update", result["error"])
        self.assertFalse(any("install" in line for line in self.log("uv")))

    def test_next_invocation_recovers_a_hard_interruption_without_github(self) -> None:
        from district import update_transaction as tx

        root = self.state_home / "district" / "update"
        current = tx.installed_identity(self.tools / "district")
        artifact = tx.copy_artifact(
            {
                "state_root": str(root),
                "tool": str(self.tools / "district"),
                "entrypoint": str(self.tool_bin / "district"),
            },
            current,
        )
        shutil.copy2(tx.__file__, root / "transaction.py")
        services = {
            name: {"loaded": False, "active_state": "inactive", "unit_file_state": "not-found", "main_pid": "0"}
            for name in tx.UNITS
        }
        request = {
            "schema": 1, "action": "update", "state_root": str(root), "tool": str(self.tools / "district"),
            "entrypoint": str(self.tool_bin / "district"), "uv": str(self.bin / "uv"),
            "lock": str(self.cache_home / "district" / "apply.lock"),
            "systemctl": str(self.bin / "systemctl"), "python_version": "3.14.0", "current": current,
            "target": {"sha": NEW, "version": "0.2.0", "source": f"https://github.com/mikeroySoft/district.git?rev={NEW}"},
            "install_source": f"git+https://github.com/mikeroySoft/district.git@{NEW}", "target_artifact": None,
            "previous_artifact": None, "stage": str(root / "stage"),
        }
        tx.atomic_json(root / "transaction.json", {
            "schema": 1, "phase": "installing", "request": request, "current": current,
            "backup": str(artifact), "services": services,
            "state_before": {"schema": 1, "current": current, "previous": None},
        })
        shutil.rmtree(self.tools / "district")
        (self.bin / "gh").unlink()
        unconfirmed = self.run_update("--json")
        self.assertEqual(unconfirmed.returncode, 1)
        self.assertIn("confirmation", self.data(unconfirmed)["error"])
        self.assertTrue((root / "transaction.json").exists())
        self.assertFalse(self.tools.joinpath("district").exists())
        lock_path = Path(request["lock"])
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = subprocess.run(
                ["/usr/bin/python3", str(root / "transaction.py"), "recover", str(root)],
                env=self.env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("another District", blocked.stderr)
            self.assertFalse(self.tools.joinpath("district").exists())
            self.assertTrue((root / "transaction.json").exists())
        proc = self.run_update("--yes", "--json")
        result = self.data(proc)
        self.assertEqual((proc.returncode, result["status"], result["rollback"]["ok"]), (0, "recovered", True))
        self.assertEqual(self.identity()["sha"], OLD)
        self.assertFalse((root / "transaction.json").exists())


if __name__ == "__main__":
    unittest.main()
