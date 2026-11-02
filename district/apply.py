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
import plistlib
import shlex
import shutil
import subprocess
import sys
import tomllib
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from district import host, install_state
from district.engine_cleanup import cleanup
from district.host import DistrictError, run
from district.service import remaining_timeout

POLICY_ENV = ("NPM_CONFIG_MIN_RELEASE_AGE", "UV_EXCLUDE_NEWER")
# doctor rows about committed files: drift the repo owner decides on
DRIFT_LABELS = (".factory.toml keys", "host settings committed", ".github/ISSUE_TEMPLATE/agent_task.md", ".factory.toml committed")
SMOKE_TIMEOUT = 10  # seconds per `--version` probe under a unit's PATH
DRAIN_TIMEOUT = 120  # maximum seconds an install may keep timers drained
RESTORE_TIMEOUT = 30  # separate budget after the idle deadline expires
SERVICE_WAIT = 3600  # maximum seconds a hand-run `--reset` pass may take
ENGINE_HISTORY = 50
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
        if name in host.repo_hosts(table, slug)
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


def _ci_state(source_cmd, slug: str, sha: str) -> str:
    """unavailable, missing, pending, failed or success for `sha`'s GitHub check runs."""
    checks = source_cmd(["gh", "api", "--paginate", "--slurp",
                         f"repos/{slug}/commits/{sha}/check-runs?per_page=100"], timeout=SMOKE_TIMEOUT)
    if checks.returncode:
        return "unavailable"
    try:
        payload = json.loads(checks.stdout)
        pages = payload if isinstance(payload, list) else [payload]
        runs = [check for page in pages for check in page["check_runs"]]
        total = pages[0]["total_count"]
    except (ValueError, KeyError, TypeError, IndexError):
        runs, total = [], None
    valid_runs = [check for check in runs if isinstance(check, dict)]
    if not valid_runs or len(valid_runs) != len(runs) or total != len(runs):
        return "missing"
    if any(check.get("status") != "completed" for check in valid_runs):
        return "pending"
    if any(check.get("conclusion") not in {"success", "skipped", "neutral"} for check in valid_runs):
        return "failed"
    return "success"


def _resolve_engine(ref: str, run_cmd, origin: str, pin: tuple[str, str] | None = None,
                    *, verify_ci: bool = True) -> tuple[str, str, str, str]:
    """Resolve only remote refs; branches follow their newest green commit, tags do not walk."""
    slug = github_slug(origin) if isinstance(origin, str) else None
    if slug is None:
        raise OSError(f"{origin}: factory_repo must name a GitHub repository")
    branch = False
    if pin is not None:
        resolved_ref, sha = pin
    elif SHA.fullmatch(ref):
        proc = run_cmd(["gh", "api", f"repos/{slug}/commits/{ref}"], timeout=SMOKE_TIMEOUT)
        payload = json.loads(proc.stdout) if proc.returncode == 0 else None
        if not isinstance(payload, dict) or payload.get("sha") != ref:
            raise OSError(f"{ref}: remote commit unavailable")
        resolved_ref, sha = ref, ref
    elif ref == "latest-odd":
        proc = run_cmd(["git", "ls-remote", "--tags", origin], timeout=SMOKE_TIMEOUT)
        if proc.returncode:
            raise OSError(proc.stderr.strip() or "ls-remote failed")
        target = latest_odd_tag(proc.stdout)
        if target is None:
            raise OSError(f"no odd-patch tag on {origin}")
        resolved_ref, sha = target
    else:
        proc = run_cmd(["git", "ls-remote", "--exit-code", origin,
                        f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}"],
                       timeout=SMOKE_TIMEOUT)
        if proc.returncode:
            raise OSError(proc.stderr.strip() or "ls-remote failed")
        refs = {parts[1]: parts[0].lower() for line in proc.stdout.splitlines()
                if len(parts := line.split()) == 2 and SHA.fullmatch(parts[0].lower())}
        for candidate in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}"):
            if candidate in refs:
                resolved_ref, sha = ref, refs[candidate]
                branch = candidate.startswith("refs/heads/")
                break
        else:
            raise OSError("remote ref did not resolve to one commit")
    if verify_ci:
        state = _ci_state(run_cmd, slug, sha)
        if state != "success" and branch:
            history = run_cmd(["gh", "api", f"repos/{slug}/commits?sha={sha}&per_page={ENGINE_HISTORY}"],
                              timeout=SMOKE_TIMEOUT)
            if history.returncode == 0:
                commits = json.loads(history.stdout)
                if isinstance(commits, list):
                    for commit in commits[:ENGINE_HISTORY]:
                        candidate = commit.get("sha") if isinstance(commit, dict) else None
                        if candidate != sha and isinstance(candidate, str) and SHA.fullmatch(candidate):
                            if _ci_state(run_cmd, slug, candidate) == "success":
                                return origin, slug, resolved_ref, candidate
        if state != "success":
            raise OSError(f"{sha[:12]} CI {state}; no eligible green commit")
    return origin, slug, resolved_ref, sha


def _receipt_sha(tool: Path, run_cmd, *, timeout: float = SMOKE_TIMEOUT) -> str | None:
    receipt = tool / "uv-receipt.toml"
    try:
        body = (run_cmd.path(str(receipt)).read_text(timeout=timeout) if isinstance(run_cmd, host.RemoteRunner)
                else receipt.read_text())
        requirements = tomllib.loads(body)["tool"]["requirements"]
        for requirement in requirements:
            if requirement.get("name") == "factory":
                revision = requirement.get("rev")
                if not revision:
                    match = re.search(r"[?&]rev=([0-9a-f]{40})(?:&|$)", requirement.get("git", ""))
                    revision = match[1] if match else None
                return revision if isinstance(revision, str) and SHA.fullmatch(revision) else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _tool_cli(tool: Path, cli: Path, run_cmd, *, timeout: float = SMOKE_TIMEOUT) -> bool:
    if isinstance(run_cmd, host.RemoteRunner):
        script = (
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "raise SystemExit(not (p.exists() and p.resolve().is_relative_to(pathlib.Path(sys.argv[2]).resolve())))"
        )
        return run_cmd(["python3", "-c", script, str(cli), str(tool)], timeout=timeout).returncode == 0
    return cli.exists() and cli.resolve().is_relative_to(tool.resolve())


