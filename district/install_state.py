"""Fast local install and timer state, with one cached release lookup."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from district import __version__, host

SCHEMA_VERSION = 2
SCHEMA_PATH = Path(__file__).with_name("install-state.schema.json")
UNKNOWN = "unknown"
CACHE_SECONDS = 3600
LOCAL_TIMEOUT = 0.5
RELEASE_TIMEOUT = 1.0
SHA = re.compile(r"^[0-9a-f]{40}$")
PINNED_REF = re.compile(r"^(?:[0-9a-f]{40}|v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
TOOL_VERSION = re.compile(r"^(district|factory)\s+v?([^\s]+)", re.MULTILINE)
RELEASE_QUERY = """query {
  district: repository(owner: "mikeroySoft", name: "district") {
    latestRelease { tagName tagCommit { oid } }
  }
  factory: repository(owner: "mikeroySoft", name: "factory") {
    latestRelease { tagName tagCommit { oid } }
  }
}"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "district" / "install-state.json"


def release_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "district" / "releases.json"


def unknown_releases() -> dict[str, dict[str, str]]:
    return {name: {"tag": UNKNOWN, "sha": UNKNOWN} for name in ("district", "factory")}


def _release(value: object) -> dict[str, str]:
    release = value if isinstance(value, dict) else {}
    tag = release.get("tagName")
    commit = release.get("tagCommit")
    sha = commit.get("oid") if isinstance(commit, dict) else None
    return {
        "tag": tag if isinstance(tag, str) and tag else UNKNOWN,
        "sha": sha.lower() if isinstance(sha, str) and SHA.fullmatch(sha.lower()) else UNKNOWN,
    }


def _fetch_releases() -> dict[str, dict[str, str]]:
    proc = host.run(["gh", "api", "graphql", "-f", f"query={RELEASE_QUERY}"], timeout=RELEASE_TIMEOUT)
    if proc.returncode:
        raise OSError((proc.stderr or proc.stdout).strip() or f"gh exited {proc.returncode}")
    payload = json.loads(proc.stdout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("release response has no data")
    return {name: _release(data.get(name, {}).get("latestRelease")) for name in ("district", "factory")}


def load_releases() -> dict[str, dict[str, str]]:
    cache = release_cache_path()
    try:
        if time.time() - cache.stat().st_mtime < CACHE_SECONDS:
            value = json.loads(cache.read_text())
            if all(isinstance(value.get(name), dict) for name in ("district", "factory")):
                return value
    except (OSError, ValueError, TypeError):
        pass
    try:
        value = _fetch_releases()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return unknown_releases()
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(value, sort_keys=True) + "\n")
        os.replace(tmp, cache)
    except OSError:
        pass
    return value


