"""The host file `$XDG_CONFIG_HOME/agent-factory/config.toml` is the registry.

agent-factory reads `[defaults.*]` and `[repo."owner/name".*]` (host-owned
tables only); District keeps its own keys beside them: `[defaults]`
clone_dir / factory_source / min_package_age / max_failed_passes, and per repo
`path`, `disabled_at`, `disabled_reason`. A repo is managed iff its table has a `path`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import tomli_w

BASE_PORT = 8764  # first allocation is 8765, agent-factory's default
DEFAULTS = {
    "clone_dir": "~/dev/mikeroysoft",
    "factory_source": "~/dev/mikeroysoft/agent-factory",
    "min_package_age": "24h",
    "max_failed_passes": 10,
}


class DistrictError(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"district: {msg}")


def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "agent-factory" / "config.toml"


def unit_dir() -> Path:
    return path().parents[1] / "systemd" / "user"


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


def repo_table(data: dict, slug: str) -> dict:
    return data.setdefault("repo", {}).setdefault(slug, {})


def next_port(data: dict) -> int:
    ports = [t.get("dashboard", {}).get("port", 0) for t in data.get("repo", {}).values() if isinstance(t, dict)]
    return max([BASE_PORT, *ports]) + 1


SHARED_TABLES = ("triage", "workers", "review", "install", "gate")  # never dashboard: port is per-repo


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


def run(argv: list[str], cwd: Path | None = None, check: bool = False, quiet: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        if not quiet:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
        raise DistrictError(f"`{' '.join(argv)}` exited {proc.returncode}")
    return proc


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args], check=check, quiet=True)


def is_active(unit: str) -> str:
    return systemctl("is-active", unit, check=False).stdout.strip()