def _verify_engine(sha: str | None, tool: Path, cli: Path, run_cmd, *, deadline: float | None = None) -> None:
    if deadline is None:
        deadline = time.monotonic() + SMOKE_TIMEOUT
    actual = _receipt_sha(tool, run_cmd, timeout=remaining_timeout(deadline))
    if actual is None or actual != sha:
        raise OSError(f"receipt rev {actual or 'unknown'} != {sha}")
    if not _tool_cli(tool, cli, run_cmd, timeout=remaining_timeout(deadline)):
        raise OSError(f"{cli} does not resolve into {tool}")
    proc = run_cmd([str(cli), "--version"], timeout=remaining_timeout(deadline))
    if proc.returncode:
        raise OSError(f"{cli} --version exited {proc.returncode}")


def _engine_current(sha: str, tool: Path, cli: Path, runner) -> bool:
    try:
        _verify_engine(sha, tool, cli, runner)
    except OSError:
        return False
    return True


def _save_engine(data: dict, name: str, runner, *, deadline: float | None = None, **values) -> None:
    previous = host.engine(data, name) or {}
    engine = dict(previous)
    engine.update(values)
    host.set_engine(data, name, engine)
    host.save(data)
    if isinstance(runner, host.RemoteRunner):
        try:
            host.sync_remote_config(data, name, runner, deadline=deadline)
        except (subprocess.TimeoutExpired, host.RemoteHostError, DistrictError):
            # Neither a newly recorded intent nor an unfinished prior one is lost.
            for key in ("paused_timers", "dashboards_pending", "install_pending"):
                if previous.get(key) and not engine.get(key):
                    engine[key] = previous[key]
            host.save(data)
            raise


def _downgrade_guard(slug: str, previous: str | None, target: str, run_cmd,
                     allow_downgrade: bool) -> None:
    if not previous or previous == target or allow_downgrade:
        return
    proc = run_cmd(["gh", "api", f"repos/{slug}/compare/{previous}...{target}"], timeout=SMOKE_TIMEOUT)
    try:
        status = json.loads(proc.stdout)["status"] if proc.returncode == 0 else None
    except (ValueError, KeyError, TypeError):
        status = None
    if status == "behind":
        raise DistrictError(f"refusing downgrade {previous} -> {target}; use --allow-downgrade")
    if status not in ("ahead", "identical", "diverged"):
        raise DistrictError(f"cannot establish ancestry {previous} -> {target}; compare unavailable")


def _engine_units(repos: dict, backend, *, timeout: float) -> tuple[list[str], list[str]]:
    units = set(backend.factory_units(timeout=timeout))
    units.update(f"{host.unit_name(slug)}{suffix}" for slug in repos for suffix in (".service", ".timer"))
    services = sorted(name for name in units if name.endswith(".service") and not name.endswith("-dashboard.service"))
    timers = sorted(name for name in units if name.endswith(".timer"))
    return services, timers


def _resolved_paths(paths: list[Path], runner, *, timeout: float = SMOKE_TIMEOUT) -> list[Path]:
    """Resolve identities on the host that owns the paths, never the controller."""
    if not isinstance(runner, host.RemoteRunner):
        return [path.resolve() for path in paths]
    script = (
        "import json,pathlib,sys; "
        "print(json.dumps([str(pathlib.Path(p).resolve()) for p in sys.argv[1:]]))"
    )
    proc = runner(["python3", "-c", script, *map(str, paths)], timeout=timeout)
    if proc.returncode:
        raise OSError(f"cannot resolve Factory paths: {proc.stderr.strip() or proc.returncode}")
    try:
        resolved = json.loads(proc.stdout)
        if not isinstance(resolved, list) or len(resolved) != len(paths):
            raise ValueError("unexpected path count")
        if not all(isinstance(path, str) and Path(path).is_absolute() for path in resolved):
            raise ValueError("expected absolute paths")
        return [Path(path) for path in resolved]
    except (TypeError, ValueError) as exc:
        raise OSError(f"cannot resolve Factory paths: {exc}") from exc


def _linux_identities(runner, *, deadline: float, pids: list[str] | None = None) -> dict:
    """Bracket argv/ownership observations with Linux process generations."""
    script = """
import json,pathlib,sys
root = pathlib.Path('/proc/')
rows = {}
for pid in sys.argv[1:] or [p.name for p in root.iterdir() if p.name.isdigit()]:
    try:
        stat = (root / pid / 'stat').read_text().rsplit(')', 1)[1].split()
        value = (root / pid / 'cgroup').read_text() if sys.argv[1:] else stat[1]
        after = (root / pid / 'stat').read_text().rsplit(')', 1)[1].split()
        if stat[19] == after[19]:
            rows[pid] = [stat[19], value]
    except (OSError, IndexError):
        pass
print(json.dumps(rows))
"""
    proc = runner(["python3", "-c", script, *(pids or [])], timeout=remaining_timeout(deadline))
    if proc.returncode:
        raise OSError(proc.stderr.strip() or "cannot inspect Factory process identity")
    return json.loads(proc.stdout)


def _ps_identities(output: str) -> tuple[dict, list[tuple[str, str]]]:
    """Parse one macOS ps observation: PID, PPID, five lstart fields, argv."""
    identities, commands = {}, []
    for line in output.splitlines():
        fields = line.split(maxsplit=7)
        if len(fields) >= 7 and fields[0].isdigit() and fields[1].isdigit():
            identities[fields[0]] = [" ".join(fields[2:7]), fields[1]]
            if len(fields) == 8:
                commands.append((fields[0], fields[7]))
    return identities, commands


def _factory_processes(tool: Path, cli: Path, run_cmd, *, platform: str, deadline: float) -> tuple[list[str], dict]:
    real_tool, real_cli = _resolved_paths([tool, cli], run_cmd, timeout=remaining_timeout(deadline))
    tool_bins = {tool / "bin", real_tool / "bin"}
    launchers = {str(cli), str(real_cli), *(str(directory / "factory") for directory in tool_bins)}
    identities = _linux_identities(run_cmd, deadline=deadline) if platform == "linux" else {}
    columns = "pid=,ppid=,lstart=,args=" if platform == "macos" else "pid=,args="
    proc = run_cmd(["ps", "-eo", columns], timeout=remaining_timeout(deadline))
    if proc.returncode:
        raise OSError(proc.stderr.strip() or "cannot inspect Factory processes")
    if platform == "macos":
        identities, commands = _ps_identities(proc.stdout)
    else:
        commands = [fields for line in proc.stdout.splitlines()
                    if len(fields := line.split(maxsplit=1)) == 2 and fields[0].isdigit()]
    busy = [f"pid {pid} {work}" for pid, command in commands
            if (work := _factory_work(command, tool_bins, launchers))]
    return busy, identities


