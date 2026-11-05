"""Retained service pass completions, independent of Factory's Python imports."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from district import host

# systemd's UNIT_STARTED (oneshot completion) and UNIT_FAILED message IDs.
UNIT_STARTED = "39f53479d3a045ac8e11786248231fbf"
UNIT_FAILED = "be02cf6855d2428ba40df7e9d022f03d"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")
END = re.compile(r"===\s*end\s+rc=(-?\d+)\b")

# Read paths and launchctl on the owning host, never on the controller.
# ponytail: inspect the last 4 MiB per log; rotated/older history is not evidence.
LAUNCHD_LOGS = """\
import json, os, pathlib, plistlib, re, subprocess, sys
expected = json.loads(sys.argv[1])
home = pathlib.Path.home()
domain = 'gui/' + str(os.getuid())
def command(*args):
    try:
        p = subprocess.run(['launchctl', *args], capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except OSError as exc:
        return 127, '', str(exc)
rc, disabled, error = command('print-disabled', domain)
jobs = {}
errors = [error or disabled] if rc else []
for plist in (home / 'Library/LaunchAgents').glob('*.plist'):
    try:
        doc = plistlib.loads(plist.read_bytes())
        label = doc.get('Label', plist.stem)
        if 'factory' in label or 'factory' in str(doc.get('ProgramArguments', [])):
            jobs[label] = (doc, plist.stat().st_mtime)
    except (OSError, ValueError) as exc:
        errors.append(str(plist) + ': ' + str(exc))
rc, listing, error = command('list')
if rc:
    errors.append(error or listing or 'launchctl list unavailable')
else:
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 3 and 'factory' in fields[-1]:
            jobs.setdefault(fields[-1], ({}, None))
for label in expected:
    if not any(label == actual or actual.endswith('.' + label) for actual in jobs):
        jobs[label] = ({}, None)
result = []
for label, (doc, installed) in jobs.items():
    root = doc.get('WorkingDirectory') or next(
        (root for stem, root in expected.items() if label == stem or label.endswith('.' + stem)), '')
    paths = [doc[key] for key in ('StandardOutPath', 'StandardErrorPath') if isinstance(doc.get(key), str)]
    error_path = doc.get('StandardErrorPath')
    if not paths:
        logs = pathlib.Path(root).expanduser() / '.factory/logs'
        stem = next((stem for stem in expected if label == stem or label.endswith('.' + stem)), label)
        paths = [str(logs / (stem + '.stdout.log')), str(logs / (stem + '.stderr.log')),
                 str(home / 'Library/Logs' / (stem + '.log'))]
        error_path = paths[1]
    logs, mtimes, gaps = {}, {}, []
    for name in dict.fromkeys(paths):
        try:
            with pathlib.Path(name).expanduser().open('rb') as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - 4 * 1024 * 1024))
                if size > 4 * 1024 * 1024:
                    stream.readline()
                logs[name] = stream.read().decode(errors='replace')
                mtimes[name] = os.fstat(stream.fileno()).st_mtime
        except FileNotFoundError:
            continue
        except OSError as exc:
            gaps.append(str(exc))
    rc, status, error = command('print', domain + '/' + label)
    if rc:
        gaps.append(error or 'launchctl print unavailable: ' + label)
    held = re.search(r'["\\x27]?' + re.escape(label) + r'["\\x27]?\\s*=>\\s*true', disabled)
    result.append({'label': label, 'root': root, 'logs': logs, 'mtimes': mtimes,
                   'stderr': logs.get(error_path, ''), 'status': status,
                   'enabled': not bool(held) and not doc.get('Disabled', False) and
                              any(key in doc for key in ('StartInterval', 'StartCalendarInterval', 'KeepAlive')),
                   'installed_at': installed, 'errors': gaps})
