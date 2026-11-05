"""Validate Factory's owning-host scheduler toolchain before activation."""

from __future__ import annotations

import copy
import plistlib
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from xml.parsers.expat import ExpatError

from district import host, systemd

UNIT_HEADER = re.compile(r"(?m)^# ([^\n]+)\n")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
FIXED_TOOLS = ("factory", "gh", "git", "uv", "python3")
TOOLCHAINS = {"macos-xcode": ("cargo", "xcodegen", "swift", "xcodebuild")}
VERSION_FLAGS = {tool: "--version" for tool in (
    *FIXED_TOOLS, *TOOLCHAINS["macos-xcode"], "rustc", "bun", "node", "omp",
    "npm", "npx", "pnpm", "yarn", "python", "go", "make", "cmake",
)}
VERSION_FLAGS["xcodebuild"] = "-version"
VERSION_FLAGS["go"] = "version"
PROBE_TIMEOUT = 10
READ_MISSING = 44
_RESOLVE = 'case "$1" in */*) [ -f "$1" ] && [ -x "$1" ] && printf "%s\\n" "$1";; *) command -v "$1";; esac'
_READ_CONFIG = 'if [ ! -e "$1" ]; then exit 44; fi; exec /bin/cat "$1"'


def _row(status: str, label: str, detail: str) -> dict[str, str]:
    return {"status": status, "label": label, "detail": detail}


def _detail(proc: subprocess.CompletedProcess, command: str) -> str:
    output = ((proc.stderr or "").strip() or (proc.stdout or "").strip()).splitlines()
    return output[-1] if output else f"{command} exited {proc.returncode}"


def _exception(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timed out after {exc.timeout}s"
    return str(exc) or type(exc).__name__


def rendered_units(output: str) -> dict[str, str]:
    """Split `factory install --print` output into its `# filename` sections."""
    matches = list(UNIT_HEADER.finditer(output))
    return {
        match.group(1): output[
            match.end():(matches[index + 1].start() if index + 1 < len(matches) else None)
        ].removesuffix("\n")
        for index, match in enumerate(matches)
    }


def install_environment(run_cmd) -> dict[str, str] | None:
    """Environment needed while rendering/installing on a remote owning host."""
    if not isinstance(run_cmd, host.RemoteRunner):
        return None
    facts = run_cmd.facts()
    return {"PATH": host.service_path(facts["path"], home=Path(facts["home"]))}


def _default_home(run_cmd) -> str:
    if isinstance(run_cmd, host.RemoteRunner):
        facts = run_cmd.facts()
        home = facts.get("home")
        if not isinstance(home, str) or not home:
            raise ValueError("owning host returned no HOME")
        return home
    return str(Path.home())


def scheduler_environment(rendered: str, run_cmd=None) -> dict[str, str]:
    """Return the rendered dispatch PATH/HOME, defaulting only HOME on its owner."""
    units = rendered_units(rendered)
    selected = []
    for filename, body in units.items():
        if not filename.startswith("factory-") or not filename.endswith((".service", ".plist")):
            continue
        stem, _, extension = filename.rpartition(".")
        if any(stem.endswith(suffix) and f"{stem.removesuffix(suffix)}.{extension}" in units
               for suffix in ("-dashboard", "-triage-probe", "-eval")):
            continue
        selected.append((filename, body))
    if len(selected) != 1:
        reason = "no rendered dispatch service" if not selected else "multiple rendered dispatch services"
        raise ValueError(reason)
    filename, body = selected[0]
    if filename.endswith(".plist"):
        try:
            values = plistlib.loads(body.encode()).get("EnvironmentVariables", {})
        except (ValueError, TypeError, AttributeError, plistlib.InvalidFileException, ExpatError) as exc:
            raise ValueError(f"invalid {filename}: {exc}") from exc
        if not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                               for key, value in values.items()):
            raise ValueError(f"invalid {filename} EnvironmentVariables")
        environment = dict(values)
    else:
        environment: dict[str, str] = {}
        for line in body.splitlines():
            if not line.startswith("Environment="):
                continue
            try:
                assignments = systemd.words(line.removeprefix("Environment="))
            except ValueError as exc:
                raise ValueError(f"invalid {filename} Environment: {exc}") from exc
            for assignment in assignments:
                if "=" not in assignment:
                    raise ValueError(f"invalid {filename} Environment assignment {assignment!r}")
                key, value = assignment.split("=", 1)
                if ENV_NAME.fullmatch(key) is None:
                    raise ValueError(f"invalid {filename} Environment name {key!r}")
                environment[key] = value.replace("%%", "%")
    path = environment.get("PATH")
    if not isinstance(path, str) or not path:
        raise ValueError(f"{filename}: no Environment PATH")
    home = environment.get("HOME")
    if home is None:
        home = _default_home(run_cmd)
    if not isinstance(home, str) or not home:
        raise ValueError(f"{filename}: invalid Environment HOME")
    return {"PATH": path, "HOME": home}


