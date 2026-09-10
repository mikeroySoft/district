#!/usr/bin/env python3
"""Disposable real-uv District update + offline rollback smoke.

Run after the target ref has successful ``ci.yml`` / ``test``:

    uv run python scripts/smoke_update_uv.py --to <published-ref>

Only temporary XDG/uv directories and read-only GitHub API requests are used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "f6250949983087f3fef7ec8a603ffab5c9ff7e43"
SYSTEMCTL = """#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if args[:1] == ["--user"]: args = args[1:]
if args[:1] != ["show"]: raise SystemExit(90)
prop = next(value.split("=", 1)[1] for value in args if value.startswith("--property="))
print({"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": "not-found", "MainPID": "0"}.get(prop, ""))
"""


def run(argv: list[str], env: dict[str, str], *, ok: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, cwd=ROOT, env=env, text=True, capture_output=True, timeout=1800, check=False)
    if ok and proc.returncode:
        raise SystemExit(f"{shutil.which(argv[0], path=env['PATH']) or argv[0]} exited {proc.returncode}\n{proc.stdout}{proc.stderr}")
    return proc


def sha(tool: Path) -> str:
    direct = list(tool.glob("lib/python*/site-packages/district-*.dist-info/direct_url.json"))
    if len(direct) != 1:
        raise SystemExit("District provenance is missing or ambiguous")
    return json.loads(direct[0].read_text())["vcs_info"]["commit_id"]


def result(proc: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise SystemExit(f"updater returned non-JSON output\n{proc.stdout}{proc.stderr}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", default="main", help="published official ref with successful CI (default main)")
    parser.add_argument("--baseline", default=BASELINE, help="official commit to retain and restore")
    args = parser.parse_args()
    uv, gh = shutil.which("uv"), shutil.which("gh")
    if not uv or not gh:
        raise SystemExit("uv and gh are required")
    with tempfile.TemporaryDirectory(prefix="district-update-smoke-") as directory:
        root = Path(directory)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        systemctl = bin_dir / "systemctl"
        systemctl.write_text(SYSTEMCTL)
        systemctl.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHONPATH": str(ROOT),
            "UV_TOOL_DIR": str(root / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(root / "uv-bin"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
        }
        source = f"git+https://github.com/mikeroySoft/district.git@{args.baseline}"
        run([uv, "tool", "install", "--force", "--link-mode", "copy", source], env)
        tool = Path(env["UV_TOOL_DIR"]) / "district"
        if sha(tool) != args.baseline:
            raise SystemExit("baseline uv install resolved to the wrong commit")
        updated_proc = run([sys.executable, "-m", "district", "update", "--to", args.to, "--yes", "--json"], env)
        updated = result(updated_proc)
        if not updated.get("ok") or sha(tool) != updated["current"]["sha"]:
            raise SystemExit(f"update did not install its reported identity: {updated}")
        blocker = bin_dir / "gh"
        blocker.write_text("#!/bin/sh\necho 'rollback attempted GitHub access' >&2\nexit 99\n")
        blocker.chmod(0o755)
        rolled_proc = run([sys.executable, "-m", "district", "update", "--rollback", "--yes", "--json"], env)
        rolled = result(rolled_proc)
        if not rolled.get("ok") or sha(tool) != args.baseline:
            raise SystemExit(f"rollback did not restore {args.baseline}: {rolled}")
        print(json.dumps({
            "ok": True,
            "baseline": args.baseline,
            "updated": updated["current"],
            "rolled_back": rolled["current"],
            "state": str(root / "state" / "district" / "update" / "state.json"),
        }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