print(json.dumps({'jobs': result, 'errors': errors}))
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
    native = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(\S+)", line)
    if native:
        try:
            day = datetime.fromisoformat(native[1]).strftime("%d %b %Y")
            parsed = parsedate_to_datetime(f"{day} {native[2]} {native[3]}")
            return parsed.isoformat() if parsed.tzinfo else None
        except (ValueError, TypeError):
            return None
    # launchd shell jobs commonly use `date`, including PST/PDT rather than ISO.
    plain = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\d{4}", line)
    try:
        parsed = parsedate_to_datetime(plain[0] if plain else line)
        return parsed.isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _error(line: str) -> bool:
    return bool(re.search(r"error|exception|failed|fatal", line, re.I))


def journal_passes(text: str, unit: str) -> tuple[list[PassResult], str | None]:
    passes = []
    error = None
    for line in text.splitlines():
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("journal record must be an object")
        message = item.get("MESSAGE")
        if item.get("UNIT", item.get("USER_UNIT")) == unit and item.get("MESSAGE_ID") in (UNIT_STARTED, UNIT_FAILED):
            timestamp = datetime.fromtimestamp(int(item["__REALTIME_TIMESTAMP"]) / 1_000_000, timezone.utc).isoformat()
            failed = (item["MESSAGE_ID"] == UNIT_FAILED or item.get("RESULT", "success") != "success"
                      or str(item.get("ExecMainStatus", item.get("EXIT_STATUS", "0"))) != "0")
            passes.append(PassResult(failed, timestamp, error))
            error = None
        elif isinstance(message, str) and message.strip() and item.get("MESSAGE_ID") is None:
            for detail in message.strip().splitlines():
                if detail.strip() and (error is None or (_error(detail) and not _error(error))):
                    error = detail.strip()
    return passes, error


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
        elif line.strip() and (error is None or (_error(line) and not _error(error))):
            error = line.strip()
    return passes


def threshold(data: dict, name: str) -> int:
    defaults = data.get("defaults", {}) if name == "local" else host.remote_config(data, name).get("defaults", {})
    value = defaults.get("failed_pass_streak", host.DEFAULTS["failed_pass_streak"])
    if type(value) is not int or value < 1:
        raise host.DistrictError("[defaults].failed_pass_streak must be a positive integer")
    return value


def describe(alert: dict) -> str:
    prefix = f"{alert['host']} {alert['repo'] or 'unregistered'} {alert['label']}: "
    if alert["rule"] == "stalled":
        detail = f"0 runs in last 2 h (0 failures); last run {alert['last_run_at'] or 'never'}"
    elif alert["rule"] == "failure_rate":
        detail = f"{alert['count']}/{alert['total']} runs failed in last 6 h"
        if alert.get("undated_count"):
            detail += f" (includes {alert['undated_count']} undated retained runs; window uncertain)"
    else:
        detail = f"last {alert['count']} factory passes failed; first failure {alert['first_failed_at'] or 'undated'}"
    error = alert["first_error"] or alert["last_error"]
    return prefix + detail + (f"; {error}" if error else "")


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


def _launchd_history(payload: dict) -> list[PassResult]:
    logs, stderr = payload["logs"], payload["stderr"]
    if not isinstance(logs, dict) or not all(isinstance(value, str) for value in logs.values()):
        raise ValueError("launchd logs must be an object of strings")
    if not isinstance(stderr, str):
        raise ValueError("launchd stderr must be a string")
    # stdout/stderr may be the same file; never count a pass twice.
    passes = max((launchd_passes(value) for value in logs.values()), key=_latest_pass_at, default=[])
    if passes and stderr.strip() and not END.search(stderr):
        lines = stderr.strip().splitlines()
        passes[-1] = replace(passes[-1], error=next((line for line in lines if _error(line) and "Traceback" not in line), lines[-1]))
    return passes


