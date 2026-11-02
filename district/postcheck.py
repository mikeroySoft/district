"""Post-upgrade checks run while a District drain still excludes new Factory passes."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from district import host, service

PROBE_TIMEOUT = 30
INSTALL_TIMEOUT = 600
SHA = re.compile(r"^[0-9a-f]{40}$")
_HTTP = (
    "import sys,urllib.request; "
    "response=urllib.request.urlopen(sys.argv[1],timeout=float(sys.argv[2])); "
    "print(response.status)"
)
_READ_RECEIPT = "import pathlib,sys; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())"
_REMOTE_TEMP = "import tempfile; print(tempfile.mkdtemp(prefix='district-factory-postcheck-'))"
_REMOTE_REMOVE = "import shutil,sys; shutil.rmtree(sys.argv[1])"
PROBE_ERRORS = (OSError, subprocess.TimeoutExpired, host.DistrictError, host.RemoteHostError)


def _detail(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exited {proc.returncode}"


def _exception(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {exc.timeout:g}s"
    return str(exc) or type(exc).__name__

def _timeout(deadline: float | None, maximum: float = PROBE_TIMEOUT) -> float:
    return maximum if deadline is None else min(maximum, service.remaining_timeout(deadline))


def _command(failures: list[str], label: str, runner, argv: list[str], *,
             deadline: float | None = None, **kwargs) -> bool:
    try:
        proc = runner(argv, timeout=_timeout(deadline), **kwargs)
    except PROBE_ERRORS as exc:
        failures.append(f"{label}: {_exception(exc)}")
        return False
    if proc.returncode:
        failures.append(f"{label}: {_detail(proc)}")
        return False
    return True




def _engine(failures: list[str], data: dict, name: str, runner, tool: Path, *,
            deadline: float | None) -> None:
    from district.apply import _receipt_sha, github_slug

    record = host.engine(data, name) or {}
    recorded = record.get("sha")
    target = record.get("target") or recorded
    try:
        receipt = _receipt_sha(tool, runner, timeout=_timeout(deadline))
    except PROBE_ERRORS as exc:
        failures.append(f"factory receipt: {_exception(exc)}")
    else:
        if not isinstance(recorded, str) or not SHA.fullmatch(recorded):
            failures.append("recorded engine revision unavailable")
        elif receipt != recorded:
            failures.append(f"receipt rev {receipt or 'unknown'} != recorded engine {recorded}")

    if not isinstance(recorded, str) or not SHA.fullmatch(recorded):
        return
    if not isinstance(target, str) or not SHA.fullmatch(target):
        failures.append("engine target revision unavailable")
        return
    if recorded == target:
        return
    slug = github_slug(str(host.default(data, "factory_repo")))
    if not slug:
        failures.append(f"recorded engine {recorded} is not target {target} or newer: factory repository is not GitHub")
        return
    argv = ["gh", "api", f"repos/{slug}/compare/{target}...{recorded}"]
    try:
        proc = runner(argv, timeout=_timeout(deadline))
        status = json.loads(proc.stdout).get("status") if proc.returncode == 0 else None
    except (*PROBE_ERRORS, ValueError, TypeError, AttributeError) as exc:
        failures.append(f"recorded engine {recorded} is not target {target} or newer: {_exception(exc)}")
        return
    if status not in ("ahead", "identical"):
        detail = _detail(proc) if proc.returncode else f"compare status {status or 'unknown'}"
        failures.append(f"recorded engine {recorded} is not target {target} or newer: {detail}")


def _http(failures: list[str], label: str, runner, url: str, *,
          deadline: float | None) -> None:
    try:
        timeout = _timeout(deadline)
        argv = ["python3", "-c", _HTTP, url, str(timeout)]
        proc = runner(argv, timeout=timeout)
    except PROBE_ERRORS as exc:
        failures.append(f"{label}: {_exception(exc)}")
        return
    raw = proc.stdout.strip().splitlines()
    status = raw[-1] if raw and raw[-1].isdigit() else None
    if proc.returncode or status != "200":
        failures.append(f"{label}: HTTP {status}" if status else f"{label}: {_detail(proc)}")


def _dashboard_port(argv: tuple[str, ...] | None) -> int | None:
    values = list(argv or ())
    indexes = [index for index, value in enumerate(values) if value == "--port"]
    if len(indexes) > 1 or indexes and indexes[0] + 1 >= len(values):
        return None
    try:
        port = int(values[indexes[0] + 1]) if indexes else 8760
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _dashboards(failures: list[str], runner, backend, repos: dict[str, dict], *,
                deadline: float | None) -> None:
    for slug, table in sorted(repos.items()):
        dashboard = table.get("dashboard") if isinstance(table, dict) else None
        port = dashboard.get("port") if isinstance(dashboard, dict) else None
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            failures.append(f"{slug} dashboard: registered port unavailable")
            continue
        _http(failures, f"{slug} dashboard", runner, f"http://127.0.0.1:{port}/",
              deadline=deadline)

    name = "district-dashboard.service"
    try:
        status = backend.status(name, timeout=_timeout(deadline))
    except PROBE_ERRORS as exc:
        failures.append(f"{name}: {_exception(exc)}")
        return
    if not status.loaded:
        return
    try:
        proc = backend.start(name, restart=True, timeout=_timeout(deadline))
    except PROBE_ERRORS as exc:
        failures.append(f"{name} restart: {_exception(exc)}")
    else:
        if proc.returncode:
            failures.append(f"{name} restart: {_detail(proc)}")
    port = _dashboard_port(status.argv)
    if port is None:
        failures.append(f"{name}: invalid dashboard port")
        return
    _http(failures, name, runner, f"http://127.0.0.1:{port}/", deadline=deadline)


def _checkout(failures: list[str], runner, repos: dict[str, dict], *,
              deadline: float | None) -> Path | None:
    for _, table in sorted(repos.items()):
        root = table.get("path") if isinstance(table, dict) else None
        if not isinstance(root, str) or not root:
            continue
        path = Path(root)
        try:
            proc = runner(
                ["git", "rev-parse", "--path-format=absolute", "--show-toplevel"],
                cwd=path,
                timeout=_timeout(deadline),
            )
        except PROBE_ERRORS:
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return path
    failures.append("factory watch --dry-run: no factory repo checkout")
    return None


def _install_source(data: dict, revision: str) -> str:
    origin = str(host.default(data, "factory_repo"))
    if origin.startswith("git@"):
        origin = "ssh://" + origin.replace(":", "/", 1)
    extras = host.default(data, "factory_extras")
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"git+{origin}@{revision}{suffix}"


def _verify_previous_at(failures: list[str], data: dict, runner, previous: str, root: Path,
                        *, deadline: float | None) -> None:
    from district.apply import _receipt_sha

    tool_dir, bin_dir = root / "tools", root / "bin"
    env = ({**os.environ, "UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(bin_dir)}
           if not isinstance(runner, host.RemoteRunner)
           else {"UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(bin_dir)})
    source = _install_source(data, previous)
    try:
        proc = runner(
            ["uv", "tool", "install", "--force", "--from", source, "factory"],
            env=env,
            timeout=_timeout(deadline, INSTALL_TIMEOUT),
        )
    except PROBE_ERRORS as exc:
        failures.append(f"previous engine install {previous}: {_exception(exc)}")
        return
    if proc.returncode:
        failures.append(f"previous engine install {previous}: {_detail(proc)}")
        return

    tool, cli = tool_dir / "factory", bin_dir / "factory"
    try:
        installed = _receipt_sha(tool, runner, timeout=_timeout(deadline))
    except PROBE_ERRORS as exc:
        failures.append(f"previous engine receipt: {_exception(exc)}")
    else:
        if installed != previous:
            failures.append(f"previous engine receipt rev {installed or 'unknown'} != {previous}")
    _command(failures, "previous engine factory --version", runner, [str(cli), "--version"],
             deadline=deadline)
    _command(
        failures,
        "previous engine import factory",
        runner,
        [str(tool / "bin" / "python"), "-c", "import factory"],
        deadline=deadline,
    )


def _previous(failures: list[str], data: dict, runner, previous: str | None, *,
              deadline: float | None) -> None:
    if previous is None:
        return
    if not SHA.fullmatch(previous):
        failures.append(f"previous engine revision invalid: {previous}")
        return
    if isinstance(runner, host.RemoteRunner):
        root = None
        try:
            proc = runner(["python3", "-c", _REMOTE_TEMP], timeout=_timeout(deadline))
            if proc.returncode or not proc.stdout.strip():
                failures.append(f"previous engine temporary directory: {_detail(proc)}")
                return
            root = Path(proc.stdout.strip())
            _verify_previous_at(failures, data, runner, previous, root, deadline=deadline)
        except PROBE_ERRORS as exc:
            failures.append(f"previous engine temporary directory: {_exception(exc)}")
        finally:
            if root is not None:
                try:
                    proc = runner(["python3", "-c", _REMOTE_REMOVE, str(root)], timeout=PROBE_TIMEOUT)
                    if proc.returncode:
                        failures.append(f"previous engine cleanup: {_detail(proc)}")
                except PROBE_ERRORS as exc:
                    failures.append(f"previous engine cleanup: {_exception(exc)}")
        return

    try:
        root = Path(tempfile.mkdtemp(prefix="district-factory-postcheck-"))
    except OSError as exc:
        failures.append(f"previous engine temporary directory: {_exception(exc)}")
        return
    try:
        _verify_previous_at(failures, data, runner, previous, root, deadline=deadline)
    finally:
        try:
            shutil.rmtree(root)
        except OSError as exc:
            failures.append(f"previous engine cleanup: {_exception(exc)}")


def check(data: dict, name: str, runner, backend, repos: dict[str, dict], *,
          previous: str | None = None, deadline: float | None = None) -> list[str]:
    """Return every failed post-upgrade check without abandoning later probes."""
    failures: list[str] = []
    try:
        tool, cli = host.factory_tool_paths(runner, timeout=_timeout(deadline))
    except PROBE_ERRORS as exc:
        failures.append(f"factory tool paths: {_exception(exc)}")
        tool, cli = None, Path("factory")
    _command(failures, "factory --help", runner, [str(cli), "--help"], deadline=deadline)
    _command(failures, "factory --version", runner, [str(cli), "--version"], deadline=deadline)
    if tool is None:
        failures.extend(("import factory: factory tool path unavailable",
                         "factory receipt: factory tool path unavailable"))
    else:
        _command(failures, "import factory", runner, [str(tool / "bin" / "python"), "-c", "import factory"],
                 deadline=deadline)
        _engine(failures, data, name, runner, tool, deadline=deadline)
    _dashboards(failures, runner, backend, repos, deadline=deadline)
    checkout = _checkout(failures, runner, repos, deadline=deadline)
    if checkout is not None:
        _command(
            failures,
            "factory watch --dry-run",
            runner,
            [str(cli), "watch", "--dry-run"],
            cwd=checkout,
            deadline=deadline,
        )
    _previous(failures, data, runner, previous, deadline=deadline)
    return failures
