"""`district doctor`: check District's host-side prerequisites."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from district import host
from district.add import remote_slug
from district.apply import POLICY_ENV

UNITS = (
    "district-apply.timer",
    "district-health.timer",
    "district-metrics.timer",
    "district-dashboard.service",
)
POLICY_TOOLS = {"uv": POLICY_ENV[1], "npm": POLICY_ENV[0]}


def row(status: str, label: str, detail: str) -> dict[str, str]:
    return {"status": status, "label": label, "detail": detail}

def run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return host.run(argv)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args])


def command(label: str, argv: list[str], detail: str) -> dict[str, str]:
    proc = run(argv)
    return row("PASS", label, detail) if proc.returncode == 0 else row("FAIL", label, f"{' '.join(argv)} exited {proc.returncode}")


def factory_rows(data: dict) -> list[dict[str, str]]:
    if not shutil.which("factory"):
        return [row("FAIL", "factory", "not found on PATH")]
    proc = run(["factory", "--version"])
    if proc.returncode:
        return [row("FAIL", "factory", f"factory --version exited {proc.returncode}")]
    version = (proc.stdout or proc.stderr).strip()
    expected = data.get("defaults", {}).get("engine", {}).get("sha")
    installed = next(iter(re.findall(r"\b[0-9a-fA-F]{7,40}\b", version)), None)
    if expected and installed and not (installed.lower().startswith(str(expected).lower()) or str(expected).lower().startswith(installed.lower())):
        return [row("WARN", "factory", f"installed {installed}, expected {expected}")]
    return [row("PASS", "factory", version)]


def repo_rows(data: dict) -> list[dict[str, str]]:
    rows = []
    for slug, table in host.repos(data).items():
        path = Path(os.path.expanduser(table["path"]))
        if not path.exists():
            rows.append(row("FAIL", f"repo {slug}", f"{path} does not exist"))
            continue
        proc = run(["git", "-C", str(path), "remote", "get-url", "origin"])
        actual = remote_slug(proc.stdout.strip()) if proc.returncode == 0 else None
        if actual != slug.lower():
            detail = f"origin resolves to {actual or 'nothing'}, expected {slug}" if proc.returncode == 0 else f"git remote get-url origin exited {proc.returncode}"
            rows.append(row("FAIL", f"repo {slug}", detail))
        else:
            rows.append(row("PASS", f"repo {slug}", f"{path}: origin resolves to {slug}"))
    return rows


def port_rows(data: dict) -> list[dict[str, str]]:
    rows = []
    by_port: dict[int, list[str]] = {}
    for slug, table in host.repos(data).items():
        port = table.get("dashboard", {}).get("port")
        if port is not None:
            by_port.setdefault(port, []).append(slug)
    for port, slugs in sorted(by_port.items()):
        if len(slugs) > 1:
            rows.append(row("FAIL", f"dashboard port {port}", f"shared by {', '.join(sorted(slugs))}"))
            continue
        slug = slugs[0]
        unit = f"{host.unit_name(slug)}-dashboard.service"
        proc = run(["ss", "-ltnp", "sport", "=", f":{port}"])
        if proc.returncode:
            rows.append(row("FAIL", f"dashboard port {port}", f"ss -ltnp exited {proc.returncode}"))
            continue
        listeners = re.findall(r'users:\(\("([^"\n]+)",pid=(\d+)', proc.stdout)
        if not listeners:
            rows.append(row("PASS", f"dashboard port {port}", "free"))
            continue
        main_pid = systemctl("show", "-p", "MainPID", "--value", unit).stdout.strip()
        foreign = next(((process, pid) for process, pid in listeners if pid != main_pid), None)
        if foreign:
            process, pid = foreign
            rows.append(row("FAIL", f"dashboard port {port}", f"held by {process} (pid {pid}), not {unit}"))
        else:
            rows.append(row("PASS", f"dashboard port {port}", f"held by {unit}"))
    return rows


def policy_rows(data: dict) -> list[dict[str, str]]:
    rows = []
    env = data.get("defaults", {}).get("install", {}).get("env", {})
    for tool, key in POLICY_TOOLS.items():
        if shutil.which(tool):
            status = "PASS" if key in env else "WARN"
            detail = f"{key} set" if status == "PASS" else f"{key} missing: district apply writes it"
            rows.append(row(status, f"policy {tool}", detail))
    if shutil.which("cargo"):
        rows.append(row("WARN", "policy cargo", "cargo has no minimum-release-age control; crates.io installs are unguarded"))
    return rows


def unit_rows() -> list[dict[str, str]]:
    rows = []
    for unit in UNITS:
        loaded = systemctl("show", "-p", "LoadState", "--value", unit).stdout.strip()
        if loaded in ("", "not-found"):
            continue
        active = systemctl("is-active", unit)
        state = active.stdout.strip()
        status = "PASS" if state == "active" else "WARN"
        detail = "active" if status == "PASS" else f"{state or 'inactive'}: district dashboard --install"
        rows.append(row(status, unit, detail))
    return rows


def check() -> list[dict[str, str]]:
    rows = []
    try:
        data = host.load()
        rows.append(row("PASS", "host file", str(host.path())))
    except host.DistrictError as exc:
        data = {}
        rows.append(row("FAIL", "host file", str(exc).removeprefix("district: ")))

    rows += factory_rows(data)
    rows.append(command("gh auth", ["gh", "auth", "status"], "authenticated"))

    manager = systemctl("is-system-running")
    state = manager.stdout.strip() or manager.stderr.strip() or f"exited {manager.returncode}"
    rows.append(row("PASS" if state in ("running", "degraded") else "FAIL", "systemd user manager", state))

    linger = run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"])
    rows.append(row("PASS", "linger", "yes") if linger.returncode == 0 and linger.stdout.strip() == "yes" else row("WARN", "linger", "timers stop at logout: loginctl enable-linger"))

    rows += repo_rows(data)
    rows += port_rows(data)

    clone_dir = Path(os.path.expanduser(host.default(data, "clone_dir")))
    rows.append(row("PASS", "clone_dir", str(clone_dir)) if clone_dir.is_dir() else row("WARN", "clone_dir", f"{clone_dir} is not a directory"))
    rows += policy_rows(data)
    rows += unit_rows()
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="district doctor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = check()
    ok = not any(item["status"] == "FAIL" for item in rows)
    if args.json:
        print(json.dumps({"ok": ok, "rows": rows}))
    else:
        for item in rows:
            print(f"  {item['status']}  {item['label']}: {item['detail']}")
    return 0 if ok else 1
