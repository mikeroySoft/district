"""`district apply [slug] [--upgrade [REF]] [--reset slug]`: reconcile the host to the registry.

Idempotent. Rewrites host-side artifacts (units, policy env) without asking;
never modifies a committed file. Exit nonzero when any repo is not converged.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from district import host, install_state
from district.host import DistrictError, run

POLICY_ENV = ("NPM_CONFIG_MIN_RELEASE_AGE", "UV_EXCLUDE_NEWER")
# doctor rows about committed files: drift the repo owner decides on
DRIFT_LABELS = (".factory.toml keys", "host settings committed", ".github/ISSUE_TEMPLATE/agent_task.md", ".factory.toml committed")
SMOKE_TIMEOUT = 10  # seconds per `--version` probe under a unit's PATH
DRAIN_TIMEOUT = 120  # maximum seconds an install may keep timers drained
SERVICE_WAIT = 3600  # maximum seconds a hand-run `--reset` pass may take
SMOKE_BINARIES = ("gh", "factory")
DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(h|d|m|min|s)?$")
EXTRA_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
SHA = re.compile(r"^[0-9a-f]{40}$")
SEMVER_TAG = re.compile(
    r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")
UNIT_HEADER = re.compile(r"(?m)^# ([^\n]+)\n")


def factory_extras(data: dict) -> list[str]:
    extras = host.default(data, "factory_extras")
    if not isinstance(extras, list) or any(not isinstance(extra, str) or not EXTRA_NAME.fullmatch(extra)
                                           for extra in extras):
        raise DistrictError("[defaults].factory_extras must be an array of valid Python extra names")
    return extras


def _host_repos(data: dict, name: str = "local") -> dict[str, dict]:
    return {
        slug: table for slug, table in host.repos(data).items()
        if table.get("host", "local") == name
    }


def github_slug(url: str) -> str | None:
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            slug = url[len(prefix):].removesuffix(".git").strip("/")
            return slug if len(slug.split("/")) == 2 else None
    return None


def installed_engine_commit(data: dict, host_name: str = "local") -> str | None:
    sha = (host.engine(data, host_name) or {}).get("sha")
    return sha if isinstance(sha, str) and SHA.fullmatch(sha) else None


def latest_odd_tag(output: str) -> tuple[str, str] | None:
    """Return the highest odd-patch semver tag and its commit."""
    candidates = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2 or not SHA.fullmatch(parts[0].lower()):
            continue
        remote_ref = parts[1]
        if not remote_ref.startswith("refs/tags/"):
            continue
        peeled = remote_ref.endswith("^{}")
        tag = remote_ref.removeprefix("refs/tags/").removesuffix("^{}")
        match = SEMVER_TAG.fullmatch(tag)
        if match is None or int(match[3]) % 2 == 0:
            continue
        prerelease = match[4]
        identifiers = prerelease.split(".") if prerelease else []
        if any(value.isdigit() and len(value) > 1 and value.startswith("0") for value in identifiers):
            continue
        prerelease_key = tuple(
            (0, int(value)) if value.isdigit() else (1, value) for value in identifiers)
        key = (int(match[1]), int(match[2]), int(match[3]), prerelease is None, prerelease_key)
        previous = candidates.get(tag)
        if previous is None or peeled:
            candidates[tag] = (key, parts[0].lower(), peeled)
    if not candidates:
        return None
    tag = max(candidates, key=lambda name: candidates[name][0])
    return tag, candidates[tag][1]


def _defer(record: install_state.ApplyRun | None, target: str, busy: list[str]) -> None:
    reason = f"busy: {', '.join(busy)}"
    if record:
        record.defer(target, reason)
    print(f"{target} deferred ({reason})")


def reconcile_engine(data: dict, record: install_state.ApplyRun | None = None, *, backend=None,
                     host_name: str = "local", repos: dict[str, dict] | None = None,
                     run_cmd=None) -> str:
    """Install a changed, CI-green remote Factory ref without modifying its checkout."""
    run_cmd = run if run_cmd is None else run_cmd
    backend = backend or host.service_backend()
    repos = _host_repos(data, host_name) if repos is None else repos
    engine_target = "engine" if host_name == "local" else f"engine:{host_name}"
    ref = data.get("defaults", {}).get("factory_ref")
    if ref is None:
        if record:
            record.clear_deferred(engine_target)
        return "unchanged"
    if not isinstance(ref, str) or not ref.strip():
        raise DistrictError("[defaults].factory_ref must be a non-empty string")
    extras = factory_extras(data)
    source = host.factory_source(data, host_name)
    source_cmd = run_cmd
    if source is None:
        source = str(host.default(data, "factory_source"))
        source_cmd = run
    if isinstance(source_cmd, host.RemoteRunner) and source.startswith("~/"):
        source = str(Path(source_cmd.facts()["home"]) / source[2:])
    src = Path(os.path.expanduser(source)) if not isinstance(source_cmd, host.RemoteRunner) else Path(source)
    try:
        if isinstance(source_cmd, host.RemoteRunner):
            if source_cmd(["test", "-d", str(src)]).returncode:
                raise OSError(f"{src} does not exist")
        elif not src.is_dir():
            raise OSError(f"{src} does not exist")
        origin_proc = source_cmd(["git", "remote", "get-url", "origin"], cwd=src)
        if origin_proc.returncode:
            raise OSError(origin_proc.stderr.strip() or "could not read origin")
        origin = origin_proc.stdout.strip()
        slug = github_slug(origin)
        if slug is None:
            raise OSError(f"{src} origin is not a GitHub repository")
        if ref == "latest-odd":
            resolved = source_cmd(["git", "ls-remote", "--tags", origin], cwd=src)
            if resolved.returncode:
                raise OSError(resolved.stderr.strip() or "ls-remote failed")
            target = latest_odd_tag(resolved.stdout)
            if target is None:
                raise OSError(f"no odd-patch tag on {origin}")
            resolved_ref, sha = target
        else:
            resolved = source_cmd([
                "git", "ls-remote", "--exit-code", origin,
                f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}",
            ], cwd=src)
            if resolved.returncode:
                raise OSError(resolved.stderr.strip() or "ls-remote failed")
            remote_refs = {
                parts[1]: parts[0].lower()
                for line in resolved.stdout.splitlines()
                if len(parts := line.split()) == 2 and SHA.fullmatch(parts[0].lower())
            }
            for candidate in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}"):
                if candidate in remote_refs:
                    resolved_ref, sha = ref, remote_refs[candidate]
                    break
            else:
                raise OSError("remote ref did not resolve to one commit")
    except OSError as exc:
        if host_name != "local":
            print(f"engine:{host_name} FAIL factory source {src}: {exc}")
            return "failed"
        print(f"engine {ref} unresolved: {exc}; keeping installed engine")
        return "unchanged"
    if installed_engine_commit(data, host_name) == sha:
        if record:
            record.clear_deferred(engine_target)
        print(f"engine {sha[:7]} ({resolved_ref})")
        return "unchanged"
    checks = source_cmd(["gh", "api", "--paginate", "--slurp",
                         f"repos/{slug}/commits/{sha}/check-runs?per_page=100"])
    if checks.returncode:
        print(f"engine {sha[:12]} CI unavailable; keeping installed engine")
        return "unchanged"
    try:
        payload = json.loads(checks.stdout)
        pages = payload if isinstance(payload, list) else [payload]
        runs = [check for page in pages for check in page["check_runs"]]
        total = pages[0]["total_count"]
    except (ValueError, KeyError, TypeError, IndexError):
        runs, total = [], None
    valid_runs = [check for check in runs if isinstance(check, dict)]
    if not valid_runs or len(valid_runs) != len(runs) or total != len(runs):
        state = "missing"
    elif any(check.get("status") != "completed" for check in valid_runs):
        state = "pending"
    elif any(check.get("conclusion") not in {"success", "skipped", "neutral"} for check in valid_runs):
        state = "failed"
    else:
        state = "success"
    if state != "success":
        print(f"engine {sha[:12]} CI {state}; keeping installed engine")
        return "unchanged"

    services = [f"{host.unit_name(slug)}.service" for slug in repos]
    if busy := busy_units(services, backend):
        _defer(record, engine_target, busy)
        return "deferred"
    timers = [f"{host.unit_name(slug)}.timer" for slug in repos]
    active_timers = [timer for timer in timers if backend.is_active(timer) == "active"]
    install_url = origin
    if origin.startswith("git@"):
        install_url = "ssh://" + origin.replace(":", "/", 1)
    install_source = f"git+{install_url}@{sha}"
    if extras:
        install_source += f"[{','.join(extras)}]"
    try:
        if busy := drain(active_timers, services, data, record, backend=backend):
            _defer(record, engine_target, busy)
            return "deferred"
        try:
            proc = run_cmd(
                ["uv", "tool", "install", "--reinstall", "--from", install_source, "factory"],
                timeout=DRAIN_TIMEOUT if active_timers else None,
            )
        except subprocess.TimeoutExpired:
            print(f"engine {sha[:12]} install timed out after {DRAIN_TIMEOUT}s")
            return "failed"
    finally:
        restore(active_timers, repos, record, backend=backend)
    if proc.returncode:
        detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        print(f"engine {sha[:12]} install failed" + (f": {detail[-1]}" if detail else ""))
        return "failed"
    previous = (host.engine(data, host_name) or {}).get("sha")
    host.set_engine(data, host_name, {
        "ref": resolved_ref, "sha": sha, **({"previous": previous} if previous else {}), "installed_at": iso(now()),
    })
    host.save(data)
    if isinstance(run_cmd, host.RemoteRunner):
        host.sync_remote_config(data, host_name, run_cmd)
    restart_dashboards(repos, backend)
    if record:
        record.clear_deferred(engine_target)
    print(f"installed factory engine {sha[:12]} ({resolved_ref})")
    return "applied"




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


def uv_exclude_newer(age: str) -> str:
    """Relative uv duration as ISO 8601 (no spaces, so an unquoted systemd `Environment=` line, which splits
    on whitespace, keeps it whole). uv rejects fractional days; seconds round up so the cutoff never shrinks."""
    s = math.ceil(round(hours(age) * 3600, 6))
    return f"PT{s // 3600}H" if s % 3600 == 0 else f"PT{s}S"


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


def _rendered_units(output: str) -> dict[str, str]:
    matches = list(UNIT_HEADER.finditer(output))
    units = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else None
        units[match.group(1)] = output[match.end():end].removesuffix("\n")
    return units


def _units_changed(slug: str, output: str, backend) -> bool:
    wanted = _rendered_units(output)
    unit = host.unit_name(slug)
    if not wanted or any(not name.startswith(unit) for name in wanted):
        return True
    return backend.definitions_changed(unit, wanted)


def plan_apply(targets: dict[str, dict], backend, *, run_cmd=None, env=None) -> dict[str, bool]:
    run_cmd = run if run_cmd is None else run_cmd
    backend = backend or host.service_backend()
    changes = {}
    for slug, table in targets.items():
        if table.get("disabled_at"):
            changes[slug] = False
            continue
        kwargs = {"env": env} if env is not None else {}
        proc = run_cmd(["factory", "install", "--print"], cwd=Path(table["path"]), **kwargs)
        changes[slug] = proc.returncode != 0 or _units_changed(slug, proc.stdout, backend)
    return changes


def busy_units(services: list[str], backend=None) -> list[str]:
    backend = backend or host.service_backend()
    return [service for service in services
            if backend.is_active(service) in ("active", "activating", "deactivating")]


def disable_timers(timers: list[str], timeout: float, backend, *, check: bool = True,
                   record: install_state.ApplyRun | None = None) -> None:
    deadline = time.monotonic() + timeout
    for timer in timers:
        if record:
            record.disabled.add(timer)
        try:
            backend.enable(
                timer,
                False,
                check=check,
                timeout=max(0, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired as exc:
            service = f"{timer.removesuffix('.timer')}.service"
            raise DistrictError(f"still running after {int(timeout)}s: {service}") from exc


def drain(timers: list[str], services: list[str], data: dict,
          record: install_state.ApplyRun | None = None, *, backend=None) -> list[str]:
    backend = backend or host.service_backend()
    if record:
        record.observe(data)
    disable_timers(timers, DRAIN_TIMEOUT, backend, record=record)
    return busy_units(services, backend)


def restart_dashboards(repos: dict[str, dict], backend=None) -> None:
    backend = backend or host.service_backend()
    for slug in repos:
        dash = f"{host.unit_name(slug)}-dashboard.service"
        if backend.status(dash).loaded:
            backend.start(dash, restart=True, check=False)


def upgrade(data: dict, ref: str = "HEAD", record: install_state.ApplyRun | None = None, *,
            backend=None, host_name: str = "local", repos: dict[str, dict] | None = None,
            run_cmd=None) -> str:
    """Reinstall an export of `ref` only while every Factory service on the target host is idle."""
    run_cmd = run if run_cmd is None else run_cmd
    backend = backend or host.service_backend()
    repos = _host_repos(data, host_name) if repos is None else repos
    engine_target = "engine" if host_name == "local" else f"engine:{host_name}"
    extras = factory_extras(data)
    source_path = host.factory_source(data, host_name)
    source_cmd = run_cmd
    if source_path is None:
        source_path = str(host.default(data, "factory_source"))
        source_cmd = run
    if isinstance(source_cmd, host.RemoteRunner) and source_path.startswith("~/"):
        source_path = str(Path(source_cmd.facts()["home"]) / source_path[2:])
    src = (
        Path(os.path.expanduser(source_path))
        if not isinstance(source_cmd, host.RemoteRunner)
        else Path(source_path)
    )
    proc = source_cmd(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=src)
    if proc.returncode != 0:
        raise DistrictError(f"{ref}: not a commit in {src}")
    sha = proc.stdout.strip()
    if ref == "HEAD" and source_cmd(
        ["git", "status", "--porcelain"], cwd=src, check=True
    ).stdout.strip():
        raise DistrictError(f"{src} has uncommitted changes; the installed snapshot must match a commit")
    services = [f"{host.unit_name(slug)}.service" for slug in repos]
    if busy := busy_units(services, backend):
        _defer(record, engine_target, busy)
        return "deferred"
    timers = [f"{host.unit_name(slug)}.timer" for slug in repos]
    temporary = (
        run_cmd.temporary_directory()
        if isinstance(run_cmd, host.RemoteRunner)
        else tempfile.TemporaryDirectory(prefix="district-engine-")
    )
    with temporary as tmp:
        base = tmp if isinstance(tmp, host.RemotePath) else Path(tmp)
        tar, tree = base / "engine.tar", base / sha
        tree.mkdir()
        if source_cmd is run_cmd:
            source_cmd(["git", "archive", "-o", str(tar), sha], cwd=src, check=True)
        else:
            with tempfile.TemporaryDirectory(prefix="district-engine-export-") as export_tmp:
                controller_tar = Path(export_tmp) / "engine.tar"
                source_cmd(
                    ["git", "archive", "-o", str(controller_tar), sha], cwd=src, check=True
                )
                tar.write_bytes(controller_tar.read_bytes())
        run_cmd(["tar", "-x", "-C", str(tree), "-f", str(tar)], check=True)
        source = f"{tree}[{','.join(extras)}]" if extras else str(tree)
        active_timers = [timer for timer in timers if backend.is_active(timer) == "active"]
        try:
            if busy := drain(active_timers, services, data, record, backend=backend):
                _defer(record, engine_target, busy)
                return "deferred"
            try:
                proc = run_cmd(
                    ["uv", "tool", "install", "--reinstall", "--from", source, "factory"],
                    timeout=DRAIN_TIMEOUT if active_timers else None,
                )
            except subprocess.TimeoutExpired as exc:
                raise DistrictError(f"uv tool install timed out after {DRAIN_TIMEOUT}s") from exc
        finally:
            restore(active_timers, repos, record, backend=backend)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise DistrictError("uv tool install failed")
    version = run_cmd(["factory", "--version"], check=True).stdout.strip()
    print(f"installed factory {version} from {src}@{sha[:7]} ({ref})")
    previous = (host.engine(data, host_name) or {}).get("sha")
    engine = {
        "ref": ref, "sha": sha, **({"previous": previous} if previous else {}), "installed_at": iso(now()),
    }
    host.set_engine(data, host_name, engine)
    host.save(data)
    if isinstance(run_cmd, host.RemoteRunner):
        host.sync_remote_config(data, host_name, run_cmd)
    if previous:
        print(f"rollback: district apply --upgrade {previous}")
    restart_dashboards(repos, backend)
    if record:
        record.clear_deferred(engine_target)
    return "applied"


def restore(timers: list[str], repos: dict[str, dict],
            record: install_state.ApplyRun | None = None, *, backend=None) -> None:
    backend = backend or host.service_backend()
    disabled = {f"{host.unit_name(s)}.timer" for s, table in repos.items() if table.get("disabled_at")}
    for timer in timers:
        if timer not in disabled and backend.is_active(timer) != "active":
            backend.enable(timer, check=False)
            if record:
                record.reenabled.add(timer)


# ---------------------------------------------------------------- per repo


def reset(slug: str, table: dict, data: dict, record: install_state.ApplyRun | None = None, *,
          backend=None) -> bool:
    """Run one pass by hand; clear the cap only if it succeeded."""
    backend = backend or host.service_backend()
    if not table.get("disabled_at"):
        print(f"{slug}: not disabled by District; nothing to reset")
        return True
    service = f"{host.unit_name(slug)}.service"
    print(f"{slug}: running one pass ({service}) before re-enabling the timer")
    try:
        proc = backend.start(service, check=False, timeout=SERVICE_WAIT)
    except subprocess.TimeoutExpired:
        print(f"{slug}: pass timed out after {SERVICE_WAIT}s; timer stays disabled")
        return False
    if proc.returncode != 0:
        print(f"{slug}: pass failed ({proc.stderr.strip() or proc.returncode}); timer stays disabled")
        return False
    table.pop("disabled_at", None)
    table.pop("disabled_reason", None)
    host.save(data)
    backend.enable(f"{host.unit_name(slug)}.timer")
    if record:
        record.reenabled.add(f"{host.unit_name(slug)}.timer")
    print(f"{slug}: pass succeeded; cap cleared, timer re-enabled")
    return True


def repo_pass(slug: str, table: dict, data: dict,
              record: install_state.ApplyRun | None = None, *, backend=None, run_cmd=None) -> dict:
    run_cmd = run if run_cmd is None else run_cmd
    root = Path(table["path"])
    backend = backend or host.service_backend()
    row = {
        "slug": slug, "version": None, "doctor": "?", "drift": [], "failures": None,
        "fail": [], "disabled": table.get("disabled_at"),
    }
    unit = host.unit_name(slug)

    def step(*cmd: str) -> str | None:
        proc = run_cmd(list(cmd), cwd=root)
        if proc.returncode != 0:
            tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:] or ["?"]
            row["fail"].append(f"{' '.join(cmd[1:])}: {tail[0]}")
            return None
        return proc.stdout

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
    if row["failures"] is not None and row["failures"] >= cap and backend.is_active(f"{unit}.timer") == "active":
        disable_timers([f"{unit}.timer"], DRAIN_TIMEOUT, backend, check=False, record=record)
        table["disabled_at"] = iso(now())
        table["disabled_reason"] = f"{row['failures']} consecutive failed passes (cap {cap})"
        host.save(data)
        row["disabled"] = table["disabled_at"]
        print(f"{slug}: {table['disabled_reason']}; timer disabled. Fix the cause, then `district apply --reset {slug}`")
    row["timer"] = backend.is_active(f"{unit}.timer")
    return row


def deferred_row(slug: str, table: dict, reason: str, backend=None) -> dict:
    backend = backend or host.service_backend()
    return {
        "slug": slug, "version": None, "doctor": "?", "drift": [], "failures": None,
        "fail": [], "disabled": table.get("disabled_at"), "timer": backend.is_active(f"{host.unit_name(slug)}.timer"),
        "deferred": reason,
    }


def line(row: dict, status: str) -> str:
    if reason := row.get("deferred"):
        return f"{row['slug']} deferred ({reason})"
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


def smoke(slugs, backend=None, *, run_cmd=None) -> tuple[int, list[str]]:
    """`<binary> --version` under each installed dispatch/dashboard unit's baked PATH; returns (units, failures)."""
    run_cmd = run if run_cmd is None else run_cmd
    backend = backend or host.service_backend()
    units, fails = 0, []
    for slug in slugs:
        for name in (f"{host.unit_name(slug)}.service", f"{host.unit_name(slug)}-dashboard.service"):
            status = backend.status(name)
            if not status.loaded:
                continue
            units += 1
            path = status.environment.get("PATH")
            if path is None:
                fails.append(f"{name}: no Environment=PATH")
                continue
            for binary in SMOKE_BINARIES:
                exe = (
                    run_cmd.which(binary, path)
                    if isinstance(run_cmd, host.RemoteRunner)
                    else shutil.which(binary, path=path)
                )
                if exe is None:
                    fails.append(f"{name}: {binary} not on unit PATH")
                    continue
                try:
                    env = {"PATH": path} if isinstance(run_cmd, host.RemoteRunner) else {**os.environ, "PATH": path}
                    proc = run_cmd([exe, "--version"], timeout=SMOKE_TIMEOUT, env=env)
                except subprocess.TimeoutExpired:
                    fails.append(f"{name}: {binary} --version timed out after {SMOKE_TIMEOUT}s")
                    continue
                except OSError as exc:  # passes shutil.which but won't start: dead shebang interpreter, ENOEXEC, EACCES
                    fails.append(f"{name}: {binary} --version: {exc}")
                    continue
                if proc.returncode != 0:
                    tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:] or ["?"]
                    fails.append(f"{name}: {binary} --version exited {proc.returncode}: {tail[0]}")
    return units, fails