def _managed_processes(processes: list[str], identities: dict, runner, backend, repos: dict,
                       *, deadline: float) -> dict[str, str]:
    """Map only unchanged PID/start-time identities to owning-host units."""
    units = set(backend.factory_units(timeout=remaining_timeout(deadline)))
    units.update(f"{host.unit_name(slug)}{suffix}.service" for slug in repos for suffix in ("", "-dashboard"))
    services = sorted(unit for unit in units if unit.endswith(".service"))
    config = backend.configuration()

    def probe(argv):
        proc = runner(argv, timeout=remaining_timeout(deadline))
        if proc.returncode:
            raise OSError(proc.stderr.strip() or f"{argv[0]} ownership probe failed")
        return proc.stdout

    pids = [work.split()[1] for work in processes]

    def unchanged(pid, current):
        if pid not in identities or pid not in current or identities[pid] != current[pid]:
            raise OSError(f"pid {pid} process identity changed or disappeared")

    if config.get("platform") == "linux":
        output = probe([str(config.get("executable", "systemctl")), "--user", "show",
                        "-p", "Id", "-p", "ControlGroup", *services])
        groups = {}
        for block in output.strip().split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if values.get("Id") in services and values.get("ControlGroup", "").startswith("/"):
                groups[values["ControlGroup"]] = values["Id"]
        if not groups:
            return {}
        current = _linux_identities(runner, deadline=deadline, pids=pids)
        owners = {}
        for pid in pids:
            if pid not in identities or pid not in current or identities[pid][0] != current[pid][0]:
                raise OSError(f"pid {pid} process identity changed or disappeared")
            paths = [line.split(":", 2)[2] for line in current[pid][1].splitlines()]
            for group, unit in groups.items():
                if any(path == group or path.startswith(group + "/") for path in paths):
                    owners[pid] = unit
        return owners
    if config.get("platform") == "macos":
        labels = {unit.removesuffix(".service"): unit for unit in services}
        owners = {}
        for line in probe([str(config.get("executable", "launchctl")), "list"]).splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0].isdigit() and fields[2] in labels:
                owners[fields[0]] = labels[fields[2]]
        current, _ = _ps_identities(probe(["ps", "-eo", "pid=,ppid=,lstart="]))
        anchors = dict(owners)
        for pid in pids:
            ancestor, seen = pid, set()
            while ancestor not in seen:
                seen.add(ancestor)
                unchanged(ancestor, current)
                if ancestor in anchors:
                    owners[pid] = anchors[ancestor]
                    break
                parent = identities[ancestor][1]
                if parent in ("0", "1"):
                    break
                ancestor = parent
        return owners
    raise OSError(f"unsupported ownership platform: {config.get('platform')}")


def _process_work(tool: Path, cli: Path, runner, backend, repos: dict, *, deadline: float) -> tuple[list[str], list[str]]:
    processes, identities = _factory_processes(
        tool, cli, runner, platform=backend.configuration().get("platform"), deadline=deadline)
    if not processes:
        return [], []
    try:
        owners = _managed_processes(processes, identities, runner, backend, repos, deadline=deadline)
        remaining_timeout(deadline)
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.TimeoutExpired,
            host.RemoteHostError) as exc:
        return [f"Factory process ownership unavailable: {exc}", *processes], processes
    outside = [work for work in processes
               if (work.split()[1] not in owners or owners[work.split()[1]].endswith("-dashboard.service"))
               and work.endswith(("factory gate", "factory dispatch"))]
    return outside, processes


def _hand_work(tool: Path, cli: Path, runner, backend, repos: dict, *, deadline: float) -> list[str]:
    busy = _busy_handland(backend, runner, timeout=remaining_timeout(deadline))
    outside, _ = _process_work(tool, cli, runner, backend, repos, deadline=deadline)
    return busy + outside


def _restore_engine_timers(data: dict, name: str, runner, backend, record, *, deadline: float | None = None) -> None:
    paused = (host.engine(data, name) or {}).get("paused_timers", [])
    if not paused:
        return
    blocked = (host.engine(data, name) or {}).get("install_pending", {}).get("rollback_blocked")
    if blocked:
        print(f"engine:{name} FAIL keeping timers paused after blocked rollback: {', '.join(paused)}: {blocked}")
        return
    if deadline is None:
        deadline = time.monotonic() + RESTORE_TIMEOUT
    try:
        tool, cli = host.factory_tool_paths(runner, timeout=remaining_timeout(deadline))
        _verify_engine(_receipt_sha(tool, runner, timeout=remaining_timeout(deadline)),
                       tool, cli, runner, deadline=deadline)
    except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
        print(f"engine:{name} FAIL verification; keeping timers paused: {', '.join(paused)}: {exc}")
        raise
    remaining = list(paused)
    for timer in paused:
        try:
            proc = backend.resume_timer(timer, timeout=min(SMOKE_TIMEOUT, remaining_timeout(deadline)))
            if proc.returncode:
                raise OSError(f"exited {proc.returncode}")
        except (Exception, DistrictError) as exc:
            print(f"engine:{name} FAIL restoring {timer}: {exc}")
            continue
        remaining.remove(timer)
        if record:
            record.reenabled.add(timer)
    _save_engine(data, name, runner, deadline=deadline, paused_timers=remaining)
    if remaining:
        raise DistrictError(f"could not restore paused timers: {', '.join(remaining)}")


@contextmanager
def _engine_window(data: dict, name: str, runner, backend, repos: dict, record, *, deadline: float | None = None):
    target = "engine" if name == "local" else f"engine:{name}"
    if deadline is None:
        timeout = hours(host.default(data, "engine_idle_timeout"), "[defaults].engine_idle_timeout") * 3600
        deadline = time.monotonic() + timeout
    try:
        ready = False
        try:
            services, timers = _engine_units(repos, backend, timeout=remaining_timeout(deadline))
            active = [timer for timer in timers
                      if backend.is_active(timer, timeout=remaining_timeout(deadline)) == "active"]
            tool, cli = host.factory_tool_paths(runner, timeout=remaining_timeout(deadline))
            hand_work = _hand_work(tool, cli, runner, backend, repos, deadline=deadline)
            if hand_work:
                _defer(record, target, hand_work)
            else:
                if record:
                    record.observe(data)
                for timer in active:
                    remaining_timeout(deadline)
                    paused = list((host.engine(data, name) or {}).get("paused_timers", []))
                    _save_engine(data, name, runner, deadline=deadline,
                                 paused_timers=paused if timer in paused else paused + [timer])
                    remaining = remaining_timeout(deadline)
                    if record:
                        record.disabled.add(timer)
                    backend.pause_timer(timer, timeout=remaining)
                while True:
                    hand_work = _busy_handland(backend, runner, timeout=remaining_timeout(deadline))
                    if hand_work:
                        _defer(record, target, hand_work)
                        break
                    busy = [unit for unit in services if backend.is_active(
                        unit, timeout=remaining_timeout(deadline)) in ("active", "activating", "deactivating")]
                    hand_work, processes = _process_work(tool, cli, runner, backend, repos, deadline=deadline)
                    busy += processes
                    if hand_work:
                        _defer(record, target, hand_work)
                        break
                    if not busy:
                        ready = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _defer(record, target, busy)
                        break
                    time.sleep(min(1, remaining))
        except subprocess.TimeoutExpired:
            _defer(record, target, ["idle wait timed out"])
        yield deadline if ready else None
    finally:
        _restore_engine_timers(data, name, runner, backend, record)

