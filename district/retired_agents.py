"""Discover and safely adopt hand-made Factory schedulers on their owning host."""
from __future__ import annotations

import os
import plistlib
import shlex
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from xml.parsers.expat import ExpatError

from district import host, systemd
from district.service import LaunchdBackend, _definition_bytes


def canonical_root(path: str, run_cmd) -> str:
    if not isinstance(run_cmd, host.RemoteRunner):
        return str(Path(os.path.expandvars(path)).expanduser().resolve())
    script = "import os,sys; print(os.path.realpath(os.path.expanduser(os.path.expandvars(sys.argv[1]))))"
    proc = run_cmd(["python3", "-c", script, path], timeout=10, quiet=True)
    if proc.returncode:
        raise OSError(proc.stderr.strip() or f"cannot resolve checkout {path}")
    return proc.stdout.strip()


def _dispatch_root(argv: list[str], working: str) -> str | None:
    """Recognize Factory commands, including the usual `cd repo && exec factory` wrapper."""
    if not argv or not all(isinstance(arg, str) for arg in argv):
        return None
    if Path(argv[0]).name in ("sh", "bash", "zsh") and len(argv) >= 3 and "c" in argv[1]:
        lexer = shlex.shlex(argv[2], posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            return None
        if tokens[:1] == ["cd"] and len(tokens) >= 4 and tokens[2] == "&&":
            directory = tokens[1]
            working = (str(Path(working) / directory) if working and not directory.startswith(("~", "$"))
                       else directory)
            tokens = tokens[3:]
        if tokens[:1] == ["exec"]:
            tokens = tokens[1:]
        return _dispatch_root(tokens, working)
    if Path(argv[0]).name == "env":
        argv = argv[1:]
        while argv and "=" in argv[0]:
            argv = argv[1:]
        return _dispatch_root(argv, working)
    if Path(argv[0]).name == "uv" and argv[1:2] == ["run"]:
        return _dispatch_root(argv[2:], working)
    command = argv[1:] if Path(argv[0]).name == "factory" else []
    if Path(argv[0]).name.startswith("python") and "-m" in argv:
        index = argv.index("-m")
        if argv[index + 1:index + 2] == ["factory"]:
            command = argv[index + 2:]
    if command[:1] not in (["dispatch"], ["pass"]):
        return None
    return working or None


def _settings(body: bytes) -> dict[str, str]:
    values = {}
    # Unit sections do not repeat the settings used here; honor the last assignment.
    for line in body.decode().replace("\\\n", "").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator:
            values[key] = value
    return values


def _definitions(backend, run_cmd):
    if isinstance(backend, LaunchdBackend):
        for name, body in _definition_bytes(backend.agent_dir, "*.plist").items():
            try:
                doc = plistlib.loads(body)
            except (plistlib.InvalidFileException, ValueError, ExpatError) as exc:
                if b"factory" in body.lower() or "factory" in name.lower():
                    raise OSError(f"invalid launchd plist {name}: {exc}") from exc
                continue
            if not isinstance(doc, dict):
                continue
            root = _dispatch_root(doc.get("ProgramArguments", []), doc.get("WorkingDirectory", ""))
            if root:
                yield {"label": doc.get("Label", name[:-6]), "root": root,
                       "files": [str(backend.agent_dir / name)], "timer": None}
        return
    directory = (run_cmd.path(Path(run_cmd.facts()["config_home"]) / "systemd/user")
                 if isinstance(run_cmd, host.RemoteRunner) else host.unit_dir())
    bodies = _definition_bytes(directory, "*.service")
    bodies.update(_definition_bytes(directory, "*.timer"))
    timers = {}
    for name, body in bodies.items():
        if name.endswith(".timer"):
            service = _settings(body).get("Unit", name[:-6] + ".service")
            timers.setdefault(service, []).append(name)
    for name, body in bodies.items():
        if not name.endswith(".service"):
            continue
        if b"factory" not in body:
            continue
        settings = _settings(body)
        working = settings.get("WorkingDirectory", "")
        words = systemd.words(working) if working else []
        root = None
        for line in body.decode().replace("\\\n", "").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key == "ExecStart":
                if not value:
                    root = None  # An empty assignment resets earlier commands.
                else:
                    argv = systemd.words(value.lstrip("-+!:@"))
                    root = _dispatch_root(argv, words[0] if words else "") or root
        if root:
            related = timers.get(name, [])
            yield {"label": name[:-8], "root": root,
                   "files": [str(directory / item) for item in [name, *related]],
                   "timer": related[0] if related else None, "timers": related}


def scan(backend, run_cmd) -> list[dict]:
    agents = []
    for agent in _definitions(backend, run_cmd):
        root = agent["root"]
        if not isinstance(backend, LaunchdBackend):
            home = (run_cmd.facts()["home"] if isinstance(run_cmd, host.RemoteRunner)
                    else str(Path.home()))
            root = root.replace("%%", "\0").replace("%h", home).replace("\0", "%")
        agent["root"] = canonical_root(root, run_cmd)
        status = backend.status(f"{agent['label']}.service", timeout=10)
        agent["busy"] = bool(status.main_pid) or status.active_state in ("active", "activating", "deactivating")
        if isinstance(backend, LaunchdBackend):
            agent["loaded"] = status.loaded
            agent["enabled"] = backend.status(
                f"{agent['label']}.timer", timeout=10).unit_file_state == "enabled"
        else:
            states = [backend.status(timer, timeout=10) for timer in agent["timers"]]
            agent["loaded"] = agent["busy"] or any(state.active_state == "active" for state in states)
            agent["enabled"] = status.unit_file_state == "enabled" or any(
                state.unit_file_state == "enabled" for state in states)
        agents.append(agent)
    return agents


def foreign(agents: list[dict], root: str, expected_label: str) -> list[dict]:
    owned = {expected_label, *(expected_label + suffix for suffix in
                             ("-dashboard", "-triage-probe", "-eval"))}
    return [agent for agent in agents if agent["root"] == root and agent["label"] not in owned]


def _command(backend, *args: str) -> None:
    proc = backend._run(*args, check=False, timeout=30)
    if proc.returncode:
        raise OSError(f"{' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip() or proc.returncode}")


def _file_command(run_cmd, script: str, *args: str) -> str:
    proc = run_cmd(["python3", "-c", script, *args], check=False, timeout=30, quiet=True)
    if proc.returncode:
        raise OSError(proc.stderr.strip() or "agent backup/restore failed")
    return proc.stdout.strip()


def _copy(run_cmd, source: str, destination: str) -> None:
    _file_command(run_cmd,
                  "import pathlib,shutil,sys; p=pathlib.Path(sys.argv[2]); "
                  "p.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(sys.argv[1],p)",
                  source, destination)


def _pause(backend, agent: dict) -> None:
    if isinstance(backend, LaunchdBackend):
        if agent["loaded"]:
            _command(backend, "disable", f"{backend.domain}/{agent['label']}")
    else:
        for timer in agent["timers"]:
            _command(backend, "disable", "--now", timer)


def _unload(backend, agent: dict, *, wait: bool = False) -> None:
    deadline = time.monotonic() + 120 if wait else 0
    while True:
        status = backend.status(f"{agent['label']}.service", timeout=10)
        if not (status.main_pid or status.active_state in ("active", "activating", "deactivating")):
            break
        if time.monotonic() >= deadline:
            raise OSError(f"agent {agent['label']} has a pass in flight; refusing to kill it; retry when idle")
        time.sleep(0.1)
    if isinstance(backend, LaunchdBackend):
        if status.loaded:
            _command(backend, "bootout", f"{backend.domain}/{agent['label']}")
    else:
        _command(backend, "disable", "--now", f"{agent['label']}.service")


def _resume(backend, agent: dict) -> None:
    if isinstance(backend, LaunchdBackend):
        if agent["loaded"]:
            if agent["enabled"]:
                _command(backend, "enable", f"{backend.domain}/{agent['label']}")
            if not backend.status(f"{agent['label']}.service", timeout=10).loaded:
                _command(backend, "bootstrap", backend.domain, agent["files"][0])
    else:
        _command(backend, "daemon-reload")
        names = agent["timers"] or [f"{agent['label']}.service"]
        for name in names:
            if agent["enabled"]:
                _command(backend, "enable", *(["--now"] if agent["loaded"] else []), name)
            elif agent["loaded"]:
                _command(backend, "start", name)


def _discard_replacement(backend, expected: str) -> None:
    """Remove a partial install, without treating absent units as teardown failures."""
    loaded = backend.status(f"{expected}.service", timeout=10).loaded
    timers = [] if isinstance(backend, LaunchdBackend) else [
        timer for timer in [f"{expected}.timer"] if backend.status(timer, timeout=10).loaded]
    replacement = {"label": expected, "loaded": loaded, "timers": timers}
    _pause(backend, replacement)
    if loaded:
        _unload(backend, replacement, wait=True)
    backend.remove(expected)


@contextmanager
def takeover(agents: list[dict], backend, run_cmd, expected: str, host_name: str):
    """Retire before the caller installs; on any failure restore the old scheduler."""
    if not agents:
        yield
        return
    labels = ", ".join(agent["label"] for agent in agents)
    prefix = f"host {host_name}: {labels} -> {expected}"
    if any(agent["busy"] for agent in agents):
        raise OSError(f"{prefix}: pass in flight; refusing to kill it; retry when idle")
    home = (run_cmd.facts()["home"] if isinstance(run_cmd, host.RemoteRunner) else str(Path.home()))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = f"{home}/.local/state/district/retired-agents/{timestamp}"
    backups = []
    paused = []
    installing = False
    try:
        for agent in agents:
            for path in agent["files"]:
                backup = f"{directory}/{Path(path).name}"
                _copy(run_cmd, path, backup)
                backups.append((path, backup))
                print(f"{prefix}: backed up {path} to {backup}")
        for agent in agents:
            paused.append(agent)
            _pause(backend, agent)
            _unload(backend, agent)
            for path in agent["files"]:
                _file_command(run_cmd, "import os,sys; os.unlink(sys.argv[1])", path)
            print(f"{prefix}: retired {agent['label']}")
        if not isinstance(backend, LaunchdBackend):
            _command(backend, "daemon-reload")
        installing = True
        yield
    except (Exception, SystemExit) as exc:
        print(f"{prefix}: adoption failed: {exc}; rollback: restoring retired agents")
        try:
            if installing:
                _discard_replacement(backend, expected)
            for path, backup in backups:
                _copy(run_cmd, backup, path)
            for agent in paused:
                _resume(backend, agent)
                print(f"{prefix}: restored {agent['label']} from {directory}")
            if installing and isinstance(backend, LaunchdBackend):
                # Restore the old scheduler before cleanup that only affects future retries.
                _command(backend, "enable", f"{backend.domain}/{expected}")
        except (Exception, SystemExit) as rollback:
            raise OSError(f"{prefix}: install/retirement failed ({exc}); rollback failed ({rollback}); backups: {directory}") from exc
        raise OSError(f"{prefix}: install/retirement failed ({exc}); old agent restored") from exc