def _apply_host(data: dict, host_name: str, targets: dict[str, dict], args: argparse.Namespace,
                record: install_state.ApplyRun, status: str) -> tuple[list[dict], int, tuple[int, list[str]]]:
    all_repos = _host_repos(data, host_name)
    if host_name == "local":
        run_cmd = run
        backend = host.service_backend()
        install_env = None
    else:
        target = data["hosts"][host_name]
        run_cmd = host.remote_runner(host_name, target)
        backend = host.service_backend(target, run=run_cmd)
        host.sync_remote_config(data, host_name, run_cmd)
        facts = run_cmd.facts()
        install_env = {"PATH": host.service_path(facts["path"], home=Path(facts["home"]))}

    rows = []
    completed = {}
    code = 0
    engine_result = (
        upgrade(
            data, args.upgrade, record, backend=backend, host_name=host_name,
            repos=all_repos, run_cmd=run_cmd,
        )
        if args.upgrade
        else reconcile_engine(
            data, record, backend=backend, host_name=host_name,
            repos=all_repos, run_cmd=run_cmd,
        )
    )
    if engine_result == "failed":
        code = 1

    record.observe(data)
    for slug, table in targets.items():
        if args.reset and not reset(slug, table, data, record, backend=backend):
            code = 1
    changes = plan_apply(targets, run_cmd=run_cmd, backend=backend, env=install_env)
    for slug, table in targets.items():
        if table.get("disabled_at"):
            row = repo_pass(slug, table, data, record, backend=backend, run_cmd=run_cmd)
        else:
            service = f"{host.unit_name(slug)}.service"
            changed = changes[slug]
            if changed and (busy := busy_units([service], backend)):
                reason = f"busy: {', '.join(busy)}"
                record.defer(slug, reason)
                rows.append(deferred_row(slug, table, reason, backend))
                continue
            timer = f"{host.unit_name(slug)}.timer"
            active_timers = [timer] if changed and backend.is_active(timer) == "active" else []
            install_error = None
            try:
                if changed and (busy := drain(active_timers, [service], data, record, backend=backend)):
                    reason = f"busy: {', '.join(busy)}"
                    record.defer(slug, reason)
                    rows.append(deferred_row(slug, table, reason, backend))
                    continue
                try:
                    proc = run_cmd(
                        ["factory", "install"],
                        cwd=Path(table["path"]),
                        timeout=DRAIN_TIMEOUT if active_timers else None,
                        **({"env": install_env} if install_env is not None else {}),
                    )
                except subprocess.TimeoutExpired:
                    install_error = f"install: timed out after {DRAIN_TIMEOUT}s"
                else:
                    if proc.returncode:
                        tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:] or ["?"]
                        install_error = f"install: {tail[0]}"
            finally:
                restore(active_timers, all_repos, record, backend=backend)
            row = repo_pass(slug, table, data, record, backend=backend, run_cmd=run_cmd)
            if install_error:
                row["fail"].insert(0, install_error)
            else:
                record.clear_deferred(slug)
        rows.append(row)
        completed[slug] = table

    if isinstance(run_cmd, host.RemoteRunner):
        host.sync_remote_config(data, host_name, run_cmd)
    current_engine = host.engine(data, host_name)
    if current_engine and not args.upgrade and "factory_ref" not in data.get("defaults", {}):
        print(f"engine {current_engine['sha'][:7]} ({current_engine['ref']})")
    for row in rows:
        print(line(row, status))
    converged = [row for row in rows if "deferred" not in row]
    if any(row["fail"] or row["disabled"] or row["timer"] != "active" for row in converged):
        code = 1
    smoke_result = smoke(completed, backend, run_cmd=run_cmd)
    if smoke_result[1]:
        code = 1
    return rows, code, smoke_result