def _finish_dashboards(data: dict, name: str, runner, repos: dict, backend, *, deadline: float | None = None) -> None:
    if (host.engine(data, name) or {}).get("dashboards_pending"):
        if deadline is None:
            deadline = time.monotonic() + RESTORE_TIMEOUT
        tool, cli = host.factory_tool_paths(runner, timeout=remaining_timeout(deadline))
        _verify_engine(_receipt_sha(tool, runner, timeout=remaining_timeout(deadline)),
                       tool, cli, runner, deadline=deadline)
        restart_dashboards(repos, backend, deadline=deadline)
        _save_engine(data, name, runner, deadline=deadline, dashboards_pending=False)



def _replace_engine(data: dict, name: str, runner, origin: str, ref: str, sha: str,
                    previous: str | None, tool: Path, cli: Path, *, backend, repos: dict, deadline: float) -> str:
    extras = factory_extras(data)
    install_url = "ssh://" + origin.replace(":", "/", 1) if origin.startswith("git@") else origin
    _save_engine(data, name, runner, install_pending={
        "target": sha, "ref": ref, **({"previous": previous} if previous else {})})

    def install(revision):
        source = f"git+{install_url}@{revision}" + (f"[{','.join(extras)}]" if extras else "")
        proc = runner(["uv", "tool", "install", "--force", "--from", source, "factory"], timeout=DRAIN_TIMEOUT)
        if proc.returncode:
            raise OSError((proc.stderr or proc.stdout).strip() or "uv tool install failed")
        _verify_engine(revision, tool, cli, runner)

    try:
        install(sha)
    except (OSError, subprocess.TimeoutExpired, host.RemoteHostError) as exc:
        print(f"engine failed {previous or 'unknown'} -> {sha}: {exc}")
        if previous:
            try:
                busy = _hand_work(tool, cli, runner, backend, repos, deadline=deadline)
                if busy:
                    raise OSError(f"outside work blocks rollback: {', '.join(busy)}")
            except (OSError, subprocess.TimeoutExpired, host.RemoteHostError) as rollback:
                reason = ("original idle deadline expired" if isinstance(rollback, subprocess.TimeoutExpired)
                          else str(rollback))
                pending = dict((host.engine(data, name) or {})["install_pending"], rollback_blocked=reason)
                _save_engine(data, name, runner, install_pending=pending)
                print(f"engine rollback FAIL {sha} -> {previous} on {name}: {reason}; retaining recovery intent")
                return "failed"
            try:
                install(previous)
            except (OSError, subprocess.TimeoutExpired, host.RemoteHostError) as rollback:
                print(f"engine rollback FAIL {sha} -> {previous}: {rollback}")
            else:
                _save_engine(data, name, runner, sha=previous, dashboards_pending=True, install_pending={})
                print(f"engine rolled back {sha} -> {previous}")
        return "failed"
    _save_engine(data, name, runner, ref=ref, sha=sha, installed_at=iso(now()), dashboards_pending=True,
                 install_pending={}, **({"previous": previous} if previous else {}))
    print(f"installed factory engine {sha[:12]} ({ref})")
    return "applied"


def _recover_engine(data: dict, name: str, runner, backend, repos: dict, record) -> str | None:
    """Finish the durable transaction before consulting a new remote policy."""
    engine = host.engine(data, name) or {}
    pending = engine.get("install_pending") or {}
    if not pending and not engine.get("paused_timers") and not engine.get("dashboards_pending"):
        return None
    deadline = time.monotonic() + RESTORE_TIMEOUT
    stored = installed_engine_commit(data, name)
    try:
        tool, cli = host.factory_tool_paths(runner, timeout=remaining_timeout(deadline))
        receipt = _receipt_sha(tool, runner, timeout=remaining_timeout(deadline))
        try:
            _verify_engine(pending.get("target") or receipt, tool, cli, runner, deadline=deadline)
        except (OSError, subprocess.TimeoutExpired):
            target = pending.get("target") or engine.get("target") or receipt or stored
            if not isinstance(target, str) or not SHA.fullmatch(target):
                raise OSError("no recorded revision available for recovery")
            previous = (pending.get("previous") if pending else
                        stored if stored != target else engine.get("previous"))
            with _engine_window(data, name, runner, backend, repos, record) as window_deadline:
                if window_deadline is None:
                    return "deferred"
                result = _replace_engine(data, name, runner, host.default(data, "factory_repo"),
                                         pending.get("ref") or engine.get("target_ref", engine.get("ref", "main")),
                                         target, previous, tool, cli, backend=backend, repos=repos,
                                         deadline=window_deadline)
            _finish_dashboards(data, name, runner, repos, backend)
            return result
        if receipt != stored or pending:
            _save_engine(data, name, runner, sha=receipt, install_pending={},
                         ref=pending.get("ref") or engine.get("target_ref", engine.get("ref", "unknown")),
                         installed_at=iso(now()), dashboards_pending=True, deadline=deadline)
        if engine.get("paused_timers"):
            _restore_engine_timers(data, name, runner, backend, record, deadline=deadline)
        _finish_dashboards(data, name, runner, repos, backend, deadline=deadline)
    except (OSError, subprocess.TimeoutExpired, host.RemoteHostError) as exc:
        print(f"engine:{name} recovery failed: {exc}; retaining recovery state")
        paused = (host.engine(data, name) or {}).get("paused_timers", [])
        if paused:
            print(f"engine:{name} FAIL keeping timers paused: {', '.join(paused)}")
        return "failed"
    return None


