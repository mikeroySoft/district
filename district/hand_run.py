"""Frozen local hand-run units and atomic, in-use guarded script updates."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile


ACTIVE = {"active", "activating", "deactivating", "reloading"}


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "district"


def run_command(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30, **kwargs)
    if result.returncode:
        raise ValueError(f"{argv[0]} failed: {result.stderr.strip() or result.stdout.strip() or result.returncode}")
    return result


@contextmanager
def operation_lock(data: Path):
    data.mkdir(parents=True, exist_ok=True)
    with (data / ".hand-run.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def active_units() -> dict[str, dict[str, str]]:
    listing = run_command(["systemctl", "--user", "list-units", "--no-legend", "--plain",
                           "--type=service", "--state=" + ",".join(sorted(ACTIVE))])
    units = {}
    for line in listing.stdout.splitlines():
        if not line.strip():
            continue
        unit = line.split()[0]
        result = run_command(["systemctl", "--user", "show", unit,
                              "--property=ActiveState,ExecStart,ExecStopPost"])
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if not props.get("ActiveState"):
            raise ValueError(f"systemctl: missing ActiveState for {unit}")
        if props["ActiveState"] in ACTIVE:
            units[unit] = props
    return units


def decoded(value: str) -> str:
    # systemctl show escapes whitespace and other non-printing path bytes.
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), value)


def run_roots(data: Path, value: str) -> set[Path]:
    prefix = str(data / "runs") + "/"
    return {data / "runs" / match for match in re.findall(re.escape(prefix) + r"([^/\s;{}]+)", value)}


def unit_guard(target: Path, data: Path, units: dict[str, dict[str, str]]) -> None:
    for unit, props in units.items():
        commands = decoded(props.get("ExecStart", "") + " " + props.get("ExecStopPost", ""))
        if str(target) in commands:
            raise ValueError(f"{target}: in use by {unit} (ExecStart/ExecStopPost)")
        for root in run_roots(data, commands):
            if target.is_relative_to(root):
                raise ValueError(f"{target}: frozen run directory in use by {unit}")
            try:
                sources = json.loads((root / "sources.json").read_text())["sources"]
                if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
                    raise ValueError("invalid sources")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{unit}: cannot inspect frozen run {root}: {exc}") from exc
            if any(target.is_relative_to(Path(source)) for source in sources):
                raise ValueError(f"{target}: source tree in use by {unit} (frozen run {root})")


def process_guard(target: Path) -> None:
    identity = target.stat()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            own = process.stat().st_uid == os.getuid()
        except FileNotFoundError:
            continue
        try:
            command = (process / "cmdline").read_bytes().split(b"\0")
            for argument in command:
                text = os.fsdecode(argument)
                # Relative script arguments are resolved against that process's cwd.
                if text and not text.startswith("-"):
                    candidate = process / "cwd" / text
                    try:
                        if candidate.samefile(target):
                            raise ValueError(f"{target}: executing in process {process.name} (cmdline)")
                    except OSError:
                        pass
            try:
                descriptors = list((process / "fd").iterdir())
            except PermissionError:
                # Non-dumpable processes can expose cmdline but not their descriptors.
                continue
            for fd in descriptors:
                try:
                    info = fd.stat()
                except (FileNotFoundError, PermissionError):
                    # Protected manager descriptors may be opaque even to their owner.
                    # Its active services are also checked through systemctl.
                    continue
                if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                    raise ValueError(f"{target}: open in process {process.name} (fd {fd.name})")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as exc:
            if own:
                raise ValueError(f"{target}: cannot inspect own process {process.name}: {exc}") from exc


def update(target: Path, replacement: Path, data: Path) -> None:
    if target.is_symlink():
        raise ValueError(f"{target}: refusing symlink update")
    target = target.resolve(strict=True)
    if not target.is_relative_to(data) or not target.is_file():
        raise ValueError(f"{target}: expected a hand-run file under {data}")
    content = replacement.read_bytes()
    mode = stat.S_IMODE(target.stat().st_mode)
    # Recheck immediately before rename; our launch/update operations share the lock.
    unit_guard(target, data, active_units())
    process_guard(target)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        unit_guard(target, data, active_units())
        process_guard(target)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check(data: Path) -> list[str]:
    failures = []
    for unit, props in active_units().items():
        commands = decoded(props.get("ExecStart", "") + props.get("ExecStopPost", ""))
        if not unit.startswith("handland-") and str(data) not in commands:
            continue
        for key in ("ExecStart", "ExecStopPost"):
            value = decoded(props.get(key, ""))
            if not value and key == "ExecStopPost":
                continue
            paths = re.findall(r"(?:^|[\s{])path=(.*?)\s*;", value)
            roots = run_roots(data, value)
            if not paths or len(roots) != 1:
                failures.append(f"{unit}: {key} must point only into a frozen run directory")
                continue
            root = roots.pop()
            expected = root / ("start" if key == "ExecStart" else "stop")
            if any(Path(path) != expected for path in paths):
                failures.append(f"{unit}: {key} must execute {expected}, not a live path/interpreter")
                continue
            try:
                if not (root / "sources.json").is_file() or root.is_symlink():
                    raise ValueError("missing frozen sources manifest or symlink run")
                for path in [root, *root.rglob("*")]:
                    if path.is_symlink() or path.stat().st_mode & 0o222:
                        raise ValueError(f"writable or symlink path: {path}")
                run_command(["sha256sum", "--check", "--status", "checksums.txt"], cwd=root)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failures.append(f"{unit}: {key} unsafe frozen run: {exc}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="district hand-run", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("launch", help="freeze handland trees and start a user unit")
    launch.add_argument("name", help="unit name, with or without the handland- prefix")
    launch.add_argument("script", type=Path)
    launch.add_argument("--stop-post", type=Path)
    launch.add_argument("--include", action="append", type=Path, default=[], help="extra dependency tree under District data")
    launch.add_argument("--arg", action="append", default=[], help="entry script argument (repeatable)")
    edit = commands.add_parser("update", help="atomically replace a script only when not in use")
    edit.add_argument("target", type=Path)
    edit.add_argument("replacement", type=Path)
    commands.add_parser("check", help="fail for unsafe active hand-run ExecStart/ExecStopPost")
    args = parser.parse_args(argv)
    data = data_dir().resolve()
    try:
        with operation_lock(data):
            if args.command == "update":
                update(args.target.expanduser(), args.replacement.expanduser(), data)
                print(f"Updated {args.target} by atomic rename")
            elif args.command == "check":
                failures = check(data)
                if failures:
                    raise ValueError("\n".join(failures))
                print("Active hand-run units use verified frozen scripts")
            else:
                from .frozen_run import freeze

                name = args.name.removesuffix(".service")
                name = name if name.startswith("handland-") else "handland-" + name
                if not re.fullmatch(r"handland-[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                    raise ValueError(f"invalid hand-run unit name: {args.name}")
                if name + ".service" in active_units():
                    raise ValueError(f"{name}.service: already running")
                root = freeze(data, args.script.expanduser(), args.stop_post.expanduser() if args.stop_post else None,
                              [path.expanduser() for path in args.include], args.arg)
                command = ["systemd-run", "--user", "--collect", f"--unit={name}"]
                if args.stop_post:
                    command.append("--property=ExecStopPost=" + shlex.quote(str(root / "stop")))
                command += ["--", str(root / "start")]
                run_command(command)
                print(root)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"district hand-run: {exc}", file=sys.stderr)
        return 1