def _runner(data: dict, name: str, run_cmd):
    if run_cmd is not None:
        return run_cmd
    if name == "local":
        return host.run
    return host.remote_runner(name, host.hosts(data)[name])


def _isolated(environment: dict[str, str]) -> list[str]:
    return ["/usr/bin/env", "-i", f"PATH={environment['PATH']}", f"HOME={environment['HOME']}"]


def _argv(value, source: str, rows: list[dict[str, str]]) -> list[str] | None:
    try:
        if isinstance(value, str):
            result = shlex.split(value)
        elif isinstance(value, list) and all(isinstance(part, str) for part in value):
            result = value
        else:
            raise ValueError("must be an argv array or shell-style string")
    except ValueError as exc:
        rows.append(_row("FAIL", source, f"invalid command: {exc}"))
        return None
    if not result or not result[0]:
        rows.append(_row("FAIL", source, "invalid command: missing executable"))
        return None
    if result[0].startswith("-"):
        rows.append(_row("FAIL", source, f"invalid executable {result[0]!r}"))
        return None
    return result


def _metadata(runner, root: Path, environment: dict[str, str], name: str) -> tuple[dict, list[dict[str, str]]]:
    argv = [*_isolated(environment), "/bin/sh", "-c", _READ_CONFIG, "district-preflight", ".factory.toml"]
    try:
        proc = runner(argv, cwd=root, timeout=PROBE_TIMEOUT, quiet=True)
        if proc.returncode == READ_MISSING:
            return {}, []
        if proc.returncode:
            return {}, [_row("FAIL", f"host {name} .factory.toml", _detail(proc, "read .factory.toml"))]
        document = tomllib.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, host.RemoteHostError) as exc:
        return {}, [_row("FAIL", f"host {name} .factory.toml", f"cannot read metadata: {_exception(exc)}")]
    except (AttributeError, TypeError, tomllib.TOMLDecodeError) as exc:
        return {}, [_row("FAIL", f"host {name} .factory.toml", f"invalid metadata: {exc}")]
    return document, []


def _effective(data: dict, name: str, table: dict) -> dict:
    if name == "local":
        defaults = {key: value for key, value in data.get("defaults", {}).items() if key != "engine"}
    else:
        defaults = host.remote_config(data, name).get("defaults", {})
    config = copy.deepcopy(defaults)
    host._merge(config, {key: copy.deepcopy(value) for key, value in table.items() if key not in host.HOST_KEYS})
    return config


def _add(requirements: dict[str, list[str]], binary: str, source: str) -> None:
    sources = requirements.setdefault(binary, [])
    if source not in sources:
        sources.append(source)


def _requirements(data: dict, name: str, table: dict, metadata: dict,
                  rows: list[dict[str, str]]) -> dict[str, list[str]]:
    requirements: dict[str, list[str]] = {}
    for binary in FIXED_TOOLS:
        _add(requirements, binary, "scheduler")

    profile = table.get("toolchain")
    if profile is not None:
        if not isinstance(profile, str) or not profile:
            rows.append(_row("FAIL", f"host {name} toolchain", "profile must be a non-empty string"))
        elif profile not in TOOLCHAINS:
            rows.append(_row("FAIL", f"host {name} toolchain", f"unknown profile {profile!r}"))
        else:
            platform = ("macos" if sys.platform == "darwin" else "linux") if name == "local" else \
                data.get("hosts", {}).get(name, {}).get("platform")
            if platform != "macos":
                rows.append(_row("FAIL", f"host {name} {profile}",
                                 f"requires platform macos, found {platform}"))
            else:
                for binary in TOOLCHAINS[profile]:
                    _add(requirements, binary, profile)

    try:
        effective = _effective(data, name, table)
    except (AttributeError, KeyError, TypeError, ValueError, host.DistrictError) as exc:
        rows.append(_row("FAIL", f"host {name} metadata", str(exc)))
        effective = {}
    for section in ("review", "manager"):
        config = effective.get(section, {})
        if config is None:
            continue
        if not isinstance(config, dict):
            rows.append(_row("FAIL", f"defaults.{section}", "must be a table"))
            continue
        if "command" in config and (command := _argv(config["command"], f"defaults.{section}", rows)):
            _add(requirements, command[0], f"defaults.{section}")

    gate = metadata.get("gate", {})
    if not isinstance(gate, dict):
        rows.append(_row("FAIL", f"host {name} .factory.toml gate", "[gate] must be a table"))
        return requirements
    checks = gate.get("check", [])
    if not isinstance(checks, list):
        rows.append(_row("FAIL", f"host {name} .factory.toml gate.check", "must be an array of tables"))
        return requirements
    for index, item in enumerate(checks):
        if not isinstance(item, dict):
            rows.append(_row("FAIL", f"gate.check[{index}]", "must be a table"))
            continue
        source = item.get("name")
        if not isinstance(source, str) or not source:
            rows.append(_row("FAIL", f"gate.check[{index}]", "name must be a non-empty string"))
            source = f"gate.check[{index}]"
        if "run" not in item:
            rows.append(_row("FAIL", source, "invalid command: missing run"))
            continue
        if command := _argv(item["run"], source, rows):
            _add(requirements, command[0], source)
    return requirements