def reconcile_engine(data: dict, record: install_state.ApplyRun | None = None, *, backend=None,
                     host_name: str = "local", repos: dict[str, dict] | None = None,
                     run_cmd=None, pin: tuple[str, str] | None = None, verify_ci: bool = True,
                     allow_downgrade: bool = False, ref: str | None = None,
                     window_deadline: float | None = None) -> str:
    """Converge this host's one uv tool to its recorded, remote CI-green target."""
    run_cmd = run if run_cmd is None else run_cmd
    backend = backend or host.service_backend()
    repos = _host_repos(data, host_name) if repos is None else repos
    engine_target = "engine" if host_name == "local" else f"engine:{host_name}"
    if window_deadline is None:
        recovery = _recover_engine(data, host_name, run_cmd, backend, repos, record)
        if recovery is not None:
            return recovery
    explicit_ref = ref is not None
    if ref is None:
        ref = pin[0] if pin is not None else data.get("defaults", {}).get("factory_ref")
    if ref is None and pin is None:
        if record:
            record.clear_deferred(engine_target)
        return "unchanged"
    if not isinstance(ref, str) or not ref.strip():
        raise DistrictError("[defaults].factory_ref must be a non-empty string")
    factory_extras(data)
    origin = host.default(data, "factory_repo")
    try:
        origin, slug, resolved_ref, sha = _resolve_engine(ref, run_cmd, origin, pin, verify_ci=verify_ci)
    except subprocess.TimeoutExpired as exc:
        print(f"{engine_target} {ref} unresolved: {exc}; keeping installed engine")
        return "failed"
    except (OSError, ValueError, TypeError) as exc:
        print(f"{engine_target} {ref} unresolved: {exc}; keeping installed engine")
        return "failed" if explicit_ref or host_name != "local" or pin is not None else "unchanged"
    try:
        _save_engine(data, host_name, run_cmd, target=sha, target_ref=resolved_ref, resolved_at=iso(now()))
        tool, cli = host.factory_tool_paths(run_cmd)
        receipt = _receipt_sha(tool, run_cmd)
        stored_sha = installed_engine_commit(data, host_name)
        previous = receipt or stored_sha
        if _engine_current(sha, tool, cli, run_cmd):
            if stored_sha != sha:
                _save_engine(data, host_name, run_cmd, sha=sha, ref=resolved_ref,
                             installed_at=iso(now()), dashboards_pending=True)
            if window_deadline is None:
                _finish_dashboards(data, host_name, run_cmd, repos, backend)
            if record:
                record.clear_deferred(engine_target)
            print(f"engine {sha[:7]} ({resolved_ref})")
            return "unchanged"
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{engine_target} failed preparing {sha}: {exc}; retaining recovery state")
        return "failed"
    if receipt == sha:
        previous = stored_sha if stored_sha != sha else (host.engine(data, host_name) or {}).get("previous")
    try:
        _downgrade_guard(slug, previous, sha, run_cmd, allow_downgrade)
    except subprocess.TimeoutExpired as exc:
        print(f"{engine_target} failed ancestry {previous} -> {sha}: {exc}; keeping installed engine")
        return "failed"
    try:
        if window_deadline is not None:
            result = _replace_engine(data, host_name, run_cmd, origin, resolved_ref, sha, previous, tool, cli,
                                     backend=backend, repos=repos, deadline=window_deadline)
        else:
            with _engine_window(data, host_name, run_cmd, backend, repos, record) as window_deadline:
                if window_deadline is None:
                    return "deferred"
                result = _replace_engine(data, host_name, run_cmd, origin, resolved_ref, sha, previous, tool, cli,
                                         backend=backend, repos=repos, deadline=window_deadline)
            _finish_dashboards(data, host_name, run_cmd, repos, backend)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{engine_target} failed {previous or 'unknown'} -> {sha}: {exc}")
        return "failed"
    if result == "applied" and record:
        record.clear_deferred(engine_target)
    return result




