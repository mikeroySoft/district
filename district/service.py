"""Host service-manager adapters for systemd user units and launchd agents."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

Runner = Callable[..., subprocess.CompletedProcess]
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(h|d|m|min|s)?$")
LAUNCHD_CLEANUP_TIMEOUT = 3


@dataclass(frozen=True)
class ServiceDefinition:
    label: str
    description: str
    argv: tuple[str, ...]
    root: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    interval: str | None = None
    boot_delay: str | None = None
    timer_description: str | None = None
    randomized_delay: str | None = None
    keep_alive: bool = False
    after_network: bool = False
    working_directory: bool = True


@dataclass(frozen=True)
class ServiceStatus:
    load_state: str
    active_state: str
    unit_file_state: str
    main_pid: int = 0
    argv: tuple[str, ...] | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    @property
    def loaded(self) -> bool:
        return self.load_state == "loaded"


@dataclass(frozen=True)
class ScheduledJobStatus:
    unit_file_state: str
    active_state: str
    service_active_state: str
    last_trigger: str = ""
    next_trigger: str = ""


@dataclass(frozen=True)
class LogLocation:
    stdout: str
    stderr: str


class ServiceBackend(ABC):
    @abstractmethod
    def configuration(self) -> Mapping[str, object]:
        raise NotImplementedError
    @abstractmethod
    def definitions_changed(self, label: str, wanted: Mapping[str, str]) -> bool:
        raise NotImplementedError



    @abstractmethod
    def install(self, definition: ServiceDefinition) -> Path:
        raise NotImplementedError

    @abstractmethod
    def remove(self, label: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def start(self, name: str, *, restart: bool = False, check: bool = True,
              timeout: float = 0) -> subprocess.CompletedProcess:
        raise NotImplementedError

    @abstractmethod
    def stop(self, name: str, *, check: bool = True, timeout: float = 0) -> subprocess.CompletedProcess:
        raise NotImplementedError

    @abstractmethod
    def enable(self, name: str, enabled: bool = True, *, now: bool = True,
               check: bool = True, timeout: float = 0) -> subprocess.CompletedProcess:
        raise NotImplementedError

    @abstractmethod
    def is_active(self, name: str, *, timeout: float | None = None) -> str:
        raise NotImplementedError

    @abstractmethod
    def status(self, name: str, *, timeout: float | None = None) -> ServiceStatus:
        raise NotImplementedError

    @abstractmethod
    def scheduled_jobs(
        self, names: set[str], *, timeout: float | None = None,
    ) -> Mapping[str, ScheduledJobStatus]:
        raise NotImplementedError

    @abstractmethod
    def logs(self, label: str, root: Path) -> LogLocation:
        raise NotImplementedError

    @abstractmethod
    def manager_status(self) -> str:
        raise NotImplementedError

    def factory_units(self, *, timeout: float = 10) -> set[str]:
        """Every Factory service/timer, including auxiliary and unregistered units."""
        raise NotImplementedError

    def pause_timer(self, name: str, *, timeout: float) -> subprocess.CompletedProcess:
        return self.enable(name, False, timeout=timeout)

    def resume_timer(self, name: str, *, timeout: float = 10) -> subprocess.CompletedProcess:
        return self.enable(name, check=False, timeout=timeout)

def remaining_timeout(deadline: float) -> float:
    """Reject an expired shared deadline before issuing another command."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("engine idle window", 0)
    return remaining



def _definition_bodies(directory, pattern: str, *, deadline: float | None = None) -> dict[str, str]:
    timeout = remaining_timeout(deadline) if deadline is not None else 10
    if isinstance(directory, Path):
        return {path.name: path.read_text() for path in directory.glob(pattern)}
    script = (
        "import json,pathlib,sys; "
        "print(json.dumps({p.name:p.read_text() for p in pathlib.Path(sys.argv[1]).glob(sys.argv[2])}))"
    )
    proc = directory.runner(["python3", "-c", script, str(directory), pattern], timeout=timeout)
    if proc.returncode:
        raise OSError(proc.stderr.strip() or f"cannot scan {directory}")
    return json.loads(proc.stdout)