def _run(args: argparse.Namespace, record: install_state.ApplyRun) -> int:
    # Factory units bake the installing process PATH; keep wrappers after real installs.
    os.environ["PATH"] = host.service_path()
    lock_path = host.operation_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"district apply: another apply is running ({lock_path})", file=sys.stderr)
            return 1

        data = host.load()
        host.hosts(data)
        registered = host.repos(data)
        for slug in registered:
            host.factory_host(data, slug)
        selected = host.select(data, args.reset or args.slug)
        if not selected:
            print("no repositories registered (district add)")
            return 0
        status, warnings = policy(data)
        host.save(data)
        groups: dict[str, dict[str, dict]] = {}
        for slug, table in selected.items():
            groups.setdefault(table.get("host", "local"), {})[slug] = table

        rows = []
        smokes = []
        code = 0
        for host_name, targets in groups.items():
            try:
                host_rows, host_code, host_smoke = _apply_host(
                    data, host_name, targets, args, record, status,
                )
            except host.RemoteHostError as exc:
                for slug in targets:
                    print(f"{slug}: {exc}")
                code = 1
                continue
            except DistrictError as exc:
                if host_name == "local":
                    raise
                for slug in targets:
                    print(f"{slug}: host {host_name} operation failed: {exc}")
                code = 1
                continue
            rows.extend(host_rows)
            code = max(code, host_code)
            smokes.append(host_smoke)
        for warning in warnings:
            print(warning)
        for units, smoke_fails in smokes:
            for fail in smoke_fails:
                print(f"smoke FAIL {fail}")
            if not smoke_fails:
                print(f"smoke OK: {', '.join(SMOKE_BINARIES)} --version under {units} unit(s)")
        versions = {row["version"] for row in rows if "deferred" not in row}
        if len(versions) > 1:
            print(f"versions differ across the fleet: {', '.join(str(v) for v in sorted(versions, key=str))}")
            code = 1
        return code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district apply", description=__doc__.split("\n", 1)[0])
    parser.add_argument("slug", nargs="?", help="one repo (owner/name or basename); default all")
    parser.add_argument("--upgrade", nargs="?", const="HEAD", metavar="REF",
                        help="reinstall factory from [defaults].factory_source first, at REF (tag, branch, sha; default HEAD)")
    parser.add_argument("--reset", metavar="SLUG", help="run one pass by hand and re-enable the timer if it succeeds")
    args = parser.parse_args(argv)
    record = install_state.ApplyRun.start()
    code = 1
    try:
        code = _run(args, record)
        return code
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        raise
    finally:
        record.finish(code)