def reconcile_fleet_engine(data: dict, record: install_state.ApplyRun | None,
                           contexts: dict[str, tuple], *, allow_downgrade: bool = False,
                           ref: str | None = None) -> str:
    """Hold every changed host's upgrade window before moving any shared-repo engine."""
    recovery_result = None
    for name, (runner, backend, repos) in contexts.items():
        try:
            recovered = _recover_engine(data, name, runner, backend, repos, record)
        except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
            print(f"engine:{name} recovery failed: {exc}; retaining recovery state")
            recovered = "failed"
        if recovered in ("failed", "deferred"):
            recovery_result = recovered
    if recovery_result:
        return recovery_result
    ref = ref if ref is not None else data.get("defaults", {}).get("factory_ref")
    if ref == "HEAD":
        ref = data.get("defaults", {}).get("factory_ref", "main")
    if ref is None:
        return "unchanged"
    names = ", ".join(contexts)
    origin = host.default(data, "factory_repo")
    resolved, previous = {}, {}
    for name, (runner, backend, _) in contexts.items():
        try:
            _, slug, resolved_ref, sha = _resolve_engine(ref, runner, origin)
            tool, _ = host.factory_tool_paths(runner)
            receipt = _receipt_sha(tool, runner)
            stored = installed_engine_commit(data, name)
            previous[name] = stored if receipt == sha and stored != sha else receipt or stored
            _downgrade_guard(slug, previous[name], sha, runner, allow_downgrade)
        except (OSError, ValueError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
            print(f"engine fleet ({names}) held by {name}: {exc}; every host keeps its engine")
            return "failed"
        resolved[name] = (resolved_ref, sha)
    if len(set(resolved.values())) > 1:
        detail = ", ".join(f"{name} {sha[:12]}" for name, (_, sha) in resolved.items())
        print(f"engine fleet ({names}) held: {ref} resolves differently ({detail}); every host keeps its engine")
        return "failed"
    pin = next(iter(resolved.values()))
    for name, (runner, _, _) in contexts.items():
        try:
            _save_engine(data, name, runner, target=pin[1], target_ref=pin[0], resolved_at=iso(now()))
        except subprocess.TimeoutExpired as exc:
            print(f"engine fleet ({names}) held by {name}: {exc}; every host keeps its engine")
            return "failed"
    moved, attempted = [], []
    result = "unchanged"
    opened = set()
    deadlines = {}

    def hand_ready(name):
        runner, backend, repos = contexts[name]
        try:
            tool, cli = host.factory_tool_paths(runner, timeout=remaining_timeout(deadlines[name]))
            busy = _hand_work(tool, cli, runner, backend, repos, deadline=deadlines[name])
        except subprocess.TimeoutExpired:
            busy = ["idle wait timed out"]
        if busy:
            _defer(record, "engine" if name == "local" else f"engine:{name}", busy)
        return not busy

    # Preflight every changed host before pausing any host's timers.
    for name, (runner, backend, _) in contexts.items():
        try:
            tool, cli = host.factory_tool_paths(runner)
            if (_engine_current(pin[1], tool, cli, runner)
                    and not (host.engine(data, name) or {}).get("paused_timers")):
                continue
            timeout = hours(host.default(data, "engine_idle_timeout"), "[defaults].engine_idle_timeout") * 3600
            deadlines[name] = time.monotonic() + timeout
            if not hand_ready(name):
                print(f"engine fleet ({names}) held by {name} (busy): every host keeps its engine")
                return "deferred"
        except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
            print(f"engine fleet ({names}) held by {name}: {exc}; every host keeps its engine")
            return "failed"
    with ExitStack() as windows:
        for name, (runner, backend, repos) in contexts.items():
            if name not in deadlines:
                continue
            try:
                ready = windows.enter_context(_engine_window(
                    data, name, runner, backend, repos, record, deadline=deadlines[name]))
            except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
                print(f"engine fleet ({names}) held by {name}: {exc}; every host keeps its engine")
                return "failed"
            if not ready:
                print(f"engine fleet ({names}) held by {name} (busy): every host keeps its engine")
                return "deferred"
            opened.add(name)
        # A later host's drain can outlive an earlier host's readiness probe.
        for name in deadlines:
            try:
                if not hand_ready(name):
                    print(f"engine fleet ({names}) held by {name} (busy): every host keeps its engine")
                    return "deferred"
            except (OSError, host.RemoteHostError, DistrictError) as exc:
                print(f"engine fleet ({names}) held by {name}: {exc}; every host keeps its engine")
                return "failed"
        for name, (runner, backend, repos) in contexts.items():
            attempted.append(name)
            try:
                if name in opened and not hand_ready(name):
                    result = "deferred"
                else:
                    result = reconcile_engine(data, record, backend=backend, host_name=name, repos=repos,
                                              run_cmd=runner, pin=pin, verify_ci=False, window_deadline=deadlines.get(name),
                                              allow_downgrade=allow_downgrade)
                why = result
            except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
                result, why = "failed", f"failed: {exc}"
            if result == "applied":
                moved.append(name)
                continue
            if result == "unchanged" and installed_engine_commit(data, name) == pin[1]:
                continue
            stuck = []
            for back in moved:
                back_cmd, back_backend, back_repos = contexts[back]
                tool, cli = host.factory_tool_paths(back_cmd)
                try:
                    if back not in opened:
                        deadlines[back] = windows.enter_context(_engine_window(
                            data, back, back_cmd, back_backend, back_repos, record))
                        if deadlines[back] is None:
                            raise OSError("rollback idle wait timed out")
                        opened.add(back)
                    if not hand_ready(back):
                        raise OSError("hand work or expired idle deadline blocks rollback")
                    restored = bool(previous[back]) and _replace_engine(
                        data, back, back_cmd, origin, "previous", previous[back], pin[1], tool, cli,
                        backend=back_backend, repos=back_repos, deadline=deadlines[back],
                    ) == "applied"
                except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
                    print(f"engine:{back} rollback FAIL: {exc}")
                    restored = False
                if not restored:
                    stuck.append(back)
            rolled = [back for back in moved if back not in stuck]
            print(f"engine fleet ({names}) held by {name} ({why}): every host keeps its engine"
                  + (f"; rolled back {', '.join(rolled)}" if rolled else "")
                  + (f"; FAIL could not roll back {', '.join(stuck)}" if stuck else ""))
            result = "failed"
            break
    for name in attempted:
        runner, backend, repos = contexts[name]
        try:
            _finish_dashboards(data, name, runner, repos, backend)
        except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, DistrictError) as exc:
            print(f"engine:{name} dashboard restart FAIL: {exc}")
            result = "failed"
    return "failed" if result == "failed" else ("applied" if moved else "unchanged")


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


def _argv_error(argv, tool: Path, cli: Path, runner) -> str | None:
    if not argv:
        return "missing Factory argv"
    executable = Path(argv[0])
    offset = 2 if len(argv) > 1 and argv[1] == "-P" else 1
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable.name):
        parent, tool_bin = _resolved_paths([executable.parent, tool / "bin"], runner)
        if parent == tool_bin and list(argv[offset:offset + 2]) == ["-m", "factory"]:
            return None
    else:
        resolved, real_cli = _resolved_paths([executable, cli], runner)
        if resolved == real_cli:
            return None
    return f"Factory argv must use {cli} or {tool}/bin/python -m factory; got {shlex.join(argv)}"


def _render_errors(output: str, tool: Path, cli: Path, runner) -> list[str]:
    units = _rendered_units(output)
    if not any(name.endswith((".service", ".plist")) for name in units):
        return ["factory install --print: no rendered Factory service"]
    errors = []
    for name, body in units.items():
        if name.endswith(".timer"):
            continue
        try:
            if name.endswith(".plist"):
                argv = plistlib.loads(body.encode()).get("ProgramArguments")
            else:
                commands = [line.removeprefix("ExecStart=").strip() for line in body.splitlines()
                            if line.startswith("ExecStart=")]
                argv = shlex.split(commands[0]) if len(commands) == 1 else None
            error = _argv_error(argv, tool, cli, runner)
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError,
                plistlib.InvalidFileException) as exc:
            error = f"invalid Factory argv: {exc}"
        if error:
            errors.append(f"{name}: {error}")
    return errors


def plan_apply(targets: dict[str, dict], backend, *, run_cmd=None, env=None,
               errors: dict[str, list[str]] | None = None) -> dict[str, bool]:
    run_cmd = run if run_cmd is None else run_cmd
    backend = backend or host.service_backend()
    changes = {}
    tool, cli = host.factory_tool_paths(run_cmd)
    for slug, table in targets.items():
        if table.get("disabled_at"):
            changes[slug] = False
            continue
        kwargs = {"env": env} if env is not None else {}
        proc = run_cmd([str(cli), "install", "--print"], cwd=Path(table["path"]), **kwargs)
        if errors is not None:
            failures = ([f"factory install --print exited {proc.returncode}: {proc.stderr.strip()}"]
                        if proc.returncode else _render_errors(proc.stdout, tool, cli, run_cmd))
            if failures:
                errors[slug] = failures
        try:
            changes[slug] = proc.returncode != 0 or _units_changed(slug, proc.stdout, backend)
        except OSError as exc:
            if errors is None:
                raise DistrictError(str(exc)) from exc
            errors.setdefault(slug, []).append(str(exc))
            changes[slug] = True
    return changes


