"""Retained service pass completions, independent of Factory's Python imports."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from district import host

# systemd's UNIT_STARTED (oneshot completion) and UNIT_FAILED message IDs.
UNIT_STARTED = "39f53479d3a045ac8e11786248231fbf"
UNIT_FAILED = "be02cf6855d2428ba40df7e9d022f03d"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")
END = re.compile(r"===\s*end\s+rc=(-?\d+)\b")

# Read on the owning host, so launchd paths/HOME never resolve on the controller.
# ponytail: inspect the last 4 MiB per log; rotated/older history is not evidence.
LAUNCHD_LOGS = """\
import json, pathlib, plistlib, sys
label, root = sys.argv[1:]
home = pathlib.Path.home()
paths = []
document = {}
plist = home / 'Library/LaunchAgents' / (label + '.plist')
try:
    document = plistlib.loads(plist.read_bytes())
    paths = [document[key] for key in ('StandardOutPath', 'StandardErrorPath')
             if isinstance(document.get(key), str)]
except FileNotFoundError:
    pass
error_path = document.get('StandardErrorPath')
if not paths:
    logs = pathlib.Path(root).expanduser() / '.factory/logs'
    paths = [str(logs / (label + '.stdout.log')), str(logs / (label + '.stderr.log')),
             str(home / 'Library/Logs' / (label + '.log'))]
    error_path = paths[1]
result = {}
for name in dict.fromkeys(paths):
    try:
        with pathlib.Path(name).expanduser().open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 4 * 1024 * 1024))
            if size > 4 * 1024 * 1024:
                stream.readline()
            result[name] = stream.read().decode(errors='replace')
    except FileNotFoundError:
        continue
print(json.dumps({'logs': result, 'stderr': result.get(error_path, '')}))
"""


@dataclass(frozen=True)
class PassResult:
    failed: bool
    at: str | None
    error: str | None = None


def _stamp(line: str) -> str | None:
    match = STAMP.search(line)
    if match:
        try:
            return datetime.fromisoformat(match[0].replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return None


def journal_passes(text: str, unit: str) -> list[PassResult]:
    passes = []
    error = None
    for line in text.splitlines():
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("journal record must be an object")
        message = item.get("MESSAGE")
        if item.get("UNIT", item.get("USER_UNIT")) == unit and item.get("MESSAGE_ID") in (UNIT_STARTED, UNIT_FAILED):
            timestamp = datetime.fromtimestamp(int(item["__REALTIME_TIMESTAMP"]) / 1_000_000, timezone.utc).isoformat()
            passes.append(PassResult(item["MESSAGE_ID"] == UNIT_FAILED, timestamp, error))
            error = None
        elif isinstance(message, str) and message.strip() and item.get("MESSAGE_ID") is None:
            error = message.strip().splitlines()[-1]
    return passes


def launchd_passes(text: str) -> list[PassResult]:
    passes = []
    started = None
    error = None
    for line in text.splitlines():
        match = END.search(line)
        if match:
            passes.append(PassResult(int(match[1]) != 0, started or _stamp(line), error))
            started, error = None, None
        elif "===" in line:
            started, error = _stamp(line), None
        elif line.strip():
            error = line.strip()
    return passes


def threshold(data: dict, name: str) -> int:
    defaults = data.get("defaults", {}) if name == "local" else host.remote_config(data, name).get("defaults", {})
    value = defaults.get("failed_pass_streak", host.DEFAULTS["failed_pass_streak"])
    if type(value) is not int or value < 1:
        raise host.DistrictError("[defaults].failed_pass_streak must be a positive integer")
    return value


def describe(alert: dict) -> str:
    detail = (f"{alert['host']} {alert['repo']}: last {alert['count']} factory passes failed; "
              f"first failure {alert['first_failed_at']}")
    return f"{detail}; {alert['last_error']}" if alert["last_error"] else detail


def _output(runner, argv: list[str], timeout: float) -> str:
    proc = runner(argv, timeout=timeout, quiet=True)
    if proc.returncode:
        raise OSError(proc.stderr.strip() or proc.stdout.strip() or "pass history unavailable")
    return proc.stdout


def _latest_pass_at(items: list[PassResult]) -> float:
    if not items:
        return float("-inf")
    # An undated completion cannot safely lose to an older, dated failure log.
    return datetime.fromisoformat(items[-1].at).timestamp() if items[-1].at else float("inf")


def _launchd_history(text: str) -> list[PassResult]:
    payload = json.loads(text)
    logs, stderr = payload["logs"], payload["stderr"]
    if not isinstance(logs, dict) or not all(isinstance(value, str) for value in logs.values()):
        raise ValueError("launchd logs must be an object of strings")
    if not isinstance(stderr, str):
        raise ValueError("launchd stderr must be a string")
    # stdout/stderr may be the same file; never count a pass twice.
    passes = max((launchd_passes(value) for value in logs.values()), key=_latest_pass_at, default=[])
    if passes and stderr.strip() and not END.search(stderr):
        passes[-1] = replace(passes[-1], error=stderr.strip().splitlines()[-1])
    return passes


def _history(data: dict, name: str, label: str, root: str, timeout: float) -> list[PassResult]:
    target = host.hosts(data).get(name, {})
    runner = host.run if name == "local" else host.remote_runner(name, target)
    platform = ("macos" if sys.platform == "darwin" else "linux") if name == "local" else target["platform"]
    if platform == "linux":
        output = _output(runner, ["journalctl", "--user", "-u", f"{label}.service", "--output=json",
                                 "--no-pager", "-n", "10000"], timeout)
        return journal_passes(output, f"{label}.service")
    return _launchd_history(_output(runner, ["python3", "-c", LAUNCHD_LOGS, label, root], timeout))


def _alert(passes: list[PassResult], count: int, name: str, slug: str) -> dict | None:
    recent = passes[-count:]
    if len(recent) != count or not all(item.failed and item.at for item in recent):
        return None
    return {"host": name, "repo": slug, "count": count,
            "first_failed_at": recent[0].at, "last_failed_at": recent[-1].at,
            "last_error": recent[-1].error}


def collect(data: dict, slug: str, *, skip: set[str] | None = None, timeout: float = 10) -> tuple[list[dict], list[dict]]:
    """Collect each assigned host, including non-primary hosts; never sample one exit N times."""
    alerts, gaps = [], []
    table = host.repos(data)[slug]
    label = host.unit_name(slug)
    for name in host.repo_hosts(table, slug):
        count = threshold(data, name)
        if name in (skip or ()):
            continue
        try:
            passes = _history(data, name, label, table["path"], timeout)
            if alert := _alert(passes, count, name, slug):
                alerts.append(alert)
        except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, ValueError,
                TypeError, KeyError, OverflowError) as exc:
            gaps.append({"host": name, "repo": slug, "error": str(exc),
                         "unreachable": isinstance(exc, host.RemoteHostError)})
    return alerts, gaps
