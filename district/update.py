"""`district update [--dry-run] [--to REF] [--rollback] [--yes] [--json]`.

Updates only District's official uv-managed tool installation.  Factory and all
unit files are outside this command's write set.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from district import host
from district import update_transaction as transaction

REPOSITORY = "mikeroySoft/district"
WORKFLOW = "ci.yml"
REQUIRED_JOB = "test"
SHA = re.compile(r"[0-9a-f]{40}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
QUERY_TIMEOUT = 30
PREPARE_TIMEOUT = 1200
TRANSACTION_TIMEOUT = 2400


class UpdateError(RuntimeError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UpdateError(message)


def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "district" / "update"


def executable(name: str) -> Path:
    value = shutil.which(name)
    if not value:
        raise UpdateError(f"required executable not found: {name}")
    path = Path(value).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise UpdateError(f"required executable is not executable: {path}")
    return path


def external_python(tool: Path) -> Path:
    candidates = (Path("/usr/bin/python3"), Path("/bin/python3"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK) and tool not in candidate.resolve().parents:
            proc = subprocess.run(
                [str(candidate), "-c", "import sys; raise SystemExit(sys.version_info < (3, 11))"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                return candidate
    raise UpdateError("an external Python 3.11+ interpreter is required for safe recovery")


def command(argv: list[str], *, timeout: float = QUERY_TIMEOUT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return host.run(argv, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"`{Path(argv[0]).name}` failed: {exc}") from exc


def output(argv: list[str], *, timeout: float = QUERY_TIMEOUT) -> str:
    proc = command(argv, timeout=timeout)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise UpdateError(f"`{Path(argv[0]).name}` exited {proc.returncode}{suffix}")
    return proc.stdout.strip()


def gh_json(gh: Path, endpoint: str, *fields: str) -> dict:
    argv = [str(gh), "api", "--method", "GET", endpoint]
    for field in fields:
        argv.extend(("-f", field))
    env = dict(os.environ)
    env.update(GH_PROMPT_DISABLED="1", GH_PAGER="cat", GH_HOST="github.com")
    proc = command(argv, env=env)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise UpdateError(f"GitHub query failed{suffix}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise UpdateError("GitHub returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("GitHub returned an invalid response")
    return data


def valid_ref(value: str) -> str:
    if (
        not REF.fullmatch(value)
        or value.startswith("-")
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(part.startswith(".") for part in value.split("/"))
    ):
        raise UpdateError(f"invalid official repository ref: {value!r}")
    return value


def resolve_target(gh: Path, ref: str) -> str:
    data = gh_json(gh, f"repos/{REPOSITORY}/commits/{quote(valid_ref(ref), safe='')}")
    sha = str(data.get("sha", "")).lower()
    if not SHA.fullmatch(sha):
        raise UpdateError(f"GitHub did not resolve {ref!r} to an immutable commit")
    return sha


def verify_local_snapshot_commit(gh: Path, current: dict) -> None:
    if not current["source"].startswith("file:"):
        return
    resolved = resolve_target(gh, current["sha"])
    if resolved != current["sha"]:
        raise UpdateError("existing local District snapshot commit is not available in the official repository")


def target_version(gh: Path, sha: str) -> str:
    data = gh_json(gh, f"repos/{REPOSITORY}/contents/pyproject.toml", f"ref={sha}")
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        raise UpdateError("target pyproject.toml is unavailable")
    try:
        document = tomllib.loads(base64.b64decode(data["content"], validate=False).decode())
        project = document["project"]
        name, version = project["name"], project["version"]
    except (ValueError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise UpdateError("target pyproject.toml is invalid") from exc
    if name != "district" or not isinstance(version, str) or not transaction.VERSION.fullmatch(version):
        raise UpdateError("target package identity is invalid")
    return str(version)


def ci_status(gh: Path, sha: str) -> dict:
    data = gh_json(
        gh,
        f"repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs",
        f"head_sha={sha}",
        "per_page=100",
    )
    raw_runs = data.get("workflow_runs")
    runs = [
        run for run in raw_runs if isinstance(run, dict) and run.get("head_sha") == sha
    ] if isinstance(raw_runs, list) else []
    if not runs:
        return {"eligible": False, "status": "missing", "workflow": WORKFLOW, "job": REQUIRED_JOB}
    selected = max(runs, key=lambda run: run.get("id") if type(run.get("id")) is int else -1)
    run_id = selected.get("id")
    if type(run_id) is not int or run_id <= 0:
        return {"eligible": False, "status": "missing", "workflow": WORKFLOW, "job": REQUIRED_JOB}
    summary = {"workflow": WORKFLOW, "job": REQUIRED_JOB, "run_id": run_id}
    if selected.get("status") != "completed":
        return {**summary, "eligible": False, "status": "pending"}
    if selected.get("conclusion") != "success":
        return {**summary, "eligible": False, "status": "failed", "conclusion": selected.get("conclusion")}
    jobs_data = gh_json(gh, f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs", "filter=latest", "per_page=100")
    raw_jobs = jobs_data.get("jobs")
    jobs = [
        job for job in raw_jobs if isinstance(job, dict) and job.get("name") == REQUIRED_JOB
    ] if isinstance(raw_jobs, list) else []
    if not jobs:
        return {**summary, "eligible": False, "status": "missing"}
    job = max(jobs, key=lambda item: item.get("id") if type(item.get("id")) is int else -1)
    if job.get("status") != "completed":
        return {**summary, "eligible": False, "status": "pending"}
    if job.get("conclusion") != "success":
        return {**summary, "eligible": False, "status": "failed", "conclusion": job.get("conclusion")}
    return {**summary, "eligible": True, "status": "success", "job_id": job.get("id")}


def uv_paths(uv: Path) -> tuple[Path, Path]:
    tool_dir = Path(output([str(uv), "tool", "dir"]))
    bin_dir = Path(output([str(uv), "tool", "dir", "--bin"]))
    if not tool_dir.is_absolute() or not bin_dir.is_absolute():
        raise UpdateError("uv returned non-absolute tool directories")
    tool, entrypoint = tool_dir / "district", bin_dir / "district"
    try:
        transaction.entrypoint_check(tool, entrypoint)
    except transaction.TransactionError as exc:
        raise UpdateError(str(exc)) from exc
    return tool, entrypoint


def python_version(tool: Path) -> str:
    try:
        config = dict(
            (key.strip(), value.strip())
            for line in (tool / "pyvenv.cfg").read_text().splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        )
        version = config["version_info"]
    except (OSError, KeyError, ValueError) as exc:
        raise UpdateError("cannot determine the installed District Python version") from exc
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        raise UpdateError("installed District Python version is invalid")
    return version


def unit_property(systemctl: Path, unit: str, prop: str) -> str:
    proc = command([str(systemctl), "--user", "show", unit, f"--property={prop}", "--value", "--no-pager"])
    if proc.returncode:
        raise UpdateError(f"cannot query {unit} {prop}")
    return proc.stdout.strip()


def service_plan(systemctl: Path, tool: Path) -> dict:
    result = {}
    for unit in transaction.UNITS:
        load = unit_property(systemctl, unit, "LoadState")
        if load not in ("loaded", "not-found"):
            raise UpdateError(f"unsupported {unit} load state: {load or 'empty'}")
        active = unit_property(systemctl, unit, "ActiveState") if load == "loaded" else "inactive"
        if active not in ("active", "inactive", "failed", "activating", "deactivating"):
            raise UpdateError(f"unsupported {unit} active state: {active or 'empty'}")
        if unit in (*transaction.TIMERS, "district-dashboard.service") and active in ("activating", "deactivating"):
            raise UpdateError(f"{unit} is changing state; retry after it settles")
        result[unit] = {
            "loaded": load == "loaded",
            "active_state": active,
            "unit_file_state": unit_property(systemctl, unit, "UnitFileState") if load == "loaded" else "not-found",
        }
    dashboard = result["district-dashboard.service"]
    if dashboard["loaded"]:
        raw = unit_property(systemctl, "district-dashboard.service", "ExecStart")
        expected = f"argv[]={tool / 'bin' / 'python3'} -m district dashboard "
        if expected not in raw:
            raise UpdateError("district-dashboard.service does not target the uv-managed District environment")
    return result


def load_state(root: Path, current: dict, *, require_previous: bool = False) -> dict | None:
    path = root / "state.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise UpdateError(f"cannot read update metadata: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != transaction.SCHEMA:
        raise UpdateError("unsupported District update metadata schema")
    if data.get("current") != current:
        raise UpdateError("installed District identity does not match update metadata")
    previous = data.get("previous")
    if previous is not None:
        if not isinstance(previous, dict) or not isinstance(previous.get("artifact"), str):
            raise UpdateError("invalid recorded rollback installation")
        artifact = Path(previous["artifact"])
        try:
            identity = transaction.artifact_identity(artifact)
        except transaction.TransactionError as exc:
            if require_previous:
                raise UpdateError(str(exc)) from exc
            return {**data, "previous": None, "warning": f"Previous rollback artifact is unavailable: {exc}"}
        expected = {key: previous[key] for key in ("sha", "version", "source")}
        if identity != expected:
            raise UpdateError("recorded rollback identity does not match its artifact")
    return data


def identity(tool: Path) -> dict:
    try:
        return transaction.installed_identity(tool)
    except transaction.TransactionError as exc:
        raise UpdateError(str(exc)) from exc


def operations(mode: str, services: dict) -> list[str]:
    result = []
    if mode == "update":
        result.append("prepare the exact target with uv before downtime")
    else:
        result.append("restore the recorded installation without network access")
    timers = [unit for unit in transaction.TIMERS if services[unit]["active_state"] == "active"]
    if timers:
        result.append(f"stop then restore active timers without disabling them: {', '.join(timers)}")
    result.append("wait for loaded District metrics/apply oneshots to finish")
    if services["district-dashboard.service"]["active_state"] == "active":
        result.append("restart the running District dashboard with its unchanged unit and bind")
    result.extend((
        "verify installed provenance and CLI startup",
        "verify the restarted dashboard process, HTTP response, and collector publication when it was running",
        "atomically record current/previous installation metadata and retain an offline rollback artifact",
    ))
    return result


def make_plan(args: argparse.Namespace) -> tuple[dict, dict]:
    uv, systemctl = executable("uv"), executable("systemctl")
    tool, entrypoint = uv_paths(uv)
    root = state_root()
    current = transaction.current_identity(tool, entrypoint, root)
    try:
        transaction.cli_check(tool, current)
    except transaction.TransactionError as exc:
        raise UpdateError(str(exc)) from exc
    state = load_state(root, current, require_previous=args.rollback)
    services = service_plan(systemctl, tool)
    if args.rollback:
        previous = state.get("previous") if state else None
        if not previous:
            raise UpdateError("no previous successful District installation is recorded")
        target = {key: previous[key] for key in ("sha", "version", "source")}
        ci = None
        mode = "rollback"
    else:
        gh = executable("gh")
        verify_local_snapshot_commit(gh, current)
        ref = args.to if args.to is not None else "main"
        sha = resolve_target(gh, ref)
        target = {
            "sha": sha,
            "version": target_version(gh, sha),
            "source": f"https://github.com/{REPOSITORY}.git?rev={sha}",
        }
        ci = ci_status(gh, sha)
        mode = "update"
    plan = {
        "ok": bool(ci is None or ci["eligible"]),
        "warnings": [state["warning"]] if state and state.get("warning") else [],
        "status": "plan",
        "mode": mode,
        "installed": current,
        "target": target,
        "ci": ci,
        "services": services,
        "operations": operations(mode, services),
        "dry_run": args.dry_run,
    }
    context = {
        "root": root,
        "state": state,
        "uv": uv,
        "gh": None if args.rollback else gh,
        "systemctl": systemctl,
        "tool": tool,
        "entrypoint": entrypoint,
        "python": external_python(tool),
        "python_version": python_version(tool),
    }
    return plan, context


def text_plan(plan: dict) -> str:
    installed, target = plan["installed"], plan["target"]
    lines = [
        f"District {plan['mode']}",
        f"  installed: {installed['version']} {installed['sha']}",
        f"  target:    {target['version']} {target['sha']}",
    ]
    if plan["ci"]:
        lines.append(f"  CI:        {plan['ci']['status']} ({WORKFLOW} / {REQUIRED_JOB})")
    lines.append("  operations:")
    lines.extend(f"    - {item}" for item in plan["operations"])
    lines.extend(f"  warning: {item}" for item in plan.get("warnings", []))
    return "\n".join(lines)


def emit(data: dict, json_mode: bool, *, error: bool = False) -> None:
    if json_mode:
        print(json.dumps(data, sort_keys=True))
    elif error:
        print(f"district update: {data['error']}", file=sys.stderr)
    elif data.get("status") in ("plan", "up-to-date", "ineligible"):
        print(text_plan(data))
        if data["status"] == "up-to-date":
            print("Already installed; no changes made.")
        elif data["status"] == "ineligible":
            print("Target is not eligible; no changes made.")
    else:
        current = data.get("current", data.get("installed", {}))
        print(f"District {data.get('status')}: {current.get('version', '?')} {current.get('sha', '?')}")
        if data.get("error"):
            print(f"error: {data['error']}")
        rollback = data.get("rollback")
        if rollback:
            print("Previous installation restored." if rollback.get("ok") else f"Recovery failed: {rollback.get('error')}")
        if data.get("log"):
            print(f"transaction log: {data['log']}")


def confirm(plan: dict, json_mode: bool) -> bool:
    stream = sys.stderr if json_mode else sys.stdout
    print(text_plan(plan), file=stream)
    print("Proceed? [y/N] ", end="", file=stream, flush=True)
    answer = sys.stdin.readline()
    return answer.strip().lower() in ("y", "yes")


@contextmanager
def operation_lock():
    path = host.operation_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError(f"another District apply, metrics, or update operation is running ({path})") from exc
        yield lock.fileno()


def install_helper(root: Path) -> Path:
    source = Path(transaction.__file__)
    target = root / "transaction.py"
    root.mkdir(parents=True, exist_ok=True)
    temp = root / f".transaction.{os.getpid()}.tmp"
    shutil.copy2(source, temp)
    os.replace(temp, target)
    return target


def write_request(path: Path, data: dict) -> None:
    transaction.atomic_json(path, {"schema": transaction.SCHEMA, **data})


def append_prepare_log(root: Path, argv: list[str], proc: subprocess.CompletedProcess) -> None:
    root.mkdir(parents=True, exist_ok=True)
    transaction.append_log(root / "transaction.log", f"$ {shlex.join(argv)}\n{proc.stdout}{proc.stderr}")


def prepare(plan: dict, context: dict) -> Path:
    root, uv = context["root"], context["uv"]
    stage = root / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    env = dict(os.environ)
    env.update(UV_TOOL_DIR=str(stage / "tools"), UV_TOOL_BIN_DIR=str(stage / "bin"),
               GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
    source = f"git+https://github.com/{REPOSITORY}.git@{plan['target']['sha']}"
    argv = [
        str(uv), "tool", "install", "--force", "--no-config", "--no-progress", "--color", "never",
        "--link-mode", "copy", "--python", context["python_version"], "--no-python-downloads", source,
    ]
    proc = command(argv, timeout=PREPARE_TIMEOUT, env=env)
    append_prepare_log(root, argv, proc)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise UpdateError(f"uv could not prepare District {plan['target']['sha']}{suffix}")
    staged_tool = stage / "tools" / "district"
    expected = plan["target"]
    staged = identity(staged_tool)
    if (staged["sha"], staged["version"]) != (expected["sha"], expected["version"]):
        raise UpdateError("prepared District identity does not match the update plan")
    plan["target"] = staged
    try:
        transaction.cli_check(staged_tool, staged, root / "transaction.log")
    except transaction.TransactionError as exc:
        raise UpdateError(str(exc)) from exc
    return stage


def run_helper(python: Path, helper: Path, mode: str, path: Path, root: Path, lock_fd: int) -> tuple[int, dict]:
    result_path = root / "last-result.json"
    result_path.unlink(missing_ok=True)
    with (root / "transaction.log").open("a") as log:
        child = subprocess.Popen(
            [str(python), str(helper), mode, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=(lock_fd,),
            env={**os.environ, "DISTRICT_UPDATE_LOCK_FD": str(lock_fd)},
        )
        try:
            code = child.wait(timeout=TRANSACTION_TIMEOUT)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGINT)
            try:
                code = child.wait(timeout=transaction.SERVICE_WAIT + transaction.HEALTH_WAIT + 60)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    code = child.wait(timeout=60)
                except subprocess.TimeoutExpired as exc:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    raise UpdateError(f"transaction helper did not stop; recover the journal under {root}") from exc
    try:
        result = transaction.read_json(result_path)
    except transaction.TransactionError as exc:
        raise UpdateError(f"transaction helper exited {code} without an authoritative result: {exc}") from exc
    return code, result


def recover_pending(args: argparse.Namespace) -> int | None:
    root = state_root()
    journal = root / "transaction.json"
    if not journal.exists():
        return None
    if args.dry_run:
        raise UpdateError(
            f"an interrupted update requires recovery; run `district update --yes` or "
            f"`/usr/bin/python3 {root / 'transaction.py'} recover {root}`"
        )
    if not args.yes:
        if not sys.stdin.isatty():
            raise UpdateError("interrupted update recovery requires confirmation; pass --yes")
        pending = transaction.read_json(journal)
        plan = {
            "mode": "recovery", "installed": {"version": "unknown", "sha": "interrupted"},
            "target": pending["current"], "ci": None,
            "operations": ["restore the retained installation and captured District service state"],
        }
        if not confirm(plan, args.json):
            emit({**plan, "ok": False, "status": "cancelled"}, args.json)
            return 1
    with operation_lock() as lock_fd:
        helper = root / "transaction.py"
        if not helper.is_file():
            raise UpdateError(f"retained transaction helper is missing: {helper}")
        python = external_python(Path("/__district_unmanaged__"))
        code, result = run_helper(python, helper, "recover", root, root, lock_fd)
    emit(result, args.json, error=code != 0)
    return 0 if code == 0 else 1


def cleanup_transients(root: Path, state: dict | None) -> None:
    shutil.rmtree(root / "stage", ignore_errors=True)
    artifacts = root / "artifacts"
    keep = Path(state["previous"]["artifact"]) if state and state.get("previous") else None
    if not artifacts.is_dir():
        return
    for candidate in artifacts.iterdir():
        if candidate == keep:
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        else:
            candidate.unlink(missing_ok=True)


def execute(plan: dict, context: dict, lock_fd: int) -> dict:
    root = context["root"]
    state = context["state"]
    cleanup_transients(root, state)
    helper = install_helper(root)
    try:
        stage = prepare(plan, context) if plan["mode"] == "update" else None
    except UpdateError as exc:
        result = {
            "ok": False,
            "status": "prepare-failed",
            "error": str(exc),
            "current": plan["installed"],
            "target": plan["target"],
            "log": str(root / "transaction.log"),
        }
        transaction.write_outcome(root, result)
        shutil.rmtree(root / "stage", ignore_errors=True)
        return result
    previous = state.get("previous") if state else None
    request = root / "request.json"
    data = {
        "action": plan["mode"],
        "state_root": str(root),
        "tool": str(context["tool"]),
        "entrypoint": str(context["entrypoint"]),
        "lock": str(host.operation_lock_path()),
        "uv": str(context["uv"]),
        "systemctl": str(context["systemctl"]),
        "python_version": context["python_version"],
        "current": plan["installed"],
        "target": plan["target"],
        "install_source": f"git+https://github.com/{REPOSITORY}.git@{plan['target']['sha']}" if stage else None,
        "target_artifact": previous.get("artifact") if plan["mode"] == "rollback" else None,
        "previous_artifact": previous.get("artifact") if previous else None,
        "stage": str(stage) if stage else None,
    }
    write_request(request, data)
    try:
        code, result = run_helper(context["python"], helper, "run", request, root, lock_fd)
    except UpdateError:
        if not (root / "transaction.json").exists():
            cleanup_transients(root, state)
        raise
    finally:
        request.unlink(missing_ok=True)
    if code != 0 and not result.get("rollback", {}).get("ok"):
        if not (root / "transaction.json").exists():
            cleanup_transients(root, state)
        result["ok"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    json_mode = "--json" in argv
    parser = Parser(prog="district update", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="show the query-only plan without creating update state or locks")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--to", metavar="REF", help="official repository tag, branch, or commit (default main)")
    choice.add_argument("--rollback", action="store_true", help="restore the recorded previous successful installation offline")
    parser.add_argument("--yes", action="store_true", help="accept the displayed plan for noninteractive use")
    parser.add_argument("--json", action="store_true", help="emit one machine-readable result on stdout")
    try:
        args = parser.parse_args(argv)
        recovered = recover_pending(args)
        if recovered is not None:
            return recovered
        plan, context = make_plan(args)
        if plan["target"]["sha"] == plan["installed"]["sha"]:
            plan.update(ok=True, status="up-to-date", operations=[])
            emit(plan, args.json)
            return 0
        if plan["ci"] is not None and not plan["ci"]["eligible"]:
            plan["status"] = "ineligible"
            emit(plan, args.json)
            return 1
        if args.dry_run:
            emit(plan, args.json)
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                if not args.json:
                    emit(plan, False)
                raise UpdateError("confirmation required on noninteractive input; pass --yes")
            if not confirm(plan, args.json):
                result = {**plan, "ok": False, "status": "cancelled"}
                emit(result, args.json)
                return 1
        with operation_lock() as lock_fd:
            if (context["root"] / "transaction.json").exists():
                raise UpdateError("another update transaction appeared after preflight; invoke update again to recover")
            result = execute(plan, context, lock_fd)
        emit(result, args.json)
        return 0 if result.get("ok") else 1
    except Exception as exc:
        emit({"ok": False, "status": "error", "error": str(exc)}, json_mode, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
