"""Retained service pass completions, independent of Factory's Python imports."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from district import host

# systemd's UNIT_STARTED (oneshot completion) and UNIT_FAILED message IDs.
UNIT_STARTED = "39f53479d3a045ac8e11786248231fbf"
UNIT_FAILED = "be02cf6855d2428ba40df7e9d022f03d"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")
END = re.compile(r"===\s*end\s+rc=(-?\d+)\b")

# A real error worth surfacing over benign "0 failed"/"triage OK" pass summaries.
HARD_ERROR = re.compile(r"\w*(?:error|exception)\b|traceback|\bfatal\b|\bpanic\b", re.I)
BENIGN = re.compile(r"\b(?:0|no)\s+(?:failed|failures?|errors?)\b|\btriage ok\b", re.I)

# Read paths on the owning host, never on the controller. This collector trusts only UTC evidence:
# the systemd user journal (`__REALTIME_TIMESTAMP`) and launchd pass logs whose markers carry an
# ISO-8601 timestamp. Native pass-history parsing from human-zoned launchd/systemctl timestamps is
# out of scope here and rebuilt on UTC-only inputs in district#204; launchd runs that District can
# only observe through such local-zone stamps surface as `launchd_history_unsupported`, never as a
# guessed instant or as healthy evidence.
# ponytail: inspect the last 4 MiB per log; rotated/older history is not evidence.
LAUNCHD_LOGS = """\
import json, pathlib, plistlib, subprocess, sys
expected = json.loads(sys.argv[1])
home = pathlib.Path.home()
def command(*args):
    try:
        p = subprocess.run(['launchctl', *args], capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except OSError as exc:
        return 127, '', str(exc)
jobs = {}
errors = []
for plist in (home / 'Library/LaunchAgents').glob('*.plist'):
    try:
        doc = plistlib.loads(plist.read_bytes())
        label = doc.get('Label', plist.stem)
        if 'factory' in label or 'factory' in str(doc.get('ProgramArguments', [])):
            jobs[label] = doc
    except (OSError, ValueError) as exc:
        errors.append(str(plist) + ': ' + str(exc))
rc, listing, error = command('list')
if rc:
    errors.append(error or listing or 'launchctl list unavailable')
else:
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 3 and 'factory' in fields[-1]:
            jobs.setdefault(fields[-1], {})
for label in expected:
    if not any(label == actual or actual.endswith('.' + label) for actual in jobs):
        jobs[label] = {}
result = []
for label, doc in jobs.items():
    root = doc.get('WorkingDirectory') or next(
        (root for stem, root in expected.items() if label == stem or label.endswith('.' + stem)), '')
    paths = [doc[key] for key in ('StandardOutPath', 'StandardErrorPath') if isinstance(doc.get(key), str)]
    error_path = doc.get('StandardErrorPath')
    if not paths:
        logdir = pathlib.Path(root).expanduser() / '.factory/logs'
        stem = next((stem for stem in expected if label == stem or label.endswith('.' + stem)), label)
        paths = [str(logdir / (stem + '.stdout.log')), str(logdir / (stem + '.stderr.log')),
                 str(home / 'Library/Logs' / (stem + '.log'))]
        error_path = paths[1]
    logs, gaps = {}, []
    for name in dict.fromkeys(paths):
        try:
            with pathlib.Path(name).expanduser().open('rb') as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - 4 * 1024 * 1024))
                if size > 4 * 1024 * 1024:
                    stream.readline()
                logs[name] = stream.read().decode(errors='replace')
        except FileNotFoundError:
            continue
        except OSError as exc:
            gaps.append(str(exc))
    result.append({'label': label, 'root': root, 'logs': logs,
                   'stderr': logs.get(error_path, ''), 'errors': gaps})
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
    return None


