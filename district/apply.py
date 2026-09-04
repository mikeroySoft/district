"""`district apply [slug] [--upgrade] [--reset slug]`: reconcile the host to the registry.

Idempotent. Rewrites host-side artifacts (units, policy env) without asking;
never modifies a committed file. Exit nonzero when any repo is not converged.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from district import host
from district.host import DistrictError, run, systemctl

POLICY_ENV = ("NPM_CONFIG_MIN_RELEASE_AGE", "UV_EXCLUDE_NEWER")
# doctor rows about committed files: drift the repo owner decides on
DRIFT_LABELS = (".factory.toml keys", "host settings committed", ".github/ISSUE_TEMPLATE/agent_task.md", ".factory.toml committed")
SERVICE_WAIT = 3600  # seconds for running passes to finish before an upgrade
DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(h|d|m|min|s)?$")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- supply-chain policy


def hours(age: str, what: str = "[defaults].min_package_age") -> float:
    m = DURATION.match(str(age).strip())
    if not m:
        raise DistrictError(f"{what} {age!r}: use e.g. \"24h\", \"2d\", \"90min\", or \"0\"")
    n, unit = float(m.group(1)), m.group(2) or "h"
    return n * {"h": 1, "d": 24, "m": 1 / 60, "min": 1 / 60, "s": 1 / 3600}[unit]


def npm_days(age: str) -> int:
    """npm `min-release-age` is whole days; never round a positive age down to 0."""
    return max(1, math.ceil(hours(age) / 24))


def uv_exclude_newer(age: str, at: datetime | None = None) -> str:
    return iso((at or now()) - timedelta(hours=hours(age)))


def policy(data: dict) -> tuple[str, list[str]]:
    """Write the package-age policy into [defaults.install.env]; returns (status, warnings)."""
    age = str(host.default(data, "min_package_age"))
    env = data.setdefault("defaults", {}).setdefault("install", {}).setdefault("env", {})
    warnings = []
    if hours(age) == 0:
        for k in POLICY_ENV:
            env.pop(k, None)
        status = "policy off"
    else:
        env["NPM_CONFIG_MIN_RELEASE_AGE"] = str(npm_days(age))
        env["UV_EXCLUDE_NEWER"] = uv_exclude_newer(age)
        status = f"policy {age} (npm {env['NPM_CONFIG_MIN_RELEASE_AGE']}d, uv {env['UV_EXCLUDE_NEWER']})"
        if shutil.which("cargo"):
            warnings.append("WARN cargo has no minimum-release-age control; crates.io installs are unguarded")
    if not env:
        data["defaults"]["install"].pop("env")
    return status, warnings


# ---------------------------------------------------------------- upgrade


def wait_inactive(services: list[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        busy = [s for s in services if host.is_active(s) in ("active", "activating", "deactivating")]
        if not busy:
            return
        if time.monotonic() > deadline:
            raise DistrictError(f"still running after {int(timeout)}s: {', '.join(busy)}")
        time.sleep(5)


def upgrade(data: dict, active_timers: list[str]) -> None:
    """Stop → wait → reinstall → restart dashboards. Timers are re-enabled by the caller."""
    src = Path(os.path.expanduser(host.default(data, "factory_source")))
    if run(["git", "status", "--porcelain"], cwd=src, check=True).stdout.strip():
        raise DistrictError(f"{src} has uncommitted changes; the installed snapshot must match a commit")
    for timer in active_timers:
        systemctl("disable", "--now", timer)
    print(f"disabled {len(active_timers)} timer(s); waiting for running passes")
    wait_inactive([t.removesuffix(".timer") + ".service" for t in active_timers], SERVICE_WAIT)
    proc = run(["uv", "tool", "install", "--reinstall", "--from", str(src), "agent-factory"])
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise DistrictError("uv tool install failed")
    version = run(["factory", "--version"], check=True).stdout.strip()
    print(f"installed agent-factory {version} from {src}")
    for slug in host.repos(data):
        dash = f"{host.unit_name(slug)}-dashboard.service"
        if (host.unit_dir() / dash).exists():
            systemctl("restart", dash, check=False)


def restore(timers: list[str], data: dict) -> None:
    disabled = {f"{host.unit_name(s)}.timer" for s, t in host.repos(data).items() if t.get("disabled_at")}
    for timer in timers:
        if timer not in disabled and host.is_active(timer) != "active":
            systemctl("enable", "--now", timer, check=False)


# ---------------------------------------------------------------- per repo


def reset(slug: str, table: dict, data: dict) -> bool:
    """Run one pass by hand; clear the cap only if it succeeded."""
    if not table.get("disabled_at"):
        print(f"{slug}: not disabled by District; nothing to reset")
        return True
    service = f"{host.unit_name(slug)}.service"
    print(f"{slug}: running one pass ({service}) before re-enabling the timer")
    proc = systemctl("start", service, check=False)
    if proc.returncode != 0:
        print(f"{slug}: pass failed ({proc.stderr.strip() or proc.returncode}); timer stays disabled")
        return False
    table.pop("disabled_at", None)
    table.pop("disabled_reason", None)
    host.save(data)
    systemctl("enable", "--now", f"{host.unit_name(slug)}.timer")
    print(f"{slug}: pass succeeded; cap cleared, timer re-enabled")
    return True


def repo_pass(slug: str, table: dict, data: dict) -> dict:
    root = Path(table["path"])
    row = {"slug": slug, "version": None, "doctor": "?", "drift": [], "failures": None, "fail": [], "disabled": table.get("disabled_at")}
    unit = host.unit_name(slug)

    def step(*cmd: str) -> str | None:
        proc = run(list(cmd), cwd=root)
        if proc.returncode != 0:
            tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:] or ["?"]
            row["fail"].append(f"{' '.join(cmd[1:])}: {tail[0]}")
            return None
        return proc.stdout

    if not row["disabled"]:
        step("factory", "install")
    step("factory", "init", "--labels-only")
    if (out := step("factory", "doctor", "--json")) is not None:
        try:
            report = json.loads(out)
            rows = report["rows"]
        except (ValueError, KeyError):
            row["fail"].append("doctor: malformed JSON")
        else:
            fails = sum(r["status"] == "FAIL" for r in rows)
            warns = sum(r["status"] == "WARN" for r in rows)
            row["doctor"] = f"FAIL({fails})" if fails else (f"WARN({warns})" if warns else "OK")
            row["drift"] = [r["label"] for r in rows if r["status"] == "WARN" and r["label"] in DRIFT_LABELS]
            if fails:
                row["fail"].append("doctor: " + "; ".join(r["label"] for r in rows if r["status"] == "FAIL"))
    if (out := step("factory", "dashboard", "--json")) is not None:
        try:
            snap = json.loads(out)
            row["version"] = snap["version"]
            row["failures"] = int(snap["dispatcher"]["consecutive_failures"])
        except (ValueError, KeyError, TypeError):
            row["fail"].append("dashboard: malformed JSON")

    cap = int(host.default(data, "max_failed_passes"))
    if row["failures"] is not None and row["failures"] >= cap and host.is_active(f"{unit}.timer") == "active":
        systemctl("disable", "--now", f"{unit}.timer", check=False)
        table["disabled_at"] = iso(now())
        table["disabled_reason"] = f"{row['failures']} consecutive failed passes (cap {cap})"
        host.save(data)
        row["disabled"] = table["disabled_at"]
        print(f"{slug}: {table['disabled_reason']}; timer disabled. Fix the cause, then `district apply --reset {slug}`")
    row["timer"] = host.is_active(f"{unit}.timer")
    return row


def line(row: dict, status: str) -> str:
    parts = [row["slug"], row["version"] or "?", f"doctor {row['doctor']}"]
    parts.append("drift: " + (", ".join(row["drift"]) if row["drift"] else "none"))
    parts.append(f"failures {row['failures'] if row['failures'] is not None else '?'}")
    parts.append(status)
    if row["disabled"]:
        parts.append(f"DISABLED since {row['disabled']}")
    elif row["timer"] != "active":
        parts.append(f"timer {row['timer']}")
    if row["fail"]:
        parts.append("FAIL " + " | ".join(row["fail"]))
    return "  ".join(parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district apply", description=__doc__.split("\n", 1)[0])
    parser.add_argument("slug", nargs="?", help="one repo (owner/name or basename); default all")
    parser.add_argument("--upgrade", action="store_true", help="reinstall agent-factory from [defaults].factory_source first")
    parser.add_argument("--reset", metavar="SLUG", help="run one pass by hand and re-enable the timer if it succeeds")
    args = parser.parse_args(argv)

    data = host.load()
    targets = host.select(data, args.reset or args.slug)
    if not targets:
        print("no repositories registered (district add)")
        return 0
    status, warnings = policy(data)
    host.save(data)

    all_timers = [f"{host.unit_name(s)}.timer" for s in host.repos(data)]
    active = [t for t in all_timers if host.is_active(t) == "active"]
    rows = []
    code = 0
    try:
        if args.upgrade:
            upgrade(data, active)
        for slug, table in targets.items():
            if args.reset and not reset(slug, table, data):
                code = 1
            rows.append(repo_pass(slug, table, data))
    finally:
        if args.upgrade:
            restore(active, data)

    for row in rows:
        print(line(row, status))
    for w in warnings:
        print(w)
    versions = {r["version"] for r in rows}
    if any(r["fail"] or r["disabled"] or r["timer"] != "active" for r in rows):
        code = 1
    if len(versions) > 1:
        print(f"versions differ across the fleet: {', '.join(str(v) for v in sorted(versions, key=str))}")
        code = 1
    return code
