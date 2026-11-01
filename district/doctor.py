"""`district doctor`: check District's host-side prerequisites."""

from __future__ import annotations

import argparse
import copy
from ipaddress import ip_address
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from district import host
from district.add import remote_slug
from district.apply import POLICY_ENV
from district.service import SystemdUserBackend

UNITS = (
    "district-apply.timer",
    "district-health.timer",
    "district-metrics.timer",
    "district-dashboard.service",
)
POLICY_TOOLS = {"uv": POLICY_ENV[1], "npm": POLICY_ENV[0]}
TOOLCHAINS = {"macos-xcode"}
HOST_COMMAND_TIMEOUT = 30
SSH_CONNECT_TIMEOUT = 10




def row(status: str, label: str, detail: str) -> dict[str, str]:
    return {"status": status, "label": label, "detail": detail}

def run(argv: list[str], *, check: bool = False, env: dict[str, str] | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        # Doctor turns command failures into rows; service probes must not raise.
        return host.run(argv, env=env, timeout=timeout)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return subprocess.CompletedProcess(argv, 124, stdout, stderr or f"timed out after {exc.timeout}s")




def gh_auth() -> dict[str, str]:
    proc = run(["gh", "auth", "status"])
    if proc.returncode == 0:
        return row("PASS", "gh auth", "authenticated")
    lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
    return row("FAIL", "gh auth", lines[-1].strip() if lines else f"gh auth status exited {proc.returncode}")


def _host_run(name: str, table: dict, argv: list[str], *,
              env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if name == "local":
        return run(argv, env={**os.environ, **(env or {})}, timeout=HOST_COMMAND_TIMEOUT)
    command = shlex.join(argv)
    if env:
        assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        command = f"env {assignments} {command}"
    return run([
        "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        table["ssh"], "--", command,
    ], timeout=HOST_COMMAND_TIMEOUT)


def _failure(proc: subprocess.CompletedProcess, command: str) -> str:
    lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
    return lines[-1].strip() if lines else f"{command} exited {proc.returncode}"


def _version_row(name: str, label: str, argv: list[str], runner,
                 env: dict[str, str]) -> dict[str, str]:
    proc = runner(argv, env=env)
    if proc.returncode:
        return row("FAIL", f"host {name} macos-xcode {label}", _failure(proc, " ".join(argv)))
    detail = (proc.stdout or proc.stderr).strip()
    return row(
        "PASS" if detail else "FAIL",
        f"host {name} macos-xcode {label}",
        detail or f"{' '.join(argv)} returned no version",
    )


def macos_xcode_rows(name: str, table: dict) -> list[dict[str, str]]:
    runner = lambda argv, **kwargs: _host_run(name, table, argv, **kwargs)
    path_proc = runner(["printenv", "PATH"])
    if path_proc.returncode:
        return [row("FAIL", f"host {name} macos-xcode", _failure(path_proc, "printenv PATH"))]
    raw_path = path_proc.stdout.strip()
    if not raw_path:
        return [row("FAIL", f"host {name} macos-xcode", "printenv PATH returned no value")]
    path = raw_path if "/opt/homebrew/bin" in raw_path.split(":") else f"{raw_path}:/opt/homebrew/bin"
    env = {"PATH": path}
    rows = [row("PASS", f"host {name} macos-xcode launchd PATH", f"/opt/homebrew/bin in {path}")]

    xcode = runner(["xcodebuild", "-version"], env=env)
    xcode_label = f"host {name} macos-xcode xcodebuild"
    if xcode.returncode:
        rows.append(row("FAIL", xcode_label, _failure(xcode, "xcodebuild -version")))
    elif not (version := re.search(r"^Xcode\s+(.+)$", xcode.stdout, re.MULTILINE)):
        rows.append(row("FAIL", xcode_label, "xcodebuild -version returned no Xcode version"))
    elif expected := table.get("xcode_version"):
        actual = version.group(1).strip()
        rows.append(row(
            "PASS" if actual == expected else "WARN",
            xcode_label,
            xcode.stdout.strip() if actual == expected else f"installed {actual}, expected {expected}",
        ))
    else:
        rows.append(row("PASS", xcode_label, xcode.stdout.strip()))

    brew = runner(["brew", "--prefix", "--installed", "xcodegen"], env=env)
    prefix = brew.stdout.strip()
    if brew.returncode:
        rows.append(row("FAIL", f"host {name} macos-xcode xcodegen", _failure(brew, "brew --prefix --installed xcodegen")))
    elif not prefix.startswith("/opt/homebrew/"):
        rows.append(row("FAIL", f"host {name} macos-xcode xcodegen", f"brew prefix is {prefix or 'empty'}"))
    else:
        version = runner(["xcodegen", "--version"], env=env)
        detail = (version.stdout or version.stderr).strip()
        if version.returncode:
            rows.append(row("FAIL", f"host {name} macos-xcode xcodegen", _failure(version, "xcodegen --version")))
        elif not detail:
            rows.append(row("FAIL", f"host {name} macos-xcode xcodegen", "xcodegen --version returned no version"))
        else:
            rows.append(row("PASS", f"host {name} macos-xcode xcodegen", f"{detail} ({prefix})"))

    for tool in ("swift", "cargo", "rustc"):
        rows.append(_version_row(name, tool, [tool, "--version"], runner, env))

    auth = runner(["gh", "auth", "status"], env=env)
    rows.append(row(
        "PASS" if auth.returncode == 0 else "FAIL",
        f"host {name} macos-xcode gh auth",
        "authenticated" if auth.returncode == 0 else _failure(auth, "gh auth status"),
    ))

    runtimes = runner(["xcrun", "simctl", "list", "runtimes", "--json"], env=env)
    runtime_label = f"host {name} macos-xcode iOS Simulator"
    if runtimes.returncode:
        rows.append(row("FAIL", runtime_label, _failure(runtimes, "xcrun simctl list runtimes --json")))
    else:
        try:
            available = [
                item["name"] for item in json.loads(runtimes.stdout)["runtimes"]
                if item.get("isAvailable", True)
                and (str(item.get("name", "")).startswith("iOS ")
                     or ".SimRuntime.iOS-" in str(item.get("identifier", "")))
            ]
        except (KeyError, TypeError, ValueError):
            available = []
        rows.append(row(
            "PASS" if available else "FAIL",
            runtime_label,
            ", ".join(available) if available else "no available iOS Simulator runtime",
        ))
    return rows


def toolchain_rows(data: dict) -> list[dict[str, str]]:
    requirements: dict[str, set[str]] = {}
    rows = []
    for slug, table in host.repos(data).items():
        if "toolchain" not in table:
            continue
        profile = table["toolchain"]
        if not isinstance(profile, str) or not profile:
            rows.append(row("FAIL", f"repo {slug} toolchain", "must be a non-empty string"))
            continue
        for name in host.repo_hosts(table, slug):
            requirements.setdefault(name, set()).add(profile)
    configured = {"local": {
        "platform": "macos" if sys.platform == "darwin" else "linux",
    }, **host.hosts(data)}
    for name, profiles in sorted(requirements.items()):
        table = configured[name]
        for profile in sorted(profiles):
            if profile not in TOOLCHAINS:
                rows.append(row("FAIL", f"host {name} toolchain", f"unknown profile {profile!r}"))
            elif table["platform"] != "macos":
                rows.append(row("FAIL", f"host {name} macos-xcode",
                                f"requires platform macos, found {table['platform']}"))
            else:
                rows += macos_xcode_rows(name, table)
    return rows




def _config_strings(table: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    values = []
    for key, value in table.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            values += _config_strings(value, path)
        elif isinstance(value, str):
            values.append((".".join(path), value))
    return values


def _is_loopback_url(value: str) -> bool:
    try:
        hostname = urlsplit(value).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _controller_path_rows(name: str, table: dict, defaults: dict) -> list[dict[str, str]]:
    controller_paths = []
    controller_home = Path.home()
    for key, value in _config_strings(defaults):
        path = Path(value)
        if path.is_absolute() and path.is_relative_to(controller_home):
            controller_paths.append((key, value))
    if not controller_paths:
        return []
    paths = sorted({value for _, value in controller_paths})
    script = (
        "import json,os,sys; "
        "print(json.dumps([path for path in sys.argv[1:] if not os.path.exists(path)]))"
    )
    proc = _host_run(name, table, ["python3", "-c", script, *paths])
    if proc.returncode:
        return []
    try:
        missing = set(json.loads(proc.stdout))
    except (TypeError, ValueError):
        return []
    return [
        row(
            "WARN",
            f"host {name} remote config {key}",
            f"controller path {value} does not exist on host",
        )
        for key, value in controller_paths
        if value in missing
    ]


def remote_config_rows(data: dict) -> list[dict[str, str]]:
    rows = []
    for name, table in sorted(data.get("hosts", {}).items()):
        defaults = host.remote_config(data, name).get("defaults", {})
        rows += _controller_path_rows(name, table, defaults)
        triage_url = defaults.get("triage", {}).get("url")
        if isinstance(triage_url, str) and _is_loopback_url(triage_url):
            rows.append(row(
                "WARN",
                f"host {name} remote config triage.url",
                f"loopback URL {triage_url} resolves on the remote host",
            ))
    return rows


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


PARITY_WORKER_CLIS = ("omp", "droid")


def _tool_version(data: dict, name: str, tool: str) -> str:
    table = host.hosts(data).get(name, {})
    argv = [tool, "--version"] if name == "local" else ["sh", "-c", host.REMOTE_TOOL, tool, "--version"]
    try:
        proc = _host_run(name, table, argv)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    lines = proc.stdout.strip().splitlines()
    return lines[0].strip() if proc.returncode == 0 and lines else "missing"


def _models(config: dict) -> str:
    """`label=model` for each configured worker and the reviewer, read from `--model` arguments."""
    found = []
    commands = [(f"workers.{label}", worker) for label, worker in sorted(config.get("workers", {}).items())]
    commands.append(("review", config.get("review", {})))
    for label, table in commands:
        if not isinstance(table, dict):
            continue
        argv = [str(arg) for arg in table.get("command", [])]
        model = table.get("model")
        for i, arg in enumerate(argv):
            if arg == "--model" and i + 1 < len(argv):
                model = argv[i + 1]
            elif arg.startswith("--model="):
                model = arg.split("=", 1)[1]
        found.append(f"{label}={model or 'default'}")
    return " ".join(found)


def _effective(data: dict, name: str, table: dict) -> dict:
    defaults = host.remote_config(data, name).get("defaults", {}) if name != "local" else {
        key: value for key, value in data.get("defaults", {}).items() if key != "engine"
    }
    config = copy.deepcopy(defaults)
    host._merge(config, {key: copy.deepcopy(value) for key, value in table.items()
                         if key not in host.HOST_KEYS})
    return config


def parity_rows(data: dict) -> list[dict[str, str]]:
    """Compare engine, worker CLIs and models across every host that runs one repo; drift is WARN."""
    return [item for rows in parity_by_slug(data).values() for item in rows]


def parity_by_slug(data: dict) -> dict[str, list[dict[str, str]]]:
    by_slug = {}
    for slug, table in host.repos(data).items():
        names = host.repo_hosts(table, slug)
        if len(names) < 2:
            continue
        rows = by_slug.setdefault(slug, [])
        checks: list[tuple[str, dict[str, str]]] = []
        engines = {}
        for name in names:
            version = _tool_version(data, name, "factory")
            installed = next(iter(re.findall(r"\b[0-9a-fA-F]{7,40}\b", version)), None)
            recorded = (host.engine(data, name) or {}).get("sha")
            # Only a sha shortens: two sha-less versions (`factory 0.4.6`, `factory 0.4.7`) compare whole.
            sha = installed or (str(recorded) if recorded else None)
            engines[name] = sha[:7] if sha else version
        checks.append(("engine", engines))
        for tool in PARITY_WORKER_CLIS:
            checks.append((f"worker {tool}", {name: _tool_version(data, name, tool) for name in names}))
        checks.append(("models", {name: _models(_effective(data, name, table)) for name in names}))
        drift = [(what, values) for what, values in checks if len(set(values.values())) > 1]
        for what, values in drift:
            rows.append(row("WARN", f"parity {slug} {what}", "; ".join(f"{n}: {v}" for n, v in values.items())))
        if not drift:
            rows.append(row("PASS", f"parity {slug}", f"{', '.join(names)}: same engine, worker CLIs and models"))
    return by_slug


def repo_rows(data: dict) -> list[dict[str, str]]:
    rows = []
    for slug, table in host.repos(data).items():
        if host.is_remote(data, slug):
            continue
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


def port_rows(data: dict, backend) -> list[dict[str, str]]:
    rows = []
    by_port: dict[int, list[str]] = {}
    for slug, table in host.repos(data).items():
        if host.is_remote(data, slug):
            continue
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
        sockets = [line for line in proc.stdout.splitlines() if line.lstrip().startswith("LISTEN ")]
        if not sockets:
            rows.append(row("PASS", f"dashboard port {port}", "free"))
            continue
        main_pid = str(backend.status(unit).main_pid)
        for socket in sockets:
            listeners = re.findall(r'\("([^"\n]+)",pid=(\d+)', socket)
            if not listeners:
                rows.append(row("FAIL", f"dashboard port {port}", f"held by an unidentified process, not {unit}"))
                break
            foreign = next(((process, pid) for process, pid in listeners if pid != main_pid), None)
            if foreign:
                process, pid = foreign
                rows.append(row("FAIL", f"dashboard port {port}", f"held by {process} (pid {pid}), not {unit}"))
                break
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


def unit_rows(backend) -> list[dict[str, str]]:
    rows = []
    for unit in UNITS:
        loaded = backend.status(unit).load_state
        if loaded in ("", "not-found"):
            continue
        state = backend.is_active(unit)
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
    host.hosts(data)
    for slug in host.repos(data):
        host.factory_host(data, slug)
    backend = host.service_backend(run=run)

    rows += factory_rows(data)
    rows.append(gh_auth())
    rows += toolchain_rows(data)
    rows += remote_config_rows(data)
    rows += parity_rows(data)

    state = backend.manager_status()
    manager = "systemd user manager" if isinstance(backend, SystemdUserBackend) else "launchd user manager"
    rows.append(row("PASS" if state in ("running", "degraded") else "FAIL", manager, state))

    if isinstance(backend, SystemdUserBackend):
        linger = run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"])
        rows.append(row("PASS", "linger", "yes") if linger.returncode == 0 and linger.stdout.strip() == "yes" else row("WARN", "linger", "timers stop at logout: loginctl enable-linger"))

    rows += repo_rows(data)
    rows += port_rows(data, backend)

    clone_dir = Path(os.path.expanduser(host.default(data, "clone_dir")))
    rows.append(row("PASS", "clone_dir", str(clone_dir)) if clone_dir.is_dir() else row("WARN", "clone_dir", f"{clone_dir} is not a directory"))
    rows += policy_rows(data)
    rows += unit_rows(backend)
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