def _home_local_bin(home: Path) -> Path:
    return home / ".local" / "bin"


def _is_user_local_bin(entry: str, home: Path) -> bool:
    if not entry:
        return False
    target = _home_local_bin(home)
    if entry in {str(target), str(home / ".local/bin")}:
        return True
    try:
        return Path(entry).resolve() == target.resolve()
    except OSError:
        return False


def _is_tool_install(entry: str, home: Path) -> bool:
    if not entry or _is_user_local_bin(entry, home):
        return False
    norm = entry.replace("\\", "/")
    return (
        "/.local/share/mise/installs/" in norm
        or norm.rstrip("/").endswith("/.local/share/mise/shims")
        or "/.local/share/mise/shims/" in norm
        or norm.rstrip("/").endswith("/.cargo/bin")
        or "/.local/share/uv/tools/" in norm and norm.rstrip("/").endswith("/bin")
    )


def service_path(raw: str | None = None, *, home: Path | None = None) -> str:
    """Prefer real tool installs to wrappers in ~/.local/bin, preserving all entries."""
    path = os.environ.get("PATH", "") if raw is None else raw
    home = Path.home() if home is None else home
    parts = path.split(":")
    deferred: list[str] = []
    seen_deferred: set[str] = set()
    remaining: list[str] = []
    first_deferred_at: int | None = None
    for entry in parts:
        if entry and _is_user_local_bin(entry, home):
            try:
                key = str(Path(entry).resolve())
            except OSError:
                key = entry
            if key not in seen_deferred:
                seen_deferred.add(key)
                deferred.append(entry)
                if first_deferred_at is None:
                    first_deferred_at = len(remaining)
            continue
        remaining.append(entry)
    if not deferred:
        return path
    insert_at = 0
    for index, entry in enumerate(remaining):
        if entry and _is_tool_install(entry, home):
            insert_at = index + 1
    if insert_at == 0:
        insert_at = 0 if first_deferred_at is None else first_deferred_at
    return ":".join(remaining[:insert_at] + deferred + remaining[insert_at:])


def interval_seconds(value: str) -> int:
    match = _DURATION.fullmatch(value.strip())
    if not match:
        raise ValueError(f"invalid service interval: {value!r}")
    number, unit = float(match.group(1)), match.group(2) or "s"
    seconds = number * {"d": 86400, "h": 3600, "m": 60, "min": 60, "s": 1}[unit]
    if not seconds.is_integer() or seconds <= 0:
        raise ValueError(f"service interval must be a positive whole number of seconds: {value!r}")
    return int(seconds)


def _exec_start(raw: str) -> tuple[str, ...] | None:
    match = re.fullmatch(
        r"\{\s*path=(.*?)\s+;\s+argv\[]=(.*?)\s+;\s+ignore_errors=(?:yes|no)\s+;[^{}]*\}", raw,
    )
    if not match:
        return None
    try:
        argv = tuple(shlex.split(match[2]))
    except ValueError:
        return None
    return argv if argv and argv[0] == match[1] else None


def _show_blocks(output: str) -> dict[str, dict[str, str]]:
    result = {}
    for raw in output.strip().split("\n\n") if output.strip() else []:
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if values.get("Id"):
            result[values["Id"]] = values
    return result


