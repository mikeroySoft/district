"""Stdlib-only transaction driver for ``district update``.

This file is copied outside the managed environment before it is executed.  It
must not import District or third-party packages: the environment it replaces
may be absent or broken while recovery is running.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from email.parser import Parser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SCHEMA = 1
UNITS = (
    "district-dashboard.service",
    "district-metrics.service",
    "district-metrics.timer",
    "district-apply.service",
    "district-apply.timer",
)
TIMERS = ("district-metrics.timer", "district-apply.timer")
ONESHOTS = ("district-metrics.service", "district-apply.service")
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}")
COMMAND_TIMEOUT = 1200
SERVICE_WAIT = 300
HEALTH_WAIT = 30


class TransactionError(RuntimeError):
    pass


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise TransactionError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise TransactionError(f"unsupported transaction metadata in {path}")
    return data


def append_log(path: Path, text: str) -> None:
    with path.open("a") as stream:
        stream.write(text.rstrip() + "\n")


def run(argv: list[str], *, timeout: float, env: dict[str, str] | None = None,
        check: bool = True, log: Path | None = None) -> subprocess.CompletedProcess:
    with subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
    result = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    if log:
        append_log(log, f"$ {shlex.join(argv)}\n{stdout}{stderr}")
    if check and result.returncode:
        detail = (stderr or stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise TransactionError(f"{Path(argv[0]).name} exited {result.returncode}{suffix}")
    return result


def _official(url: str) -> bool:
    if url.startswith("git+"):
        url = url[4:]
    if re.fullmatch(r"git@github\.com:mikeroysoft/district(?:\.git)?", url, re.IGNORECASE):
        return True
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    path = parsed.path.rstrip("/").removesuffix(".git")
    return (
        parsed.hostname == "github.com"
        and port is None
        and parsed.scheme in ("https", "ssh")
        and (parsed.username in (None, "git"))
        and path.lower() == "/mikeroysoft/district"
        and not parsed.fragment
    )


def _git(source: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        raise TransactionError("git is required to verify the existing local District snapshot")
    proc = run([git, "-C", str(source), *args], timeout=20, check=False)
    if proc.returncode:
        raise TransactionError("existing District source is not a valid Git snapshot")
    return proc.stdout.strip()


def _matching_package(source: Path, installed: Path) -> bool:
    def files(root: Path) -> dict[Path, Path]:
        if not root.is_dir() or root.is_symlink():
            return {}
        result = {}
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                return {}
            if path.is_file():
                result[relative] = path
        return result

    source_files, installed_files = files(source), files(installed)
    return (
        bool(source_files)
        and source_files.keys() == installed_files.keys()
        and all(source_files[name].read_bytes() == installed_files[name].read_bytes() for name in source_files)
    )


def _local_identity(direct: dict, requirement: dict, package: Path) -> tuple[str, str]:
    url = direct.get("url")
    parsed = urlparse(url) if isinstance(url, str) else None
    directory = requirement.get("directory")
    if (
        parsed is None
        or parsed.scheme != "file"
        or parsed.netloc
        or not isinstance(directory, str)
    ):
        raise TransactionError("District must be a non-editable install from the official Git repository")
    raw_source = Path(unquote(parsed.path))
    receipt_source = Path(directory)
    if (
        not raw_source.is_absolute()
        or not receipt_source.is_absolute()
        or raw_source.is_symlink()
        or not raw_source.is_dir()
        or raw_source.resolve() != receipt_source.resolve()
    ):
        raise TransactionError("District local source receipt is invalid")
    source = raw_source.resolve()
    if _git(source, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TransactionError("existing District source snapshot has local changes")
    origin = _git(source, "remote", "get-url", "origin")
    if not _official(origin) or "?" in origin:
        raise TransactionError("existing District source snapshot has a foreign Git origin")
    sha = _git(source, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if not SHA.fullmatch(sha):
        raise TransactionError("existing District source snapshot has an invalid commit")
    if not _matching_package(source / "district", package):
        raise TransactionError("installed District package does not match its source commit")
    return sha, url


def installed_identity(tool: Path) -> dict:
    if not tool.is_dir() or tool.is_symlink():
        raise TransactionError(f"unsupported District tool environment: {tool}")
    direct_files = list(tool.glob("lib/python*/site-packages/district-*.dist-info/direct_url.json"))
    metadata_files = list(tool.glob("lib/python*/site-packages/district-*.dist-info/METADATA"))
    if len(direct_files) != 1 or len(metadata_files) != 1 or direct_files[0].parent != metadata_files[0].parent:
        raise TransactionError("District installation metadata is missing or ambiguous")
    try:
        direct = json.loads(direct_files[0].read_text())
        metadata = Parser().parsestr(metadata_files[0].read_text())
        receipt = tomllib.loads((tool / "uv-receipt.toml").read_text())
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise TransactionError(f"invalid District installation metadata: {exc}") from exc
    directory = direct.get("dir_info") if isinstance(direct, dict) else None
    if not isinstance(direct, dict) or (directory is not None and (not isinstance(directory, dict) or directory.get("editable") is True)):
        raise TransactionError("District must be a non-editable install from the official Git repository")
    version = metadata.get("Version", "")
    if not VERSION.fullmatch(version) or metadata.get("Name", "").lower() != "district":
        raise TransactionError("District installation identity is invalid")
    receipt_tool = receipt.get("tool")
    requirements = receipt_tool.get("requirements") if isinstance(receipt_tool, dict) else None
    if (
        not isinstance(requirements, list)
        or len(requirements) != 1
        or not isinstance(requirements[0], dict)
        or requirements[0].get("name") != "district"
    ):
        raise TransactionError("District uv receipt is missing or ambiguous")
    requirement = requirements[0]
    vcs = direct.get("vcs_info")
    if isinstance(vcs, dict):
        if vcs.get("vcs") != "git" or not isinstance(direct.get("url"), str) or not _official(direct["url"]):
            raise TransactionError("District must be a non-editable install from the official Git repository")
        sha = str(vcs.get("commit_id", "")).lower()
        source = requirement.get("git")
        if not SHA.fullmatch(sha) or not isinstance(source, str) or not _official(source):
            raise TransactionError("District uv receipt is not pinned to the official Git repository")
        revisions = parse_qs(urlparse(source.removeprefix("git+")).query).get("rev", [])
        if revisions != [sha]:
            raise TransactionError("District uv receipt and installed commit do not match")
    else:
        sha, source = _local_identity(direct, requirement, direct_files[0].parent.parent / "district")
    return {"sha": sha, "version": version, "source": source}


def current_identity(tool: Path, entrypoint: Path, root: Path) -> dict:
    """Trust recorded provenance only while the installed artifact remains byte-identical."""
    path = root / "state.json"
    if path.exists():
        state = read_json(path)
        if state.get("schema") != SCHEMA:
            raise TransactionError("unsupported District update metadata schema")
        digest = state.get("current_digest")
        if digest:
            if digest != artifact_digest(tool, entrypoint):
                raise TransactionError("installed District changed since the recorded update")
            return state["current"]
    return installed_identity(tool)


def cli_check(tool: Path, expected: dict, log: Path | None = None) -> None:
    python = tool / "bin" / "python3"
    if not python.is_file():
        raise TransactionError(f"District interpreter is missing: {python}")
    proc = run([str(python), "-I", "-m", "district", "--version"], timeout=20, log=log)
    if proc.stdout.strip() != expected["version"]:
        raise TransactionError("District CLI version does not match installed metadata")


def validate_request(data: dict, root: Path) -> dict:
    if data.get("schema") != SCHEMA or data.get("action") not in ("update", "rollback"):
        raise TransactionError("unsupported transaction request")
    root = root.resolve()
    if Path(data.get("state_root", "")).resolve() != root:
        raise TransactionError("transaction state root mismatch")
    if not Path(data.get("lock", "")).is_absolute():
        raise TransactionError("invalid District operation lock path")
    tool = Path(data.get("tool", ""))
    entrypoint = Path(data.get("entrypoint", ""))
    if not tool.is_absolute() or tool.name != "district":
        raise TransactionError("unsafe uv tool path")
    if not entrypoint.is_absolute() or entrypoint.name != "district":
        raise TransactionError("unsafe District entrypoint path")
    for key in ("systemctl", "uv"):
        value = Path(data.get(key, ""))
        if not value.is_absolute() or not value.is_file() or not os.access(value, os.X_OK):
            raise TransactionError(f"invalid {key} executable")
    current = data.get("current")
    target = data.get("target")
    if not isinstance(current, dict) or not SHA.fullmatch(str(current.get("sha", ""))):
        raise TransactionError("invalid current identity")
    if not isinstance(target, dict) or not SHA.fullmatch(str(target.get("sha", ""))):
        raise TransactionError("invalid target identity")
    if data["action"] == "update":
        source = data.get("install_source")
        expected = f"git+https://github.com/mikeroySoft/district.git@{target['sha']}"
        if source != expected:
            raise TransactionError("unsafe District install source")
    if data["action"] == "rollback":
        artifact = Path(data.get("target_artifact", ""))
        if root not in artifact.resolve().parents or artifact.parent.name != "artifacts":
            raise TransactionError("unsafe rollback artifact path")
    data["state_root"] = str(root)
    data["tool"] = str(tool)
    data["entrypoint"] = str(entrypoint)
    return data


def systemctl(req: dict, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run([req["systemctl"], "--user", *args], timeout=15, check=check, log=Path(req["log"]))


def property_value(req: dict, unit: str, prop: str) -> str:
    proc = systemctl(req, "show", unit, f"--property={prop}", "--value", "--no-pager", check=False)
    if proc.returncode:
        raise TransactionError(f"cannot query {unit} {prop}")
    return proc.stdout.strip()


def capture_services(req: dict) -> dict:
    result = {}
    for unit in UNITS:
        load = property_value(req, unit, "LoadState")
        if load not in ("loaded", "not-found"):
            raise TransactionError(f"unsupported {unit} load state: {load or 'empty'}")
        result[unit] = {
            "loaded": load == "loaded",
            "active_state": property_value(req, unit, "ActiveState") if load == "loaded" else "inactive",
            "unit_file_state": property_value(req, unit, "UnitFileState") if load == "loaded" else "not-found",
            "main_pid": property_value(req, unit, "MainPID") if load == "loaded" else "0",
        }
        if result[unit]["active_state"] not in ("active", "inactive", "failed", "activating", "deactivating"):
            raise TransactionError(f"unsupported {unit} active state: {result[unit]['active_state'] or 'empty'}")
        if unit in (*TIMERS, "district-dashboard.service") and result[unit]["active_state"] in ("activating", "deactivating"):
            raise TransactionError(f"{unit} is changing state; retry after it settles")
    dashboard = result["district-dashboard.service"]
    if dashboard["loaded"]:
        dashboard["exec_start"] = property_value(req, "district-dashboard.service", "ExecStart")
    return result


def stop_services(req: dict, states: dict) -> None:
    active_timers = [unit for unit in TIMERS if states[unit]["loaded"] and states[unit]["active_state"] == "active"]
    if active_timers:
        systemctl(req, "stop", *active_timers)
    deadline = time.monotonic() + SERVICE_WAIT
    while True:
        busy = [unit for unit in ONESHOTS if states[unit]["loaded"] and property_value(req, unit, "ActiveState") in
                ("active", "activating", "deactivating")]
        if not busy:
            break
        if time.monotonic() >= deadline:
            raise TransactionError(f"District oneshot still running after {SERVICE_WAIT}s: {', '.join(busy)}")
        time.sleep(1)
    dashboard = "district-dashboard.service"
    if states[dashboard]["loaded"] and states[dashboard]["active_state"] == "active":
        systemctl(req, "stop", dashboard)


def restore_services(req: dict, states: dict) -> None:
    dashboard_name = "district-dashboard.service"
    if states[dashboard_name]["loaded"] and states[dashboard_name]["active_state"] == "active":
        systemctl(req, "start", dashboard_name)
    active_timers = [unit for unit in TIMERS if states[unit]["loaded"] and states[unit]["active_state"] == "active"]
    if active_timers:
        systemctl(req, "start", *active_timers)
    for unit in (*TIMERS, dashboard_name):
        if not states[unit]["loaded"]:
            continue
        want = states[unit]["active_state"] == "active"
        have = property_value(req, unit, "ActiveState") == "active"
        if want != have:
            raise TransactionError(f"failed to restore {unit} active state")
        if property_value(req, unit, "UnitFileState") != states[unit]["unit_file_state"]:
            raise TransactionError(f"{unit} enablement changed during update")
    for unit in ONESHOTS:
        if (
            states[unit]["loaded"]
            and property_value(req, unit, "UnitFileState") != states[unit]["unit_file_state"]
        ):
            raise TransactionError(f"{unit} enablement changed during update")
    dashboard = states[dashboard_name]
    if dashboard["loaded"] and property_value(req, dashboard_name, "ExecStart") != dashboard["exec_start"]:
        raise TransactionError("district-dashboard.service ExecStart changed during update")


def dashboard_argv(req: dict) -> tuple[list[str], str, int]:
    raw = property_value(req, "district-dashboard.service", "ExecStart")
    match = re.search(r"argv\[]=(.*?)\s+;\s+ignore_errors=", raw)
    if not match:
        raise TransactionError("cannot parse district-dashboard.service ExecStart")
    try:
        argv = shlex.split(match.group(1))
    except ValueError as exc:
        raise TransactionError("cannot parse district-dashboard.service argv") from exc
    prefix = [str(Path(req["tool"]) / "bin" / "python3"), "-m", "district", "dashboard"]
    if argv[:4] != prefix:
        raise TransactionError("district-dashboard.service does not use the managed District environment")
    host, port = "127.0.0.1", 8760
    for option, cast in (("--host", str), ("--port", int)):
        indexes = [i for i, value in enumerate(argv) if value == option]
        if len(indexes) > 1 or (indexes and indexes[0] + 1 >= len(argv)):
            raise TransactionError(f"invalid {option} in district-dashboard.service")
        if indexes:
            try:
                value = cast(argv[indexes[0] + 1])
            except ValueError as exc:
                raise TransactionError(f"invalid {option} in district-dashboard.service") from exc
            if option == "--host":
                host = value
            else:
                port = value
    if host not in ("0.0.0.0", "127.0.0.1", "::", "::1") or not 1 <= port <= 65535:
        raise TransactionError("unsupported District dashboard bind")
    return argv, host, port


def _fleet(port: int, ipv6: bool = False) -> tuple[int, int]:
    address = "::1" if ipv6 else "127.0.0.1"
    authority = f"[{address}]:{port}" if ipv6 else f"{address}:{port}"
    conn = http.client.HTTPConnection(address, port, timeout=2)
    try:
        conn.request("GET", "/api/fleet", headers={"Host": authority})
        response = conn.getresponse()
        body = response.read(2_000_000)
    finally:
        conn.close()
    if response.status != 200:
        raise TransactionError(f"District dashboard returned HTTP {response.status}")
    data = json.loads(body)
    revision, fleet = data.get("revision"), data.get("fleet")
    if type(revision) is not int or not isinstance(fleet, dict):
        raise TransactionError("District dashboard returned an invalid fleet observation")
    return revision, len(fleet)


def verify_dashboard(req: dict, states: dict, expected: dict, *, require_replaced: bool = True) -> dict | None:
    dashboard = "district-dashboard.service"
    if not (states[dashboard]["loaded"] and states[dashboard]["active_state"] == "active"):
        return None
    argv, host, port = dashboard_argv(req)
    old_pid = int(states[dashboard]["main_pid"] or 0)
    deadline = time.monotonic() + HEALTH_WAIT
    first = None
    last_error = "dashboard did not start"
    while time.monotonic() < deadline:
        try:
            if property_value(req, dashboard, "ActiveState") != "active":
                raise TransactionError("dashboard service is not active")
            pid = int(property_value(req, dashboard, "MainPID"))
            if pid <= 0 or (require_replaced and pid == old_pid):
                raise TransactionError("dashboard process was not replaced")
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            process_argv = [part.decode(errors="replace") for part in cmdline if part]
            if process_argv[:4] != argv[:4]:
                raise TransactionError("running dashboard command does not use the installed District")
            observed = _fleet(port, host in ("::", "::1"))
            if observed[1] == 0 or (first is not None and observed[0] > first[0]):
                return {"pid": pid, "revision": observed[0], "factories": observed[1], "port": port, "host": host}
            first = observed if first is None else first
        except (OSError, ValueError, json.JSONDecodeError, TransactionError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise TransactionError(f"District dashboard/collector verification failed: {last_error}")


def entrypoint_check(tool: Path, entrypoint: Path) -> None:
    if not entrypoint.is_file():
        raise TransactionError(f"District entrypoint is missing: {entrypoint}")
    if entrypoint.is_symlink() and entrypoint.resolve() != (tool / "bin" / "district").resolve():
        raise TransactionError("District entrypoint symlink targets another environment")
    try:
        shebang = entrypoint.open("rb").readline().decode().strip()
    except (OSError, UnicodeError) as exc:
        raise TransactionError(f"cannot inspect District entrypoint: {exc}") from exc
    if shebang not in {f"#!{tool / 'bin' / name}" for name in ("python", "python3")}:
        raise TransactionError("District entrypoint uses another interpreter")


def copy_entrypoint(source: Path, target: Path) -> None:
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_file():
        shutil.copy2(source, target)
    else:
        raise TransactionError(f"District entrypoint is missing: {source}")


def artifact_digest(tool: Path, entrypoint: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(tool.rglob("*"), key=lambda item: item.relative_to(tool).as_posix()):
        relative_path = path.relative_to(tool)
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        relative = relative_path.as_posix().encode()
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink():
            payload = b"L" + os.readlink(path).encode()
        elif path.is_dir():
            payload = b"D"
        elif path.is_file():
            payload = b"F" + path.read_bytes()
        else:
            raise TransactionError(f"unsupported file in District artifact: {path}")
        digest.update(relative + b"\0" + str(mode).encode() + b"\0" + payload + b"\0")
    if entrypoint.is_symlink():
        entry_payload = b"L" + os.readlink(entrypoint).encode()
    elif entrypoint.is_file():
        entry_payload = b"F" + entrypoint.read_bytes()
    else:
        raise TransactionError(f"District entrypoint is missing: {entrypoint}")
    digest.update(b"entrypoint\0" + entry_payload)
    return digest.hexdigest()


def copy_artifact(req: dict, identity: dict) -> Path:
    root = Path(req["state_root"])
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    name = f"{identity['sha']}-{uuid.uuid4().hex[:12]}"
    temp, final = artifacts / f".{name}.tmp", artifacts / name
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir()
    shutil.copytree(req["tool"], temp / "tool", symlinks=True)
    copy_entrypoint(Path(req["entrypoint"]), temp / "entrypoint")
    checksum = artifact_digest(temp / "tool", temp / "entrypoint")
    atomic_json(temp / "manifest.json", {"schema": SCHEMA, "identity": identity, "sha256": checksum})
    os.replace(temp, final)
    return final


def artifact_identity(path: Path) -> dict:
    manifest = read_json(path / "manifest.json")
    identity = manifest.get("identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"sha", "version", "source"}
        or not SHA.fullmatch(str(identity.get("sha", "")))
        or not VERSION.fullmatch(str(identity.get("version", "")))
        or not isinstance(identity.get("source"), str)
        or not _official(identity["source"]) and not identity["source"].startswith("file:///")
        or manifest.get("sha256") != artifact_digest(path / "tool", path / "entrypoint")
    ):
        raise TransactionError("rollback artifact identity does not match its manifest")
    return identity


def artifact_matches_live(req: dict, artifact: Path, expected: dict) -> bool:
    return (
        artifact_identity(artifact) == expected
        and artifact_digest(Path(req["tool"]), Path(req["entrypoint"]))
        == read_json(artifact / "manifest.json")["sha256"]
    )


def restore_artifact(req: dict, artifact: Path, expected: dict) -> None:
    if artifact_identity(artifact) != expected:
        raise TransactionError("rollback artifact is not the expected installation")
    live = Path(req["tool"])
    entrypoint = Path(req["entrypoint"])
    incoming = live.parent / ".district-update-incoming"
    displaced = live.parent / ".district-update-displaced"
    entry_temp = entrypoint.with_name(f".{entrypoint.name}.district-update.tmp")
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.rmtree(displaced, ignore_errors=True)
    entry_temp.unlink(missing_ok=True)
    shutil.copytree(artifact / "tool", incoming, symlinks=True)
    copy_entrypoint(artifact / "entrypoint", entry_temp)
    if live.exists():
        os.replace(live, displaced)
    os.replace(incoming, live)
    os.replace(entry_temp, entrypoint)
    shutil.rmtree(displaced, ignore_errors=True)
    if artifact_digest(live, entrypoint) != read_json(artifact / "manifest.json")["sha256"]:
        raise TransactionError("restored District installation does not match its artifact")
    entrypoint_check(live, entrypoint)


def state_record(current: dict, previous: dict | None, digest: str) -> dict:
    return {"schema": SCHEMA, "current": current, "previous": previous, "current_digest": digest}


def write_outcome(root: Path, result: dict) -> None:
    atomic_json(root / "last-result.json", {"schema": SCHEMA, **result})


def install_target(req: dict) -> None:
    env = dict(os.environ)
    env.update(
        UV_TOOL_DIR=str(Path(req["tool"]).parent),
        UV_TOOL_BIN_DIR=str(Path(req["entrypoint"]).parent),
    )
    argv = [
        req["uv"], "tool", "install", "--offline", "--reinstall", "--force", "--no-config",
        "--no-progress", "--color", "never", "--link-mode", "copy", "--python", req["python_version"],
        "--no-python-downloads", req["install_source"],
    ]
    run(argv, timeout=COMMAND_TIMEOUT, env=env, log=Path(req["log"]))


def _remove_artifact(path: str | None, root: Path, keep: Path | None = None) -> None:
    if not path:
        return
    candidate = Path(path)
    if candidate != keep and candidate.parent == root / "artifacts":
        shutil.rmtree(candidate, ignore_errors=True)


def _remove_stage(req: dict, root: Path) -> None:
    value = req.get("stage")
    if not isinstance(value, str) or not value:
        return
    stage = Path(value)
    if root in stage.resolve().parents:
        shutil.rmtree(stage, ignore_errors=True)


def recover(req: dict, *, interrupted: bool = False) -> dict:
    root = Path(req["state_root"])
    journal_path = root / "transaction.json"
    journal = read_json(journal_path)
    req = validate_request(journal["request"], root)
    req["log"] = str(root / "transaction.log")
    backup = Path(journal["backup"])
    expected = journal["current"]
    states = journal["services"]
    phase = journal.get("phase")
    state_before = journal.get("state_before")
    if phase not in ("backed-up", "services-stopped", "installing", "restoring-previous"):
        raise TransactionError("interrupted transaction has an invalid phase")
    if (
        not isinstance(state_before, dict)
        or state_before.get("schema") != SCHEMA
        or state_before.get("current") != expected
    ):
        raise TransactionError("interrupted transaction has invalid prior update metadata")
    try:
        if phase == "backed-up":
            if not artifact_matches_live(req, backup, expected):
                raise TransactionError("installed District changed before recovery")
            cli_check(Path(req["tool"]), expected, Path(req["log"]))
            restore_services(req, states)
            dashboard = verify_dashboard(req, states, expected, require_replaced=False)
        else:
            stop_services(req, states)
            restore_artifact(req, backup, expected)
            cli_check(Path(req["tool"]), expected, Path(req["log"]))
            restore_services(req, states)
            dashboard = verify_dashboard(req, states, expected)
        entrypoint_check(Path(req["tool"]), Path(req["entrypoint"]))
        atomic_json(root / "state.json", state_before)
        result = {
            "ok": False,
            "status": "recovered" if interrupted else "rolled-back",
            "current": expected,
            "target": req["target"],
            "rollback": {"ok": True, "restored": expected},
            "dashboard": dashboard,
            "log": req["log"],
        }
        write_outcome(root, result)
        journal_path.unlink(missing_ok=True)
        keep = Path(req["previous_artifact"]) if req.get("previous_artifact") else None
        _remove_artifact(str(backup), root, keep)
        entrypoint_check(Path(req["tool"]), Path(req["entrypoint"]))
        _remove_stage(req, root)
        return result
    except BaseException as exc:
        result = {
            "ok": False,
            "status": "recovery-failed",
            "current": expected,
            "target": req.get("target"),
            "error": str(exc),
            "rollback": {"ok": False, "error": str(exc)},
            "log": req["log"],
        }
        write_outcome(root, result)
        return result


def transact(request_path: Path) -> dict:
    request = read_json(request_path)
    root = Path(request["state_root"]).resolve()
    req = validate_request(request, root)
    log = root / "transaction.log"
    req["log"] = str(log)
    current = current_identity(Path(req["tool"]), Path(req["entrypoint"]), root)
    if current != req["current"]:
        raise TransactionError("installed District changed after preflight")
    cli_check(Path(req["tool"]), current, log)
    entrypoint_check(Path(req["tool"]), Path(req["entrypoint"]))
    states = capture_services(req)
    backup = copy_artifact(req, current)
    state_path = root / "state.json"
    if state_path.exists():
        state_before = read_json(state_path)
        if state_before.get("current") != current:
            raise TransactionError("installed District does not match prior update metadata")
    else:
        state_before = state_record(current, None, artifact_digest(Path(req["tool"]), Path(req["entrypoint"])))
        atomic_json(state_path, state_before)
    journal = {
        "schema": SCHEMA,
        "phase": "backed-up",
        "request": req,
        "current": current,
        "backup": str(backup),
        "services": states,
        "state_before": state_before,
    }
    atomic_json(root / "transaction.json", journal)
    try:
        stop_services(req, states)
        journal["phase"] = "services-stopped"
        atomic_json(root / "transaction.json", journal)
        if req["action"] == "update":
            journal["phase"] = "installing"
            atomic_json(root / "transaction.json", journal)
            install_target(req)
        else:
            journal["phase"] = "restoring-previous"
            atomic_json(root / "transaction.json", journal)
            restore_artifact(req, Path(req["target_artifact"]), req["target"])
        target = (
            installed_identity(Path(req["tool"]))
            if req["action"] == "update"
            else artifact_identity(Path(req["target_artifact"]))
        )
        if target != req["target"]:
            raise TransactionError("installed target identity does not match the update plan")
        entrypoint_check(Path(req["tool"]), Path(req["entrypoint"]))
        cli_check(Path(req["tool"]), target, log)
        restore_services(req, states)
        dashboard = verify_dashboard(req, states, target)
        previous = {**current, "artifact": str(backup)}
        atomic_json(state_path, state_record(target, previous, artifact_digest(Path(req["tool"]), Path(req["entrypoint"]))))
        result = {
            "ok": True,
            "status": "updated" if req["action"] == "update" else "rolled-back",
            "current": target,
            "previous": previous,
            "dashboard": dashboard,
            "services": states,
            "log": str(log),
        }
        write_outcome(root, result)
        (root / "transaction.json").unlink(missing_ok=True)
        _remove_artifact(req.get("previous_artifact"), root, backup)
        if req["action"] == "rollback":
            _remove_artifact(req.get("target_artifact"), root, backup)
        _remove_stage(req, root)
        return result
    except BaseException as exc:
        append_log(log, f"transaction failed: {exc}")
        rolled_back = recover(req)
        rolled_back["error"] = str(exc)
        rolled_back["status"] = "rolled-back" if rolled_back.get("rollback", {}).get("ok") else "recovery-failed"
        write_outcome(root, rolled_back)
        return rolled_back


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("run", "recover"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    def interrupted(signum: int, _frame: object) -> None:
        raise InterruptedError(f"interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        root = args.path.resolve() if args.mode == "recover" else args.path.resolve().parent
        request = read_json(root / "transaction.json")["request"] if args.mode == "recover" else read_json(args.path)
        request = validate_request(request, root)
        lock_path = Path(request["lock"])
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        inherited = os.environ.get("DISTRICT_UPDATE_LOCK_FD")
        lock_fd = int(inherited) if inherited else os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        held, wanted = os.fstat(lock_fd), lock_path.stat()
        if (held.st_dev, held.st_ino) != (wanted.st_dev, wanted.st_ino):
            raise TransactionError("inherited operation lock does not match the transaction")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another District apply, metrics, or update operation is running ({lock_path})", file=sys.stderr)
            return 2
        if args.mode == "run":
            result = transact(args.path)
        else:
            result = recover({"state_root": str(root)}, interrupted=True)
    except BaseException as exc:
        root = args.path.resolve() if args.mode == "recover" else args.path.resolve().parent
        with contextlib.suppress(Exception):
            write_outcome(root, {"ok": False, "status": "error", "error": str(exc)})
        return 2
    return 0 if result.get("ok") or result.get("rollback", {}).get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