def busy_units(services: list[str], backend=None, *, timeout: float | None = None) -> list[str]:
    backend = backend or host.service_backend()
    kwargs = {"timeout": timeout} if timeout is not None else {}
    return [service for service in services
            if backend.is_active(service, **kwargs) in ("active", "activating", "deactivating")]


def _python_entrypoint(argv: list[str]) -> list[str]:
    """Stop at Python's module/script boundary, not text in script arguments."""
    args = iter(argv)
    for arg in args:
        if arg in {"-", "--"}:
            return []
        if arg == "--check-hash-based-pycs":
            next(args, None)
        elif not arg.startswith("-"):
            return [arg, *args]
        elif option := re.match(r"^-[^-]*?([cmWX])(.*)$", arg):
            # Short options can group; c/m/W/X consume the rest or the next token.
            flag, value = option.groups()
            if flag == "c":
                return []
            value = value or next(args, "")
            if flag == "m":
                return ["-m", value, *args]
    return []


def _python_factory_args(argv: list[str], tool_bins: set[Path], launchers: set[str]) -> list[str]:
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(argv[0]).name):
        return []
    args = _python_entrypoint(argv[1:])
    if args and args[0] in launchers:
        return args[1:]
    if Path(argv[0]).parent in tool_bins and args[:2] == ["-m", "factory"]:
        return args[2:]
    return []


def _factory_work(command: str, tool_bins: set[Path], launchers: set[str]) -> str | None:
    """Recognize Factory entrypoints, not text mentioning commands or dashboards."""
    argv = command.split()
    if not argv:
        return None
    if argv[0] in launchers or argv[0] == "factory":
        args = argv[1:]
    else:
        args = _python_factory_args(argv, tool_bins, launchers)
    if args and args[0] != "dashboard":
        return f"factory {args[0]}"
    return None


def _busy_handland(backend, run_cmd, *, timeout: float) -> list[str]:
    config = backend.configuration()
    if config.get("platform") == "linux":
        units = run_cmd([
            str(config.get("executable", "systemctl")), "--user", "list-units", "--no-legend", "--plain",
            "--state=active,activating,deactivating", "handland-*",
        ], timeout=timeout)
        if units.returncode:
            raise OSError(units.stderr.strip() or "cannot inspect handland user units")
        return [line.split()[0] for line in units.stdout.splitlines() if line.strip()]
    return []




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


def restart_dashboards(repos: dict[str, dict], backend=None, *, deadline: float | None = None) -> None:
    backend = backend or host.service_backend()
    if deadline is None:
        deadline = time.monotonic() + RESTORE_TIMEOUT
    dashboards = {f"{host.unit_name(slug)}-dashboard.service" for slug in repos}
    dashboards.update(name for name in backend.factory_units(timeout=remaining_timeout(deadline))
                      if name.endswith("-dashboard.service"))
    for dash in sorted(dashboards):
        if backend.status(dash, timeout=remaining_timeout(deadline)).loaded:
            proc = backend.start(dash, restart=True, check=False, timeout=remaining_timeout(deadline))
            if proc.returncode:
                raise OSError(f"{dash}: restart exited {proc.returncode}")


def upgrade(data: dict, ref: str | None = None, record: install_state.ApplyRun | None = None, *,
            backend=None, host_name: str = "local", repos: dict[str, dict] | None = None,
            run_cmd=None, allow_downgrade: bool = False) -> str:
    """Explicit upgrades use the same remote target and verified uv tool as plain apply."""
    if ref in (None, "HEAD"):
        ref = data.get("defaults", {}).get("factory_ref", "main")
    return reconcile_engine(data, record, backend=backend, host_name=host_name, repos=repos,
                            run_cmd=run_cmd, ref=ref, allow_downgrade=allow_downgrade)


def restore(timers: list[str], repos: dict[str, dict],
            record: install_state.ApplyRun | None = None, *, backend=None) -> None:
    backend = backend or host.service_backend()
    disabled = {f"{host.unit_name(s)}.timer" for s, table in repos.items() if table.get("disabled_at")}
    for timer in timers:
        if timer not in disabled and backend.is_active(timer) != "active":
            backend.enable(timer, check=False)
        if record and timer in record.disabled and backend.is_active(timer) == "active":
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


def _host_context(data: dict, host_name: str):
    """(run_cmd, backend) for one host; a remote host gets its synced factory config first."""
    if host_name == "local":
        return run, host.service_backend()
    target = data["hosts"][host_name]
    run_cmd = host.remote_runner(host_name, target)
    host.sync_remote_config(data, host_name, run_cmd)
    return run_cmd, host.service_backend(target, run=run_cmd)


