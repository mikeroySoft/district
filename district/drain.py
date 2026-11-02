"""Host-level Factory drain flag and operator clear command."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from district import host

DRAIN_MAX_TTL = 8 * 60 * 60
DEFAULT_TIMEOUT = 6 * 60 * 60
_EXPIRY_MARGIN = 15 * 60
_REMOTE_TIMEOUT = 10
_REQUIRED = {
    "schema_version": int,
    "requested_by": str,
    "reason": str,
    "pid": int,
    "created_at": str,
    "expires_at": str,
}


def path(runner: host.RemoteRunner | None = None) -> Path | host.RemotePath:
    if isinstance(runner, host.RemoteRunner):
        script = (
            "import os; from pathlib import Path; "
            "base=Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state'); "
            "print(base/'district/drain')"
        )
        proc = runner(["python3", "-c", script], quiet=True, timeout=_REMOTE_TIMEOUT)
        if proc.returncode:
            raise OSError(proc.stderr.strip() or "cannot resolve remote drain path")
        value = proc.stdout.strip()
        if not value:
            raise OSError("remote drain path is empty")
        return runner.path(value)
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "district" / "drain"


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def request(runner: host.RemoteRunner | None = None, *, timeout: float = DEFAULT_TIMEOUT,
            reason: str = "engine upgrade", requested_by: str = "district apply") -> dict:
    created = datetime.now(timezone.utc)
    ttl = min(DRAIN_MAX_TTL, max(0, timeout) + _EXPIRY_MARGIN)
    value = {
        "schema_version": 1,
        "requested_by": requested_by,
        "reason": reason,
        "pid": os.getpid(),
        "created_at": _timestamp(created),
        "expires_at": _timestamp(created + timedelta(seconds=ttl)),
    }
    target = path(runner)
    body = json.dumps(value, sort_keys=True) + "\n"
    if isinstance(target, host.RemotePath):
        target.write_text(body, timeout=_REMOTE_TIMEOUT)
        return value
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def clear(runner: host.RemoteRunner | None = None) -> None:
    target = path(runner)
    if isinstance(target, host.RemotePath):
        script = "import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)"
        runner(["python3", "-c", script, str(target)], check=True, quiet=True,
               timeout=_REMOTE_TIMEOUT)
    else:
        target.unlink(missing_ok=True)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp is not UTC")
    return parsed.astimezone(timezone.utc)


def _remote_snapshot(runner: host.RemoteRunner, target: host.RemotePath) -> tuple[str, float] | None:
    script = (
        "import json,pathlib,sys\n"
        "p=pathlib.Path(sys.argv[1])\n"
        "try:\n s=p.stat(); body=p.read_text(encoding='utf-8')\n"
        "except FileNotFoundError:\n raise SystemExit(44)\n"
        "except OSError as e:\n print(str(e),file=sys.stderr); raise SystemExit(45)\n"
        "print(json.dumps({'body':body,'mtime':s.st_mtime}))"
    )
    proc = runner(["python3", "-c", script, str(target)], quiet=True, timeout=_REMOTE_TIMEOUT)
    if proc.returncode == 44:
        return None
    if proc.returncode:
        raise OSError(proc.stderr.strip() or f"cannot read {target}")
    value = json.loads(proc.stdout)
    return value["body"], float(value["mtime"])


def _snapshot(runner: host.RemoteRunner | None) -> tuple[str, float] | None:
    target = path(runner)
    if isinstance(target, host.RemotePath):
        return _remote_snapshot(runner, target)
    try:
        mtime = target.stat().st_mtime
        return target.read_text(encoding="utf-8"), mtime
    except FileNotFoundError:
        return None


def _advice(detail: str) -> str:
    return f"{detail}; run `district drain --clear` to remove it"


def status(runner: host.RemoteRunner | None = None) -> str | None:
    try:
        snapshot = _snapshot(runner)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            host.RemoteHostError) as exc:
        return _advice(f"unreadable drain flag ({exc}); Factory ignores unreadable flags")
    if snapshot is None:
        return None
    body, mtime_value = snapshot
    now = datetime.now(timezone.utc)
    try:
        mtime = datetime.fromtimestamp(mtime_value, timezone.utc)
        safety_expiry = mtime + timedelta(seconds=DRAIN_MAX_TTL)
    except (OverflowError, OSError, ValueError) as exc:
        return _advice(f"unreadable drain flag mtime ({exc}); Factory ignores unreadable flags")
    if mtime > now + timedelta(seconds=60):
        return _advice(
            f"future-mtime drain flag ({_timestamp(mtime)}); Factory ignores future-mtime flags"
        )
    if safety_expiry <= now:
        return _advice(
            f"stale drain flag (8h safety limit passed at {_timestamp(safety_expiry)}); it no longer blocks Factory"
        )
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("JSON root is not an object")
        if any(key not in value or not isinstance(value[key], kind) for key, kind in _REQUIRED.items()):
            raise ValueError("required field is missing or has the wrong type")
        if (isinstance(value["schema_version"], bool) or isinstance(value["pid"], bool)
                or value["schema_version"] != 1):
            raise ValueError("unsupported schema or pid")
        created = _parse_timestamp(value["created_at"])
        expires = _parse_timestamp(value["expires_at"])
        if expires > created + timedelta(seconds=DRAIN_MAX_TTL):
            return _advice(
                f"over-cap drain flag (expires {_timestamp(expires)}); Factory ignores over-cap flags"
            )
        if expires <= created:
            raise ValueError("expiry is not after creation")
    except (OverflowError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return _advice(
            f"malformed drain flag ({exc}); Factory stops blocking no later than {_timestamp(safety_expiry)}"
        )
    if expires <= now:
        return _advice(
            f"expired drain flag (expired {_timestamp(expires)}); it no longer blocks Factory"
        )
    return _advice(
        f"active drain requested by {value['requested_by']} for {value['reason']}; expires {_timestamp(expires)}"
    )


@contextmanager
def interrupted():
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}

    def stop(signum, _frame):
        raise SystemExit(128 + signum)

    try:
        for signum in previous:
            signal.signal(signum, stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="district drain", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--clear", action="store_true", help="remove the host drain flag")
    parser.add_argument("--host", metavar="NAME", help="registered host to clear (default local)")
    args = parser.parse_args(argv)
    if not args.clear:
        parser.error("--clear is required")
    runner = None
    if args.host not in (None, "local"):
        data = host.load()
        configured = host.hosts(data)
        if args.host not in configured:
            raise host.DistrictError(f"host {args.host!r} is not defined in [hosts]")
        runner = host.remote_runner(args.host, configured[args.host])
    lock_path = host.operation_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"district drain: another District apply, update, or drain operation is running ({lock_path})",
                file=sys.stderr,
            )
            return 1
        clear(runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