def _properties(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _systemd_job(runner, label: str, root: str, timeout: float) -> dict:
    service = _properties(_output(runner, ["systemctl", "--user", "show", f"{label}.service",
        "--property=ExecMainStatus,Result,ExecMainStartTimestamp,ExecMainExitTimestamp,ActiveState,ActiveEnterTimestamp"], timeout))
    timer = _properties(_output(runner, ["systemctl", "--user", "show", f"{label}.timer",
        "--property=UnitFileState,ActiveState,LastTriggerUSec,ActiveEnterTimestamp"], timeout))
    gaps = []
    try:
        text = _output(runner, ["journalctl", "--user", "-u", f"{label}.service", "--output=json",
                               "--no-pager", "-n", "10000"], timeout)
        passes, pending_error = journal_passes(text, f"{label}.service")
    except OSError as exc:
        code = "journalctl_unavailable" if isinstance(exc, FileNotFoundError) or "not found" in str(exc) else "pass_history_unavailable"
        gaps.append({"code": code, "error": str(exc)})
        passes, pending_error = [], None
    last_run = _stamp(service.get("ExecMainStartTimestamp", "")) or _stamp(timer.get("LastTriggerUSec", ""))
    completed = _stamp(service.get("ExecMainExitTimestamp", "")) or last_run
    if (completed and "ExecMainStatus" in service and service.get("ActiveState") not in ("active", "activating")
            and (not passes or not passes[-1].at or datetime.fromisoformat(completed) > datetime.fromisoformat(passes[-1].at))):
        failed = service["ExecMainStatus"] != "0" or service.get("Result", "success") != "success"
        passes.append(PassResult(failed, completed, pending_error or
                                 f"ExecMainStatus={service['ExecMainStatus']} Result={service.get('Result', 'unknown')}"))
    return {"label": label, "root": root, "passes": passes, "enabled": timer.get("UnitFileState") in ("enabled", "enabled-runtime"),
            "last_run_at": last_run, "installed_at": _stamp(timer.get("ActiveEnterTimestamp", "")),
            "errors": gaps, "running": service.get("ActiveState") in ("active", "activating")}


def _jobs(runner, platform: str, expected: dict[str, str], timeout: float) -> tuple[list[dict], list[dict]]:
    if platform == "macos":
        payload = json.loads(_output(runner, ["python3", "-c", LAUNCHD_LOGS, json.dumps(expected)], timeout))
        jobs = payload["jobs"]
        for job in jobs:
            job["passes"] = _launchd_history(job)
            dated = [p.at for p in job["passes"] if p.at]
            job["last_run_at"] = dated[-1] if dated else None
            if not job["last_run_at"] and job["passes"] and job["mtimes"]:
                job["last_run_at"] = datetime.fromtimestamp(max(job["mtimes"].values()), timezone.utc).isoformat()
            status = job["status"]
            exit_code = re.search(r"last exit code\s*=\s*(-?\d+)", status)
            runs = re.search(r"runs\s*=\s*(\d+)", status)
            job["runs"] = int(runs[1]) if runs else None
            # The manager is authoritative for the latest exit, never N invented exits.
            if exit_code and (job["runs"] is None or job["runs"] > 0):
                failed = int(exit_code[1]) != 0
                if job["passes"]:
                    job["passes"][-1] = replace(job["passes"][-1], failed=failed)
                else:
                    job["passes"] = [PassResult(failed, None, f"launchctl last exit code={exit_code[1]}")]
            installed = job["installed_at"]
            job["installed_at"] = datetime.fromtimestamp(installed, timezone.utc).isoformat() if installed else None
            job["running"] = bool(re.search(r"^\s*state\s*=\s*running\s*$", status, re.M))
            job["errors"] = [{"code": "pass_history_unavailable", "error": error} for error in job["errors"]]
        return jobs, [{"code": "pass_history_unavailable", "error": error} for error in payload["errors"]]
    labels = set(expected)
    for command in ("list-units", "list-unit-files"):
        text = _output(runner, ["systemctl", "--user", command, "factory-*.service", "--no-legend", "--no-pager"], timeout)
        labels.update(line.split()[0].removesuffix(".service") for line in text.splitlines()
                      if line.split() and line.split()[0].endswith(".service"))
    jobs = []
    for label in sorted(labels):
        if label not in expected and label.endswith(("-dashboard", "-triage-probe", "-eval")):
            continue
        try:
            jobs.append(_systemd_job(runner, label, expected.get(label, ""), timeout))
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            jobs.append({"label": label, "root": expected.get(label, ""), "passes": [],
                         "errors": [{"code": "pass_history_unavailable", "error": str(exc)}]})
    return jobs, []


def _alerts(job: dict, count: int, name: str, slug: str | None, at: datetime) -> list[dict]:
    passes = job["passes"]
    base = {"host": name, "repo": slug, "label": job["label"], "last_run_at": job.get("last_run_at")}

    def alert(rule, items, total, window):
        failed = [item for item in items if item.failed]
        return {**base, "rule": rule, "count": len(failed), "total": total, "window_hours": window,
                "first_failed_at": failed[0].at if failed else None,
                "last_failed_at": failed[-1].at if failed else None,
                "first_error": failed[0].error if failed else None,
                "last_error": failed[-1].error if failed else None}

    alerts = []
    recent = passes[-count:]
    if len(recent) == count and all(item.failed for item in recent):
        alerts.append(alert("failure_streak", recent, count, None))
    start = at - timedelta(hours=6)
    window = [p for p in passes if p.at is None or start <= datetime.fromisoformat(p.at) <= at]
    if window and sum(p.failed for p in window) > len(window) / 2:
        rate = alert("failure_rate", window, len(window), 6)
        rate["undated_count"] = sum(p.at is None for p in window)
        alerts.append(rate)
    last = job.get("last_run_at") or (passes[-1].at if passes else job.get("installed_at"))
    if job.get("enabled") and not job.get("running") and last and at - datetime.fromisoformat(last) >= timedelta(hours=2):
        alerts.append(alert("stalled", [], 0, 2))
    return alerts


def collect(data: dict, *, skip: set[str] | None = None, timeout: float = 10) -> tuple[list[dict], list[dict]]:
    """Read each registered host once, including hosts without assigned repositories."""
    tables = host.repos(data)
    assignments = {name: {} for name in host.hosts(data)}
    for slug, table in tables.items():
        for name in host.repo_hosts(table, slug):
            assignments.setdefault(name, {})[host.unit_name(slug)] = slug
    counts = {name: threshold(data, name) for name in assignments}

    def collect_host(pair):
        name, slugs = pair
        alerts, gaps = [], []
        def gap(code, error, slug=None, label=None, unreachable=False):
            gaps.append({"host": name, "repo": slug, "label": label, "code": code,
                         "error": error, "unreachable": unreachable})
        if not slugs:
            gap("host_without_repos", f"{name}: configured host has no assigned repositories")
        if name in (skip or ()):
            return alerts, gaps
        target = host.hosts(data).get(name, {})
        runner = host.run if name == "local" else host.remote_runner(name, target)
        platform = ("macos" if sys.platform == "darwin" else "linux") if name == "local" else target["platform"]
        expected = {label: tables[slug]["path"] for label, slug in slugs.items()}
        try:
            jobs, errors = _jobs(runner, platform, expected, timeout)
            for error in errors:
                gap(error["code"], error["error"])
            for job in jobs:
                label = job["label"]
                # Exclude known auxiliary services, not hand-made dispatcher labels.
                if label not in expected and label.endswith(("-dashboard", "-triage-probe", "-eval")):
                    continue
                matches = [slug for stem, slug in slugs.items() if label == stem or label.endswith("." + stem)]
                if not matches:
                    matches = [slug for slug in slugs.values() if job["root"] == tables[slug]["path"]]
                slug = matches[0] if len(matches) == 1 else None
                if slug is None:
                    gap("unrecognized_factory_job", f"{name} {label}: factory job is not assigned to a registered repository", label=label)
                for error in job["errors"]:
                    gap(error["code"], error["error"], slug, label)
                undated = sum(p.at is None for p in job["passes"])
                if undated:
                    gap("undated_history", f"{name} {label}: {undated} undated runs retained; six-hour timing uncertain", slug, label)
                alerts.extend(_alerts(job, counts[name], name, slug, datetime.now(timezone.utc)))
        except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, ValueError, TypeError, KeyError, OverflowError) as exc:
            gap("pass_history_unavailable", str(exc), unreachable=isinstance(exc, host.RemoteHostError))
        return alerts, gaps

    alerts, gaps = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found, errors in pool.map(collect_host, assignments.items()):
            alerts.extend(found)
            gaps.extend(errors)
    return alerts, gaps