def check(data: dict, name: str, slug: str, table: dict, rendered: str, *, run_cmd=None) -> list[dict[str, str]]:
    """Check one rendered scheduler environment on the host that owns it."""
    rows: list[dict[str, str]] = []
    try:
        runner = _runner(data, name, run_cmd)
        if isinstance(runner, host.RemoteRunner):
            python = runner.python(timeout=PROBE_TIMEOUT)
            rows.append(_row("PASS", f"host {name} remote helper python3",
                             f"{python}: Python {runner.python_version} (requires >=3.11)"))
        environment = scheduler_environment(rendered, runner)
    except (OSError, TypeError, ValueError, KeyError, host.DistrictError, host.RemoteHostError,
            subprocess.TimeoutExpired) as exc:
        return [_row("FAIL", f"host {name} {slug} scheduler environment", _exception(exc))]
    try:
        root = Path(table["path"])
    except (KeyError, TypeError, ValueError) as exc:
        return [_row("FAIL", f"host {name} {slug} metadata", f"invalid repo path: {_exception(exc)}")]
    metadata, metadata_rows = _metadata(runner, root, environment, name)
    rows += metadata_rows
    requirements = _requirements(data, name, table, metadata, rows)
    prefix = _isolated(environment)
    for binary, sources in requirements.items():
        source = ", ".join(sources)
        label = f"host {name} scheduler tool {binary}"
        resolve = [*prefix, "/bin/sh", "-c", _RESOLVE, "district-preflight", binary]
        try:
            proc = runner(resolve, cwd=root, timeout=PROBE_TIMEOUT, quiet=True)
            returncode = proc.returncode
            stdout = proc.stdout or ""
        except (AttributeError, OSError, subprocess.TimeoutExpired, host.RemoteHostError) as exc:
            rows.append(_row(
                "FAIL", label,
                f"cannot resolve for {source} in PATH={environment['PATH']}: {_exception(exc)}",
            ))
            continue
        resolved = stdout.strip().splitlines()[0] if returncode == 0 and stdout.strip() else None
        if resolved is None:
            detail = _detail(proc, f"resolve {binary}") if (proc.stderr or proc.stdout or "").strip() else "not executable"
            rows.append(_row("FAIL", label,
                             f"{detail}; required by {source}; PATH={environment['PATH']}"))
            continue
        if binary not in VERSION_FLAGS:
            rows.append(_row("WARN", label, f"resolved {resolved} for {source}; unknown tool version probe skipped"))
            continue
        flag = VERSION_FLAGS[binary]
        probe = [*prefix, resolved, flag]
        try:
            version = runner(probe, cwd=root, timeout=PROBE_TIMEOUT, quiet=True)
            returncode = version.returncode
            detail = ((version.stdout or "").strip() or (version.stderr or "").strip()).splitlines()
        except (AttributeError, OSError, subprocess.TimeoutExpired, host.RemoteHostError) as exc:
            rows.append(_row("WARN", label, f"resolved {resolved} for {source}; version probe {_exception(exc)}"))
            continue
        if returncode or not detail:
            failure = _detail(version, f"{binary} {flag}") if returncode else f"{binary} {flag} returned no version"
            rows.append(_row("WARN", label, f"resolved {resolved} for {source}; {failure}"))
        else:
            rows.append(_row("PASS", label, f"{source}: {resolved}: {detail[0]}"))
    return rows


def render(data: dict, name: str, slug: str, table: dict, *, run_cmd=None, env=None,
           cli="factory") -> list[dict[str, str]]:
    """Render with the public Factory CLI, then check the owning-host scheduler environment."""
    label = f"host {name} {slug} factory install --print"
    try:
        runner = _runner(data, name, run_cmd)
        root = Path(table["path"])
        argv = [str(cli), "install", "--print"]
        proc = runner(argv, cwd=root, env=env, quiet=True)
        returncode = proc.returncode
        stdout = proc.stdout
    except (AttributeError, OSError, TypeError, ValueError, KeyError, host.DistrictError,
            host.RemoteHostError, subprocess.TimeoutExpired) as exc:
        return [_row("FAIL", label, _exception(exc))]
    if returncode:
        return [_row("FAIL", label, _detail(proc, "factory install --print"))]
    if not isinstance(stdout, str):
        return [_row("FAIL", label, "returned invalid output")]
    return check(data, name, slug, table, stdout, run_cmd=runner)