def _apply_host(data: dict, host_name: str, targets: dict[str, dict], args: argparse.Namespace,
                record: install_state.ApplyRun, status: str, engine_result: str | None = None,
                context: tuple | None = None) -> tuple[list[dict], int, tuple[int, list[str]]]:
    """`context` is the (run_cmd, backend) the fleet engine step already built for this host."""
    all_repos = _host_repos(data, host_name)
    run_cmd, backend = context or _host_context(data, host_name)
    install_env = None
    if host_name != "local":
        facts = run_cmd.facts()
        install_env = {"PATH": host.service_path(facts["path"], home=Path(facts["home"]))}

    rows = []
    completed = {}
    code = 0
    engine_result = engine_result or (
        upgrade(
            data, args.upgrade, record, backend=backend, host_name=host_name,
            repos=all_repos, run_cmd=run_cmd, allow_downgrade=args.allow_downgrade,
        )
        if args.upgrade
        else reconcile_engine(
            data, record, backend=backend, host_name=host_name,
            repos=all_repos, run_cmd=run_cmd, allow_downgrade=args.allow_downgrade,
        )
    )
    if engine_result == "failed":
        code = 1
    tool, cli = host.factory_tool_paths(run_cmd)
    try:
        usable = _tool_cli(tool, cli, run_cmd) and _receipt_sha(tool, run_cmd) is not None
    except (OSError, subprocess.TimeoutExpired) as exc:
        usable = False
        print(f"engine validation at {cli}: {exc}")
    if not usable:
        reason = f"engine unavailable at {cli}: must resolve into {tool} with a valid uv receipt; repository checks skipped"
        for slug, table in targets.items():
            row = {"slug": slug, "version": None, "doctor": "?", "drift": [], "failures": None,
                   "fail": [reason], "disabled": table.get("disabled_at"), "timer": "unknown"}
            rows.append(row)
            print(line(row, status))
        return rows, 1, (0, [])

    record.observe(data)
    for slug, table in targets.items():
        if args.reset and not reset(slug, table, data, record, backend=backend):
            code = 1
    render_errors = {}
    changes = plan_apply(targets, run_cmd=run_cmd, backend=backend, env=install_env, errors=render_errors)
    _, factory_cli = host.factory_tool_paths(run_cmd)
    for slug, table in targets.items():
        if engine_result == "failed":
            row = repo_pass(slug, table, data, record, backend=backend, run_cmd=run_cmd)
            row["fail"].append("engine step failed; install skipped")
        elif slug in render_errors:
            row = repo_pass(slug, table, data, record, backend=backend, run_cmd=run_cmd)
            row["fail"].extend(render_errors[slug])
        elif table.get("disabled_at"):
            row = repo_pass(slug, table, data, record, backend=backend, run_cmd=run_cmd)
        elif not changes[slug] and backend.is_active(f"{host.unit_name(slug)}.timer") == "active":
            row = repo_pass(slug, table, data, record, backend=backend, run_cmd=run_cmd)
            record.clear_deferred(slug)
        else:
            service = f"{host.unit_name(slug)}.service"
            changed = changes[slug]
            if changed and (busy := busy_units([service], backend)):
                reason = f"busy: {', '.join(busy)}"
                record.defer(slug, reason)
                rows.append(deferred_row(slug, table, reason, backend))
                continue
            timer = f"{host.unit_name(slug)}.timer"
            timer_state = backend.is_active(timer) if changed else None
            if timer_state == "inactive" and backend.status(timer).unit_file_state == "not-found":
                timer_state = None  # First install has no operator state to preserve.
            active_timers = [timer] if timer_state == "active" else []
            install_error = None
            try:
                if changed and (busy := drain(active_timers, [service], data, record, backend=backend)):
                    reason = f"busy: {', '.join(busy)}"
                    record.defer(slug, reason)
                    rows.append(deferred_row(slug, table, reason, backend))
                    continue
                try:
                    proc = run_cmd(
                        [str(factory_cli), "install"],
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
                if timer_state == "inactive" and backend.is_active(timer) == "active":
                    backend.stop(timer, timeout=DRAIN_TIMEOUT)
                    record.stopped.add(timer)
            row = repo_pass(slug, table, data, record, backend=backend, run_cmd=run_cmd)
            if install_error:
                row["fail"].insert(0, install_error)
            else:
                record.clear_deferred(slug)
        rows.append(row)
        completed[slug] = table

    if isinstance(run_cmd, host.RemoteRunner):
        host.sync_remote_config(data, host_name, run_cmd)
    tool, cli = host.factory_tool_paths(run_cmd)
    if engine_result in ("applied", "unchanged") and _receipt_sha(tool, run_cmd) and _tool_cli(tool, cli, run_cmd):
        cleanup(data, run_cmd=run_cmd)
    current_engine = host.engine(data, host_name)
    if current_engine and not args.upgrade and "factory_ref" not in data.get("defaults", {}):
        installed = installed_engine_commit(data, host_name)
        print(f"engine {installed[:7] if installed else 'unknown'} ({current_engine.get('ref', 'unknown')})")
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
            host.save(data)
            print("no repositories registered (district add)")
            return 0
        groups: dict[str, dict[str, dict]] = {}
        for slug, table in selected.items():
            for name in host.repo_hosts(table, slug):
                groups.setdefault(name, {})[slug] = table
        fleet = {slug: table for slug, table in registered.items() if host.multi_host(table, slug)}
        fleet_hosts = list(dict.fromkeys(name for slug, table in fleet.items() for name in host.repo_hosts(table, slug)))
        # A selection that runs on no fleet host leaves the fleet alone: single-host repos keep
        # their old behavior (#134). One that does moves every fleet host's engine together.
        if not any(name in groups for name in fleet_hosts):
            fleet, fleet_hosts = {}, []
        if fleet:
            if blocked := host.claims_blockers(data, fleet_hosts):
                raise DistrictError(f"{', '.join(fleet)} lists several hosts, but {blocked}; list one host until it does")
        engine_hosts = list(dict.fromkeys([*fleet_hosts, *groups])) if (
            fleet_hosts or data.get("defaults", {}).get("factory_ref") or args.upgrade
        ) else []
        if len(engine_hosts) < 2 and not fleet_hosts:
            engine_hosts = []
        host.sync_local_claims(data)
        status, warnings = policy(data)
        host.save(data)

        rows = []
        smokes = []
        code = 0
        contexts = {}
        fleet_result = None
        context_failures = {}
        if engine_hosts:
            for name in engine_hosts:
                try:
                    run_cmd, backend = _host_context(data, name)
                    contexts[name] = (run_cmd, backend, _host_repos(data, name))
                except (host.RemoteHostError, DistrictError, subprocess.TimeoutExpired) as exc:
                    context_failures[name] = exc
            try:
                if context_failures:
                    failures = "; ".join(f"{name}: {exc}" for name, exc in context_failures.items())
                    raise DistrictError(f"host setup failed: {failures}")
                options = {}
                if args.upgrade:
                    options["ref"] = args.upgrade
                if args.allow_downgrade:
                    options["allow_downgrade"] = True
                fleet_result = reconcile_fleet_engine(data, record, contexts, **options)
                if fleet_result == "failed":
                    code = 1
            except (OSError, host.RemoteHostError, DistrictError, subprocess.TimeoutExpired) as exc:
                fleet_result = "failed"
                print(f"engine fleet ({', '.join(engine_hosts)}) held: {exc}; every host keeps its engine")
                code = 1
        for host_name, targets in groups.items():
            try:
                if host_name in context_failures:
                    raise context_failures[host_name]
                host_rows, host_code, host_smoke = _apply_host(
                    data, host_name, targets, args, record, status,
                    engine_result=fleet_result if host_name in engine_hosts else None,
                    context=contexts[host_name][:2] if host_name in contexts else None,
                )
            except host.RemoteHostError as exc:
                for slug in targets:
                    print(f"{slug}: {exc}")
                code = 1
                continue
            except subprocess.TimeoutExpired as exc:
                for slug in targets:
                    print(f"{slug}: host {host_name} operation failed: {exc}")
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
                        help="install the newest green remote REF (default factory_ref, or main)")
    parser.add_argument("--allow-downgrade", action="store_true",
                        help="allow a target that is an ancestor of the installed engine")
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
