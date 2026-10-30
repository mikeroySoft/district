"""The host file `$XDG_CONFIG_HOME/factory/config.toml` is the registry.

Factory reads `[defaults.*]` and `[repo."owner/name".*]` (host-owned
tables only); District keeps its own keys beside them: `[defaults]`
clone_dir / factory_source / factory_ref (`latest-odd`, tag, or branch) /
factory_extras / min_package_age / max_failed_passes, `[defaults.engine]` ref /
sha / previous / installed_at (what engine reconciliation or `apply --upgrade` last installed), `[hosts.*]`
SSH / platform / root / optional Xcode version, and per repo `path`, `host`,
`toolchain`, `disabled_at`, `disabled_reason`. A repo is managed iff its table has a `path`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

import tomli_w
from district import service
from district.service import service_path

BASE_PORT = 8764  # first allocation is 8765, agent-factory's default
DEFAULTS = {
    "clone_dir": "~/dev/mikeroysoft",
    "factory_source": "~/dev/mikeroysoft/factory",
    "factory_extras": [],
    "min_package_age": "24h",
    "max_failed_passes": 10,
}


class DistrictError(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"district: {msg}")


def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "factory" / "config.toml"


def unit_dir() -> Path:
    return path().parents[1] / "systemd" / "user"


def operation_lock_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "district" / "apply.lock"


def load() -> dict:
    p = path()
    if not p.exists():
        return {}
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise DistrictError(f"{p}: {exc}") from exc
    if "port" in data.get("defaults", {}).get("dashboard", {}):
        raise DistrictError(f"{p}: [defaults.dashboard].port is not allowed; ports are allocated per repo")
    return data


def save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data))
    os.replace(tmp, p)


def default(data: dict, key: str):
    return data.get("defaults", {}).get(key, DEFAULTS[key])


def repos(data: dict) -> dict[str, dict]:
    return {slug: t for slug, t in data.get("repo", {}).items() if isinstance(t, dict) and "path" in t}


def hosts(data: dict) -> dict[str, dict]:
    configured = data.get("hosts", {})
    if not isinstance(configured, dict):
        raise DistrictError("[hosts] must be a table")
    for name, table in configured.items():
        if name == "local":
            raise DistrictError("[hosts.local] is reserved for this machine")
        if not isinstance(table, dict):
            raise DistrictError(f"[hosts.{name}] must be a table")
        if not isinstance(table.get("ssh"), str) or not table["ssh"].strip():
            raise DistrictError(f"[hosts.{name}].ssh must be a non-empty SSH target")
        if table.get("platform") not in ("linux", "macos"):
            raise DistrictError(f"[hosts.{name}].platform must be `linux` or `macos`")
        xcode_version = table.get("xcode_version")
        if xcode_version is not None and (
            not isinstance(xcode_version, str) or not xcode_version.strip()
        ):
            raise DistrictError(f"[hosts.{name}].xcode_version must be a non-empty string")
    return configured


def factory_host(data: dict, slug: str) -> str | dict:
    table = repos(data).get(slug)
    if table is None:
        raise DistrictError(f"{slug} is not registered (district add)")
    name = table.get("host", "local")
    if name == "local":
        return "local"
    configured = hosts(data)
    if not isinstance(name, str) or name not in configured:
        raise DistrictError(f"{slug}: host {name!r} is not defined in [hosts]")
    return configured[name]


def is_remote(data: dict, slug: str) -> bool:
    return factory_host(data, slug) != "local"


def repo_table(data: dict, slug: str) -> dict:
    return data.setdefault("repo", {}).setdefault(slug, {})


def next_port(data: dict) -> int:
    ports = [t.get("dashboard", {}).get("port", 0) for t in data.get("repo", {}).values() if isinstance(t, dict)]
    return max([BASE_PORT, *ports]) + 1


SHARED_TABLES = ("triage", "workers", "worker_wrap", "review", "manager", "install", "gate")  # never dashboard: port is per-repo


def dedupe(data: dict) -> list[str]:
    """Promote host values every registered repo agrees on into `[defaults]`; drop per-repo copies
    that equal the default. Returns `table.key` names promoted."""
    tables = list(repos(data).values())
    defaults = data.setdefault("defaults", {})
    promoted = []
    for name in SHARED_TABLES:
        keys = {k for t in tables for k in t.get(name, {})}
        for key in sorted(keys):
            vals = [t.get(name, {}).get(key, _MISSING) for t in tables]
            if key not in defaults.get(name, {}) and all(v == vals[0] for v in vals) and vals[0] is not _MISSING:
                defaults.setdefault(name, {})[key] = vals[0]
                promoted.append(f"{name}.{key}")
            default = defaults.get(name, {}).get(key, _MISSING)
            if default is _MISSING:
                continue
            for t in tables:
                if t.get(name, {}).get(key, _MISSING) == default:
                    del t[name][key]
                    if not t[name]:
                        del t[name]
    if not defaults:
        del data["defaults"]
    return promoted


_MISSING = object()


def unit_name(slug: str) -> str:
    """agent-factory's unit stem: `factory-<repo basename>`."""
    return f"factory-{slug.rsplit('/', 1)[-1]}"




def select(data: dict, slug: str | None) -> dict[str, dict]:
    """Registry entries to act on: all, or the one named (by slug or basename)."""
    all_repos = repos(data)
    if slug is None:
        return all_repos
    if slug in all_repos:
        return {slug: all_repos[slug]}
    hits = {s: t for s, t in all_repos.items() if s.rsplit("/", 1)[-1] == slug}
    if not hits:
        raise DistrictError(f"{slug} is not registered (district add)")
    return hits


# ---------------------------------------------------------------- subprocess


def run(argv: list[str], cwd: Path | None = None, check: bool = False, quiet: bool = False,
        timeout: float | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False, env=env)
    else:
        # Collector deadlines cover the command's process group, not just its
        # Python wrapper: inherited output pipes can belong to a surviving child.
        with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True, env=env) as child:
            try:
                stdout, stderr = child.communicate(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                raise
            proc = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        if not quiet:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
        raise DistrictError(f"`{' '.join(argv)}` exited {proc.returncode}")
    return proc


def _service_run(argv: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return run(argv, check=check, quiet=True)


def service_backend(target: str | dict = "local", *, run=None, **kwargs) -> service.ServiceBackend:
    """Select the service-manager adapter for the target host platform."""
    if target == "local":
        platform = "macos" if sys.platform == "darwin" else "linux"
    elif isinstance(target, dict):
        platform = target.get("platform")
    else:
        platform = None
    runner = _service_run if run is None else run
    if platform == "linux":
        return service.SystemdUserBackend(runner, unit_dir=unit_dir(), **kwargs)
    if platform == "macos":
        return service.LaunchdBackend(runner, **kwargs)
    raise DistrictError(f"unsupported service platform: {platform!r}")