class SystemdUserBackend(ServiceBackend):
    def __init__(self, run: Runner, *, unit_dir: Path, executable: str = "systemctl") -> None:
        self.run = run
        self.unit_dir = unit_dir
        self.executable = executable
    def configuration(self) -> dict[str, str]:
        return {"platform": "linux", "executable": self.executable, "unit_dir": str(self.unit_dir)}
    def definitions_changed(self, label: str, wanted: Mapping[str, str]) -> bool:
        optional = (
            "-dashboard.service", "-triage-probe.service", "-triage-probe.timer",
            "-eval.service", "-eval.timer",
        )
        known = {f"{label}.service", f"{label}.timer", *(f"{label}{suffix}" for suffix in optional)}
        existing = {name for name in known if (self.unit_dir / name).exists()}
        return existing != set(wanted) or any(
            (self.unit_dir / name).read_text() != body for name, body in wanted.items()
        )

    def factory_units(self, *, timeout: float = 10) -> set[str]:
        deadline = time.monotonic() + timeout
        units = set(_definition_bodies(self.unit_dir, "factory-*.service", deadline=deadline))
        units.update(_definition_bodies(self.unit_dir, "factory-*.timer", deadline=deadline))
        proc = self._run("list-units", "--all", "--plain", "--no-legend",
                         "factory-*.service", "factory-*.timer", check=False, timeout=remaining_timeout(deadline))
        if proc.returncode:
            raise OSError(proc.stderr.strip() or "cannot list Factory units")
        units.update(line.split()[0] for line in proc.stdout.splitlines() if line.split())
        return {name for name in units if name.endswith((".service", ".timer"))}


    def _run(self, *args: str, check: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        argv = [self.executable, "--user", *args]
        if timeout is None:
            return self.run(argv, check=check)
        return self.run(argv, check=check, timeout=timeout)

    @staticmethod
    def _service(definition: ServiceDefinition) -> str:
        after = "After=network.target\n" if definition.after_network else ""
        kind = "Type=oneshot\n" if definition.interval else ""
        working = f"WorkingDirectory={definition.root}\n" if definition.working_directory else ""
        environment = "".join(f"Environment={key}={value}\n" for key, value in definition.environment.items())
        restart = "Restart=on-failure\nRestartSec=5\n" if definition.keep_alive else ""
        install = "\n[Install]\nWantedBy=default.target\n" if definition.keep_alive else ""
        return (
            f"[Unit]\nDescription={definition.description}\n{after}\n"
            f"[Service]\n{kind}{working}{environment}ExecStart={' '.join(definition.argv)}\n"
            f"{restart}{install}"
        )

    @staticmethod
    def _timer(definition: ServiceDefinition) -> str:
        randomized = (
            f"RandomizedDelaySec={definition.randomized_delay}\n" if definition.randomized_delay else ""
        )
        boot = f"OnBootSec={definition.boot_delay}\n" if definition.boot_delay else ""
        description = definition.timer_description or definition.description
        return (
            f"[Unit]\nDescription={description}\n\n[Timer]\n{boot}"
            f"OnUnitActiveSec={definition.interval}\n{randomized}\n"
            "[Install]\nWantedBy=timers.target\n"
        )

    def install(self, definition: ServiceDefinition) -> Path:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        service = self.unit_dir / f"{definition.label}.service"
        wanted = {service: self._service(definition)}
        if definition.interval:
            wanted[self.unit_dir / f"{definition.label}.timer"] = self._timer(definition)
        for path, body in wanted.items():
            if not path.exists() or path.read_text() != body:
                path.write_text(body)
                print(f"wrote {path}")
        self._run("daemon-reload")
        if definition.interval:
            self.enable(f"{definition.label}.timer")
        elif definition.keep_alive:
            self.enable(f"{definition.label}.service")
        return service

    def remove(self, label: str) -> None:
        self.enable(f"{label}.timer", False, check=False)
        self.enable(f"{label}.service", False, check=False)
        for suffix in ("timer", "service"):
            path = self.unit_dir / f"{label}.{suffix}"
            if path.exists():
                path.unlink()
                print(f"removed {path}")
        self._run("daemon-reload")

    def start(self, name: str, *, restart: bool = False, check: bool = True,
              timeout: float = 0) -> subprocess.CompletedProcess:
        return self._run("restart" if restart else "start", name, check=check, timeout=timeout or None)

    def stop(self, name: str, *, check: bool = True, timeout: float = 0) -> subprocess.CompletedProcess:
        return self._run("stop", name, check=check, timeout=timeout or None)

    def enable(self, name: str, enabled: bool = True, *, now: bool = True,
               check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
        if timeout is not None and timeout <= 0:
            raise subprocess.TimeoutExpired([self.executable, "enable" if enabled else "disable", name], timeout)
        args = ["enable" if enabled else "disable"]
        if now:
            args.append("--now")
        args.append(name)
        return self._run(*args, check=check, timeout=timeout)

    def is_active(self, name: str, *, timeout: float | None = None) -> str:
        return self._run("is-active", name, check=False, timeout=timeout).stdout.strip()

    def _property(self, name: str, prop: str, *, deadline: float | None = None) -> str:
        value = self._run("show", name, f"--property={prop}", "--value", "--no-pager",
                          timeout=remaining_timeout(deadline) if deadline is not None else None).stdout.strip()
        if not value:
            value = self._run("show", "-p", prop, "--value", name,
                              timeout=remaining_timeout(deadline) if deadline is not None else None).stdout.strip()
        return value

    def status(self, name: str, *, timeout: float | None = None) -> ServiceStatus:
        deadline = time.monotonic() + timeout if timeout is not None else None
        load = self._property(name, "LoadState", deadline=deadline)
        body = _definition_bodies(self.unit_dir, name, deadline=deadline).get(name)
        if not load and body is not None:
            load = "loaded"
        environment = {}
        file_argv = None
        if body is not None:
            for line in body.splitlines():
                if line.startswith("Environment=") and "=" in line.removeprefix("Environment="):
                    key, value = line.removeprefix("Environment=").split("=", 1)
                    environment[key] = value
                elif line.startswith("ExecStart="):
                    try:
                        file_argv = tuple(shlex.split(line.removeprefix("ExecStart=").lstrip("-")))
                    except ValueError:
                        pass
        if load in ("", "not-found"):
            pid = self._property(name, "MainPID", deadline=deadline) if name.endswith(".service") else ""
            return ServiceStatus(
                load or "not-found", "inactive", "not-found", int(pid) if pid.isdigit() else 0,
                environment=environment,
            )
        active = self._property(name, "ActiveState", deadline=deadline)
        enabled = self._property(name, "UnitFileState", deadline=deadline)
        pid = self._property(name, "MainPID", deadline=deadline)
        raw = self._property(name, "ExecStart", deadline=deadline) if name.endswith(".service") else ""
        return ServiceStatus(
            load, active, enabled, int(pid) if pid.isdigit() else 0,
            _exec_start(raw) if raw else file_argv, environment,
        )

    def scheduled_jobs(self, names: set[str], *,
                       timeout: float | None = None) -> dict[str, ScheduledJobStatus]:
        timers = set(names)
        try:
            timers.update(path.name for path in self.unit_dir.glob("factory-*.timer"))
        except OSError:
            pass
        ordered = sorted(timers)
        services = [name.removesuffix(".timer") + ".service" for name in ordered]
        properties = "Id,UnitFileState,ActiveState,LastTriggerUSec,NextElapseUSecRealtime"
        proc = self._run(
            "show", *ordered, *services, f"--property={properties}", check=False, timeout=timeout,
        )
        blocks = _show_blocks(proc.stdout)
        result = {}
        for name, service in zip(ordered, services):
            timer = blocks.get(name, {})
            result[name] = ScheduledJobStatus(
                unit_file_state=timer.get("UnitFileState", ""),
                active_state=timer.get("ActiveState", ""),
                service_active_state=blocks.get(service, {}).get("ActiveState", ""),
                last_trigger=timer.get("LastTriggerUSec", ""),
                next_trigger=timer.get("NextElapseUSecRealtime", ""),
            )
        return result

    def logs(self, label: str, root: Path) -> LogLocation:
        command = f"journalctl --user -u {shlex.quote(label)}.service"
        return LogLocation(command, command)

    def manager_status(self) -> str:
        proc = self._run("is-system-running", check=False)
        return proc.stdout.strip() or proc.stderr.strip() or f"exited {proc.returncode}"


class LaunchdBackend(ServiceBackend):
    def __init__(self, run: Runner, *, home: Path | None = None, uid: int | None = None,
                 raw_path: str | None = None, executable: str = "launchctl") -> None:
        self.run = run
        self.home = Path.home() if home is None else home
        self.uid = os.getuid() if uid is None else uid
        base_path = os.environ.get("PATH", "") if raw_path is None else raw_path
        with_brew = base_path if "/opt/homebrew/bin" in base_path.split(":") else f"{base_path}:/opt/homebrew/bin"
        self.path = service_path(with_brew, home=self.home)
        self.executable = executable
    def configuration(self) -> dict[str, str | int]:
        return {
            "platform": "macos",
            "executable": self.executable,
            "home": str(self.home),
            "uid": self.uid,
            "path": self.path,
        }
    def definitions_changed(self, label: str, wanted: Mapping[str, str]) -> bool:
        optional = ("-dashboard", "-triage-probe", "-eval")
        known = {f"{label}.plist", *(f"{label}{suffix}.plist" for suffix in optional)}
        existing = {name for name in known if (self.agent_dir / name).exists()}
        return existing != set(wanted) or any(
            (self.agent_dir / name).read_text() != body for name, body in wanted.items()
        )

    def factory_units(self, *, timeout: float = 10) -> set[str]:
        deadline = time.monotonic() + timeout
        units = set()
        for name, body in _definition_bodies(self.agent_dir, "*factory*.plist", deadline=deadline).items():
            document = plistlib.loads(body.encode())
            label = document.get("Label", name.removesuffix(".plist"))
            units.add(f"{label}.service")
            if "StartInterval" in document or "StartCalendarInterval" in document:
                units.add(f"{label}.timer")
        return units

    def pause_timer(self, name: str, *, timeout: float) -> subprocess.CompletedProcess:
        # Disable scheduling without bootout, which would kill the in-flight pass.
        return self._run("disable", f"{self.domain}/{self._label(name)}", timeout=timeout)

    def resume_timer(self, name: str, *, timeout: float = 10) -> subprocess.CompletedProcess:
        return self._run("enable", f"{self.domain}/{self._label(name)}", check=False, timeout=timeout)


    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def agent_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    def _run(self, *args: str, check: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        argv = [self.executable, *args]
        if timeout is None:
            return self.run(argv, check=check)
        return self.run(argv, check=check, timeout=timeout)

    def _path(self, label: str) -> Path:
        return self.agent_dir / f"{label}.plist"

    @staticmethod
    def _label(name: str) -> str:
        return name.removesuffix(".service").removesuffix(".timer")

    def logs(self, label: str, root: Path) -> LogLocation:
        directory = root / ".factory" / "logs"
        return LogLocation(str(directory / f"{label}.stdout.log"), str(directory / f"{label}.stderr.log"))

    def render(self, definition: ServiceDefinition) -> bytes:
        environment = {"PATH": self.path, **definition.environment}
        logs = self.logs(definition.label, definition.root)
        document: dict = {
            "Label": definition.label,
            "ProgramArguments": list(definition.argv),
        }
        if definition.working_directory:
            document["WorkingDirectory"] = str(definition.root)
        document.update(EnvironmentVariables=environment, RunAtLoad=True)
        if definition.interval:
            document["StartInterval"] = interval_seconds(definition.interval)
        if definition.keep_alive:
            document["KeepAlive"] = True
        document["StandardOutPath"] = logs.stdout
        document["StandardErrorPath"] = logs.stderr
        return plistlib.dumps(document, sort_keys=False)

    def install(self, definition: ServiceDefinition) -> Path:
        path = self._path(definition.label)
        path.parent.mkdir(parents=True, exist_ok=True)
        log_dir = definition.root / ".factory" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        body = self.render(definition)
        existed = path.exists()
        changed = not existed or path.read_bytes() != body
        if existed and changed:
            self._run("bootout", f"{self.domain}/{definition.label}", check=False)
        if changed:
            path.write_bytes(body)
            print(f"wrote {path}")
        if changed or not self.status(f"{definition.label}.service").loaded:
            self._run("bootstrap", self.domain, str(path))
        return path

    def remove(self, label: str) -> None:
        self._run("bootout", f"{self.domain}/{label}", check=False)
        path = self._path(label)
        if path.exists():
            path.unlink()
            print(f"removed {path}")

    def start(self, name: str, *, restart: bool = False, check: bool = True,
              timeout: float = 0) -> subprocess.CompletedProcess:
        deadline = time.monotonic() + timeout

        def budget():
            return remaining_timeout(deadline) if timeout else None

        label = self._label(name)
        path = self._path(label)
        target = f"{self.domain}/{label}"
        try:
            body = path.read_bytes(timeout=budget() or 10) if not isinstance(path, Path) else path.read_bytes()
            scheduled = "StartInterval" in plistlib.loads(body)
        except (OSError, plistlib.InvalidFileException):
            scheduled = False
        was_loaded = self.status(name, timeout=budget()).loaded
        if was_loaded and name.endswith(".timer"):
            return self._run("print", target, check=check, timeout=budget())
        try:
            if not was_loaded:
                proc = self._run("bootstrap", self.domain, str(path), check=check, timeout=budget())
            else:
                args = ["kickstart"]
                if restart:
                    args.append("-k")
                args.append(target)
                proc = self._run(*args, check=check, timeout=budget())
            if proc.returncode or not scheduled:
                return proc
            while True:
                status = self._run("print", target, check=False, timeout=remaining_timeout(deadline))
                if status.returncode:
                    result = subprocess.CompletedProcess(proc.args, status.returncode, proc.stdout, status.stderr)
                else:
                    running = re.search(r"^\s*state\s*=\s*running\s*$", status.stdout, re.MULTILINE)
                    exit_code = re.search(r"^\s*last exit code\s*=\s*(-?\d+)\s*$", status.stdout, re.MULTILINE)
                    if not running and exit_code:
                        result = subprocess.CompletedProcess(proc.args, int(exit_code.group(1)), proc.stdout, proc.stderr)
                    else:
                        time.sleep(min(0.1, remaining_timeout(deadline)))
                        continue
                return result
        finally:
            if not was_loaded:
                try:
                    self._run("bootout", target, check=False, timeout=LAUNCHD_CLEANUP_TIMEOUT)
                except Exception:
                    # Cleanup must not replace the pass's result or exception.
                    pass

    def _unload_when_idle(self, label: str, *, check: bool,
                          timeout: float) -> subprocess.CompletedProcess:
        target = f"{self.domain}/{label}"
        deadline = time.monotonic() + timeout
        while self.is_active(f"{label}.service") in ("active", "activating", "deactivating"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    [self.executable, "bootout", target], timeout,
                )
            time.sleep(min(0.1, remaining))
        return self._run("bootout", target, check=check)

    def stop(self, name: str, *, check: bool = True, timeout: float = 0) -> subprocess.CompletedProcess:
        label = self._label(name)
        if name.endswith(".timer"):
            return self._unload_when_idle(label, check=check, timeout=timeout)
        return self._run("bootout", f"{self.domain}/{label}", check=check)

    def enable(self, name: str, enabled: bool = True, *, now: bool = True,
               check: bool = True, timeout: float = 0) -> subprocess.CompletedProcess:
        label = self._label(name)
        if enabled:
            return self._run("bootstrap", self.domain, str(self._path(label)), check=check)
        if name.endswith(".timer"):
            return self._unload_when_idle(label, check=check, timeout=timeout)
        return self._run("bootout", f"{self.domain}/{label}", check=check)

    def status(self, name: str, *, timeout: float | None = None) -> ServiceStatus:
        deadline = time.monotonic() + timeout if timeout is not None else None
        label = self._label(name)
        body = None
        try:
            filename = f"{label}.plist"
            body = _definition_bodies(self.agent_dir, filename, deadline=deadline).get(filename)
            document = plistlib.loads((body or "").encode())
        except (OSError, plistlib.InvalidFileException):
            document = {}
        environment = document.get("EnvironmentVariables")
        if not isinstance(environment, dict):
            environment = {}
        proc = self._run("print", f"{self.domain}/{label}", check=False,
                         timeout=remaining_timeout(deadline) if deadline is not None else None)
        if proc.returncode:
            return ServiceStatus(
                "not-found",
                "inactive",
                "disabled" if body is not None else "not-found",
                environment=environment,
            )
        running = re.search(r"^\s*state\s*=\s*running\s*$", proc.stdout, re.MULTILINE) is not None
        pid_match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", proc.stdout, re.MULTILINE)
        scheduled = "StartInterval" in document
        if name.endswith(".timer"):
            disabled = self._run("print-disabled", self.domain, check=False,
                                 timeout=remaining_timeout(deadline) if deadline is not None else None)
            held = re.search(r'["\']?' + re.escape(label) + r'["\']?\s*=>\s*true', disabled.stdout)
            active = "inactive" if held else "active"
        elif running:
            active = "activating" if scheduled else "active"
        else:
            active = "inactive"
        argv = document.get("ProgramArguments")
        command = tuple(argv) if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv) else None
        return ServiceStatus(
            "loaded", active, "enabled", int(pid_match.group(1)) if pid_match else 0, command, environment,
        )

    def scheduled_jobs(self, names: set[str], *,
                       timeout: float | None = None) -> dict[str, ScheduledJobStatus]:
        timers = set(names)
        try:
            paths = self.agent_dir.glob("factory-*.plist")
            for path in paths:
                try:
                    document = plistlib.loads(path.read_bytes())
                except (OSError, plistlib.InvalidFileException):
                    continue
                if "StartInterval" in document:
                    timers.add(f"{path.stem}.timer")
        except OSError:
            pass
        result = {}
        deadline = time.monotonic() + timeout if timeout is not None else None
        for name in sorted(timers):
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if remaining is not None and remaining <= 0:
                break
            status = self.status(name.removesuffix(".timer") + ".service", timeout=remaining)
            result[name] = ScheduledJobStatus(
                unit_file_state=status.unit_file_state,
                active_state="active" if status.loaded else "inactive",
                service_active_state=status.active_state,
            )
        return result

    def is_active(self, name: str, *, timeout: float | None = None) -> str:
        return self.status(name, timeout=timeout).active_state

    def manager_status(self) -> str:
        proc = self._run("print", self.domain, check=False)
        return "running" if proc.returncode == 0 else (proc.stderr.strip() or f"exited {proc.returncode}")


def backend_from_config(config: Mapping[str, object], run: Runner) -> ServiceBackend:
    platform = config.get("platform")
    executable = config.get("executable")
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise ValueError("service backend executable must be absolute")
    if platform == "linux":
        unit_dir = config.get("unit_dir")
        if not isinstance(unit_dir, str) or not Path(unit_dir).is_absolute():
            raise ValueError("systemd user unit directory must be absolute")
        return SystemdUserBackend(run, executable=executable, unit_dir=Path(unit_dir))
    if platform == "macos":
        home, uid, path = config.get("home"), config.get("uid"), config.get("path")
        if not isinstance(home, str) or not Path(home).is_absolute() or not isinstance(uid, int):
            raise ValueError("launchd backend home and uid are invalid")
        if not isinstance(path, str):
            raise ValueError("launchd backend PATH is invalid")
        return LaunchdBackend(run, executable=executable, home=Path(home), uid=uid, raw_path=path)
    raise ValueError(f"unsupported service platform: {platform!r}")