def tool_versions() -> dict[str, str]:
    try:
        proc = host.run(["uv", "tool", "list", "--show-version-specifiers"], timeout=LOCAL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    versions = {name: version for name, version in TOOL_VERSION.findall(proc.stdout)} if proc and not proc.returncode else {}
    versions.setdefault("district", __version__)
    versions.setdefault("factory", UNKNOWN)
    return versions


def _timer_names(data: dict) -> list[str]:
    names = {"district-apply.timer"}
    try:
        names.update(f"{host.unit_name(slug)}.timer" for slug in host.repos(data) if not host.is_remote(data, slug))
    except (KeyError, TypeError, SystemExit):
        pass
    try:
        names.update(path.name for path in host.unit_dir().glob("factory-*.timer"))
    except OSError:
        pass
    return sorted(names)


def _show_blocks(output: str) -> dict[str, dict[str, str]]:
    result = {}
    for raw in output.strip().split("\n\n") if output.strip() else []:
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if values.get("Id"):
            result[values["Id"]] = values
    return result


def timer_states(data: dict | None = None) -> dict[str, dict]:
    data = data if isinstance(data, dict) else _safe_config()
    timers = _timer_names(data)
    services = [name.removesuffix(".timer") + ".service" for name in timers]
    properties = "Id,UnitFileState,ActiveState,LastTriggerUSec,NextElapseUSecRealtime"
    try:
        proc = host.run(["systemctl", "--user", "show", *timers, *services, f"--property={properties}"],
                        timeout=LOCAL_TIMEOUT)
        blocks = _show_blocks(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, SystemExit):
        blocks = {}
    result = {}
    for name, service in zip(timers, services):
        timer = blocks.get(name, {})
        active = blocks.get(service, {}).get("ActiveState")
        result[name] = {
            "enabled": timer.get("UnitFileState") or UNKNOWN,
            "active": timer.get("ActiveState") or UNKNOWN,
            "last_trigger": timer.get("LastTriggerUSec") or UNKNOWN,
            "next_trigger": timer.get("NextElapseUSecRealtime") or UNKNOWN,
            "service_activating": active == "activating" if active else UNKNOWN,
        }
    return result


def commit_counts(source: object, release_sha: str, installed_sha: str) -> tuple[int, int] | None:
    if not isinstance(source, str) or release_sha == UNKNOWN or installed_sha == UNKNOWN:
        return None
    try:
        proc = host.run(["git", "rev-list", "--left-right", "--count", f"{release_sha}...{installed_sha}"],
                        cwd=Path(os.path.expanduser(source)), timeout=LOCAL_TIMEOUT)
        behind, ahead = (int(value) for value in proc.stdout.split())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return (ahead, behind) if proc.returncode == 0 else None


def config_sha() -> str:
    try:
        return hashlib.sha256(host.path().read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return UNKNOWN


def _safe_config() -> dict:
    try:
        return host.load()
    except (OSError, SystemExit):
        return {}


def _engine_sha(data: dict) -> str:
    value = data.get("defaults", {}).get("engine", {}).get("sha")
    return value if isinstance(value, str) and SHA.fullmatch(value) else UNKNOWN


def unknown_last_apply() -> dict:
    return {
        "started_at": UNKNOWN, "finished_at": UNKNOWN, "exit_status": UNKNOWN,
        "disabled": [], "reenabled": [], "deferred": [],
        "engine_change": {"from": UNKNOWN, "to": UNKNOWN},
        "config_sha_before": UNKNOWN, "config_sha_after": UNKNOWN,
    }


def read_record() -> dict:
    try:
        value = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION else {}


def collect(*, data: dict | None = None, releases: dict | None = None,
            timers: dict | None = None, last_apply: dict | None = None,
            versions: dict[str, str] | None = None) -> dict:
    data = data if isinstance(data, dict) else _safe_config()
    releases = releases if isinstance(releases, dict) else load_releases()
    versions = versions if isinstance(versions, dict) else tool_versions()
    district_release = releases.get("district", {})
    factory_release = releases.get("factory", {})
    installed_sha = _engine_sha(data)
    latest_sha = factory_release.get("sha", UNKNOWN)
    counts = commit_counts(data.get("defaults", {}).get("factory_source"), latest_sha, installed_sha)
    factory_ref = data.get("defaults", {}).get("factory_ref")
    configured_ref = data.get("defaults", {}).get("engine", {}).get("ref")
    if not isinstance(configured_ref, str) or not configured_ref:
        configured_ref = factory_ref if isinstance(factory_ref, str) and factory_ref else UNKNOWN
    policy_ref = factory_ref if isinstance(factory_ref, str) else configured_ref
    if policy_ref == "latest-odd":
        policy = "latest-odd-tag"
    elif isinstance(policy_ref, str) and PINNED_REF.fullmatch(policy_ref):
        policy = "pinned"
    else:
        policy = "main-green-ci"
    latest_district = district_release.get("tag", UNKNOWN)
    up_to_date = (versions["district"].lstrip("v") == latest_district.lstrip("v")
                  if latest_district != UNKNOWN else UNKNOWN)
    holds = []
    for slug, table in sorted(host.repos(data).items()):
        if table.get("disabled_at"):
            holds.append({
                "factory": slug, "timer": f"{host.unit_name(slug)}.timer",
                "disabled_at": str(table["disabled_at"]),
                "reason": str(table.get("disabled_reason") or "disabled by District"),
            })
    previous = read_record().get("last_apply")
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": stamp(now()),
        "host": socket.gethostname(),
        "district": {
            "installed_version": versions["district"],
            "latest_release": latest_district,
            "up_to_date": up_to_date,
        },
        "engine": {
            "installed_version": versions["factory"],
            "installed_sha": installed_sha,
            "configured_ref": configured_ref,
            "latest_release_tag": factory_release.get("tag", UNKNOWN),
            "latest_release_sha": latest_sha,
            "commits_ahead": counts[0] if counts else UNKNOWN,
            "commits_behind": counts[1] if counts else UNKNOWN,
            "policy": policy,
        },
        "timers": timers if isinstance(timers, dict) else timer_states(data),
        "last_apply": last_apply if isinstance(last_apply, dict) else (
            previous if isinstance(previous, dict) else unknown_last_apply()),
        "holds": holds,
    }


def write_record(document: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


@dataclass
class ApplyRun:
    started_at: str
    config_sha_before: str
    engine_before: str
    timers_before: dict[str, dict] = field(default_factory=dict)
    observed: bool = False
    disabled: set[str] = field(default_factory=set)
    reenabled: set[str] = field(default_factory=set)
    deferred: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def start(cls) -> "ApplyRun":
        data = _safe_config()
        previous = read_record().get("last_apply", {}).get("deferred", [])
        deferred = {
            item["target"]: item
            for item in previous
            if isinstance(item, dict)
            and all(isinstance(item.get(key), str) for key in ("target", "reason", "since"))
        }
        return cls(stamp(now()), config_sha(), _engine_sha(data), deferred=deferred)

    def observe(self, data: dict) -> None:
        if not self.observed:
            self.timers_before = timer_states(data)
            self.observed = True

    def defer(self, target: str, reason: str) -> None:
        since = self.deferred.get(target, {}).get("since", stamp(now()))
        self.deferred[target] = {"target": target, "reason": reason, "since": since}

    def clear_deferred(self, target: str) -> None:
        self.deferred.pop(target, None)


    def finish(self, exit_status: int) -> None:
        data = _safe_config()
        after = timer_states(data)
        disabled = set(self.disabled)
        reenabled = set(self.reenabled)
        if self.observed:
            for name in set(self.timers_before) | set(after):
                before_active = self.timers_before.get(name, {}).get("active")
                after_active = after.get(name, {}).get("active")
                if not before_active or not after_active or UNKNOWN in (before_active, after_active):
                    continue
                if before_active == "active" and after_active != "active":
                    disabled.add(name)
                if before_active != "active" and after_active == "active":
                    reenabled.add(name)
        last_apply = {
            "started_at": self.started_at,
            "finished_at": stamp(now()),
            "exit_status": exit_status,
            "disabled": sorted(disabled),
            "reenabled": sorted(reenabled),
            "deferred": sorted(self.deferred.values(), key=lambda item: item["target"]),
            "engine_change": {"from": self.engine_before, "to": _engine_sha(data)},
            "config_sha_before": self.config_sha_before,
            "config_sha_after": config_sha(),
        }
        write_record(collect(data=data, timers=after, last_apply=last_apply))


def _summary(document: dict) -> str:
    district = document["district"]
    engine = document["engine"]
    lines = [
        f"Observed {document['observed_at']} on {document['host']}",
        f"District {district['installed_version']} (latest {district['latest_release']}, up to date {district['up_to_date']})",
        f"Factory {engine['installed_version']} at {engine['installed_sha']} (ref {engine['configured_ref']}, latest {engine['latest_release_tag']})",
    ]
    for name, timer in sorted(document["timers"].items()):
        lines.append(f"{name}: {timer['active']}, {timer['enabled']}; next {timer['next_trigger']}; service activating {timer['service_activating']}")
    reenabled = document["last_apply"].get("reenabled") or []
    if reenabled:
        lines.append(f"WARNING: last apply re-enabled {', '.join(reenabled)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="district install-state", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    document = collect()
    print(json.dumps(document, indent=2, sort_keys=True) if args.json else _summary(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