def _rank(line: str | None) -> int:
    if not line:
        return -1
    if HARD_ERROR.search(line):
        return 2
    if BENIGN.search(line):
        return -1
    return 1


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
            failed = (item["MESSAGE_ID"] == UNIT_FAILED or item.get("RESULT", "success") != "success"
                      or str(item.get("ExecMainStatus", item.get("EXIT_STATUS", "0"))) != "0")
            passes.append(PassResult(failed, timestamp, error))
            error = None
        elif isinstance(message, str) and message.strip() and item.get("MESSAGE_ID") is None:
            for detail in message.strip().splitlines():
                if detail.strip() and _rank(detail.strip()) > _rank(error):
                    error = detail.strip()
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
        elif line.strip() and _rank(line.strip()) > _rank(error):
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
    detail = f"last {alert['count']} factory passes failed; first failure {alert['first_failed_at']}"
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
        best = passes[-1].error
        for line in (candidate.strip() for candidate in stderr.strip().splitlines()):
            if line and "Traceback" not in line and _rank(line) > _rank(best):
                best = line
        passes[-1] = replace(passes[-1], error=best)
    return passes


def _systemd_job(runner, label: str, root: str, timeout: float) -> dict:
    gaps = []
    try:
        text = _output(runner, ["journalctl", "--user", "-u", f"{label}.service", "--output=json",
                               "--no-pager", "-n", "10000"], timeout)
        passes = journal_passes(text, f"{label}.service")
    except OSError as exc:
        code = "journalctl_unavailable" if isinstance(exc, FileNotFoundError) or "not found" in str(exc) else "pass_history_unavailable"
        gaps.append({"code": code, "error": str(exc)})
        passes = []
    return {"label": label, "root": root, "passes": passes, "errors": gaps}


def _jobs(runner, platform: str, expected: dict[str, str], timeout: float) -> tuple[list[dict], list[dict]]:
    if platform == "macos":
        payload = json.loads(_output(runner, ["python3", "-c", LAUNCHD_LOGS, json.dumps(expected)], timeout))
        jobs = payload["jobs"]
        for job in jobs:
            job["passes"] = _launchd_history(job)
            job["errors"] = [{"code": "pass_history_unavailable", "error": error} for error in job["errors"]]
        return jobs, [{"code": "pass_history_unavailable", "error": error} for error in payload["errors"]]
    labels = set(expected)
    for command in ("list-units", "list-unit-files"):
        # A discovery failure (e.g. no D-Bus session) must not suppress journal reads for known labels.
        try:
            text = _output(runner, ["systemctl", "--user", command, "factory-*.service",
                                    "--no-legend", "--no-pager"], timeout)
        except OSError:
            continue
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


def _alerts(job: dict, count: int, name: str, slug: str | None) -> list[dict]:
    passes = job["passes"]
    recent = passes[-count:]
    if len(recent) != count or not all(item.failed and item.at for item in recent):
        return []
    return [{"host": name, "repo": slug, "label": job["label"], "rule": "failure_streak",
             "count": count, "first_failed_at": recent[0].at, "last_failed_at": recent[-1].at,
             "first_error": recent[0].error, "last_error": recent[-1].error}]


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
                if platform == "macos" and job["passes"] and all(p.at is None for p in job["passes"]):
                    # The only runs District can see here are launchd stamps it cannot resolve to UTC.
                    # Surface the gap honestly (district#204) instead of inventing an instant or health.
                    gap("launchd_history_unsupported",
                        f"{name} {label}: native pass history not yet supported for launchd hosts (see district#204)",
                        slug, label)
                alerts.extend(_alerts(job, counts[name], name, slug))
        except (OSError, subprocess.TimeoutExpired, host.RemoteHostError, ValueError, TypeError, KeyError, OverflowError) as exc:
            gap("pass_history_unavailable", str(exc), unreachable=isinstance(exc, host.RemoteHostError))
        return alerts, gaps

    alerts, gaps = [], []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found, errors in pool.map(collect_host, assignments.items()):
            alerts.extend(found)
            gaps.extend(errors)
    return alerts, gaps
