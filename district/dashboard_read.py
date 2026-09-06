"""Dashboard-only projections; never expose the host registry or raw CLI diagnostics.

`safe_fleet` retains Atlas's operational fields, not arbitrary snapshot/config keys.
`detect` is for an already-authorized local operator: it reads Git metadata and
bounded repository markers and workflows, never clones, runs doctor, or executes gate commands.
Its text contains only repository identity, onboarding mode, check names/sources,
exclusivity and host-owned key names. Paths, commands and configuration values
are deliberately withheld. CLI/status consumers keep their existing full data.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from datetime import datetime
from itertools import islice
from pathlib import Path

from district import add, health, host, metrics

SLUG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SECRET = re.compile(r"gh[pousr]_|github_pat_|sk-[A-Za-z0-9]|AKIA[0-9A-Z]{16}")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)")
HEAD = re.compile(r"[0-9a-fA-F]{7,64}")
VERSION = re.compile(r"\d{1,5}(?:\.\d{1,5}){1,3}(?:[-+][A-Za-z0-9.-]{1,32})?")
LANGUAGES = frozenset((*metrics.LANGUAGES.values(), "other"))
MAX_FACTORIES = 64
MAX_RECORDS = 32
MAX_EVIDENCE = 8
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}")
PRIVATE_TEXT = re.compile(
    r"(?i)(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]+|AKIA[0-9A-Z]{16}|"
    r"(?:bearer|basic)\s+\S+|"
    r"\b(?:[\w.-]*(?:token|password|passwd|secret|api.?key|credential)[\w.-]*|authorization)"
    r"[\"']?\s*[=:].*|"
    r"\b[A-Za-z_][\w.-]*(?:[\"']\s*[:=]|\s*=).*|"
    r"[a-z][a-z0-9+.-]*://[^\s<>\"']+|"
    r"(?<![\w])(?:~/|/)[^\s<>\"']+|[A-Za-z]:\\[^\s<>\"']+"
)


def _stamp(value: object) -> str | None:
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


LIMIT = 65536
COUNTS = (
    "loc", "files", "test_loc", "test_files", "contributors", "commits", "commits_30d", "commits_7d",
    "stars", "forks", "watchers", "open_issues", "open_bugs", "open_prs", "merged_prs_30d", "agent_prs_30d",
    "closed_issues_30d", "closed_bugs_30d",
)


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _match(value: object, pattern: re.Pattern) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) and not SECRET.search(value) else None


def _slug(value: object) -> str | None:
    result = _match(value, SLUG)
    return result if result and result.rsplit("/", 1)[-1] not in (".", "..") else None


def _count(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else 0


def _number(value: object, maximum: float = 2**53 - 1) -> int | float | None:
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum else None


def _labels(value: object) -> list[str]:
    return [label for item in _list(value)[:64] if (label := _match(item, LABEL))]


def safe_text(value: object, limit: int = 512) -> str | None:
    """One bounded evidence sentence, never a raw log/configuration document."""
    if not isinstance(value, str):
        return None
    if len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "[details withheld]"
    if "PRIVATE KEY" in value or re.search(r"(?i)(?:set-cookie|cookie)\s*:", value):
        return "[details withheld]"
    clean = PRIVATE_TEXT.sub("[redacted]", value)
    return clean if len(clean) <= limit else clean[:limit - 12] + " [truncated]"


def _identity(value: object) -> str | None:
    """Keep safe public identities; opaque hashes preserve equality without exposing configuration."""
    if not isinstance(value, str):
        return None
    if _match(value, IDENTITY) and safe_text(value) == value:
        return value
    return "opaque:" + hashlib.sha256(value.encode()).hexdigest()


def _choice(value: object, choices: tuple, default: str = "unknown") -> str:
    return value if isinstance(value, str) and value in choices else default


def _classification(raw: dict) -> dict:
    """Project the classifier's verdict; never infer a verdict from evidence or legacy health."""
    omitted = {}

    def records(value, name, limit=MAX_RECORDS):
        items = _list(value)
        omitted[name] = omitted.get(name, 0) + max(0, len(items) - limit)
        return [_dict(item) for item in items[:limit] if isinstance(item, dict)]

    findings = []
    for item in records(raw.get("findings"), "findings"):
        scope = _dict(item.get("scope"))
        scope = {"kind": _choice(scope.get("kind"), ("factory", "shared")),
                 "id": _identity(scope.get("id"))}
        condition = _choice(item.get("condition_code"), tuple(health.CHECKS))
        resource = _identity(item.get("resource"))
        finding = {
            "id": json.dumps([scope["kind"], scope["id"], condition, resource], separators=(",", ":")),
            "factory": _slug(item.get("factory")), "scope": scope,
            "condition_code": condition, "resource": resource,
            "severity": _choice(item.get("severity"), ("warning", "error")),
            "observed_at": _stamp(item.get("observed_at")),
            "impact": safe_text(item.get("impact")), "cause": safe_text(item.get("cause")),
            "evidence": [{
                "source_id": _identity(ev.get("source_id")), "factory": _slug(ev.get("factory")),
                "observed_at": _stamp(ev.get("observed_at")),
                "observation": _choice(ev.get("observation"), ("fresh", "stale", "partial", "unavailable"), "unavailable"),
                "reference": _identity(ev.get("reference")), "detail": safe_text(ev.get("detail")),
            } for ev in records(item.get("evidence"), "evidence", MAX_EVIDENCE)],
        }
        evidence_omitted = max(0, len(_list(item.get("evidence"))) - MAX_EVIDENCE)
        finding["projection"] = {"truncated": bool(evidence_omitted), "omitted": {"evidence": evidence_omitted}}
        history = _dict(item.get("history"))
        if history:
            finding["history"] = {key: _stamp(history.get(key)) for key in ("start", "end")}
            finding["history"].update({key: history.get(key) if type(history.get(key)) is bool else None
                                       for key in ("complete", "truncated")})
            finding.update({key: _stamp(item[key]) for key in ("first_observed_at", "last_observed_at") if key in item})
        findings.append(finding)
    sources = [{
        "id": _identity(item.get("id")), "observed_at": _stamp(item.get("observed_at")),
        "cadence_seconds": _number(item.get("cadence_seconds")),
        "age_seconds": item.get("age_seconds") if type(item.get("age_seconds")) in (int, float)
                       and math.isfinite(item["age_seconds"]) and abs(item["age_seconds"]) <= 2**53 - 1 else None,
        "observation": _choice(item.get("observation"), ("fresh", "stale", "partial", "unavailable"), "unavailable"),
        "error": safe_text(item.get("error")),
    } for item in records(raw.get("sources"), "sources")]
    executions = [{
        "id": _identity(item.get("id")), "source_id": _identity(item.get("source_id")),
        "state": _choice(item.get("state"), health.EXECUTION_STATES),
        "observation": _choice(item.get("observation"), ("fresh", "stale", "partial", "unavailable"), "unavailable"),
        "observed_at": _stamp(item.get("observed_at")),
        **{key: _stamp(item[key]) for key in ("entered_at",) if key in item},
        **{key: _identity(item[key]) for key in ("stage", "reference") if key in item},
        **{key: safe_text(item[key]) for key in ("reason",) if key in item},
        **{key: _choice(item[key], ("product", "mechanism", "unknown")) for key in ("outcome_kind",) if key in item},
    } for item in records(raw.get("executions"), "executions")]
    resources = [{
        "id": _identity(item.get("id")), "source_id": _identity(item.get("source_id")),
        "observed_at": _stamp(item.get("observed_at")),
        "observation": _choice(item.get("observation"), ("fresh", "stale", "partial", "unavailable"), "unavailable"),
        "held": item.get("held") if type(item.get("held")) is bool else None,
        "ownership": _choice(item.get("ownership"), ("known", "unknown", "none")),
        "owner": {"factory": _slug(item["owner"].get("factory")),
                  "execution_id": _identity(item["owner"].get("execution_id"))}
                 if isinstance(item.get("owner"), dict) else None,
    } for item in records(raw.get("resources"), "resources")]
    unknowns = _list(raw.get("unknowns"))
    omitted["unknowns"] = max(0, len(unknowns) - MAX_RECORDS)
    return {
        "schema_version": 1 if type(raw.get("schema_version")) is int and raw["schema_version"] == 1 else None,
        "assessment": _choice(raw.get("assessment"), ("normal", "attention", "unknown")),
        "operating_state": _choice(raw.get("operating_state"), (
            "running", "scheduled waiting", "deliberately paused", "capped", "unexpectedly stopped", "unknown")),
        "execution_state": _choice(raw.get("execution_state"), health.EXECUTION_STATES),
        "observation": _choice(raw.get("observation"), ("fresh", "stale", "partial", "unavailable"), "unavailable"),
        "findings": findings, "sources": sources, "executions": executions, "resources": resources,
        "unknowns": [safe_text(item) for item in unknowns[:MAX_RECORDS] if isinstance(item, str)],
        "projection": {"truncated": any(omitted.values()), "omitted": omitted},
    }


def _metrics(raw: object) -> dict | None:
    if not isinstance(raw, dict) or not raw:
        return None
    traffic = _dict(raw.get("traffic"))
    return {
        **{key: _number(raw.get(key)) for key in COUNTS},
        "head": _match(raw.get("head"), HEAD),
        "collected_at": _stamp(raw.get("collected_at")) or "unknown",
        "languages": {key: _number(raw["languages"][key]) for key in LANGUAGES if key in _dict(raw.get("languages"))},
        "open_by_label": {key: _number(raw["open_by_label"][key]) for key in metrics.FACTORY_LABELS
                          if key in _dict(raw.get("open_by_label"))},
        "top3_share": _number(raw.get("top3_share"), 1),
        "median_days_to_close": _number(raw.get("median_days_to_close")),
        "traffic": {kind: {key: _number(traffic[kind].get(key)) for key in ("count", "uniques")}
                    if isinstance(traffic.get(kind), dict) else "unavailable" for kind in ("clones", "views")},
    }


def _snapshot(raw: object) -> dict | None:
    if not isinstance(raw, dict) or not raw:
        return None
    config, dispatcher = _dict(raw.get("config")), _dict(raw.get("dispatcher"))
    timer, quality = _dict(dispatcher.get("timer")), _dict(raw.get("metrics"))
    upstream = _dict(raw.get("upstream"))
    blocker = _dict(upstream.get("blocker"))
    return {
        "version": _match(raw.get("version"), VERSION) or "?",
        "generated_at": _stamp(raw.get("generated_at")),
        "config": {
            **({"upstream": _match(config["upstream"], LABEL)} if "upstream" in config else {}),
            "gate_checks": _labels(config.get("gate_checks")),
            "exclusive_checks": _labels(config.get("exclusive_checks")),
        },
        "dispatcher": {
            "timer": {"active": timer.get("active") if type(timer.get("active")) is bool else None,
                      "next": _stamp(timer.get("next")), "last": _stamp(timer.get("last"))},
            "service_active": dispatcher.get("service_active") if type(dispatcher.get("service_active")) is bool else None,
            "consecutive_failures": _number(dispatcher.get("consecutive_failures")),
            "runs": [{"result": _choice(run.get("result"), ("running", "done", "failed")),
                      **{key: _stamp(run.get(key)) for key in ("started", "finished", "started_at", "finished_at") if key in run}}
                     for run in _list(dispatcher.get("runs"))[-32:] if isinstance(run, dict)],
        },
        "tickets": [{"number": _number(ticket.get("number")),
                     "state": ticket.get("state") if ticket.get("state") in ("OPEN", "CLOSED") else "UNKNOWN",
                     "stage": _match(ticket.get("stage"), LABEL) or "unknown",
                     "labels": [label for label in _labels(ticket.get("labels")) if label in metrics.FACTORY_LABELS]}
                    for ticket in _list(raw.get("tickets"))[:128] if isinstance(ticket, dict)],
        "metrics": {"first_pass": _number(quality.get("first_pass"), 1),
                    "bounce_rate": _number(quality.get("bounce_rate"), 1),
                    "escalations": _number(quality.get("escalations"))},
        "upstream": {"repo": _slug(upstream.get("repo")), "ahead": _number(upstream.get("ahead")),
                     "behind": _number(upstream.get("behind")),
                     "blocker": {"number": _number(blocker.get("number"))} if blocker else None},
        "errors": ["snapshot unavailable or incomplete"] if raw.get("errors") else [],
    }

def _activity(raw: object) -> dict:
    raw = _dict(raw)
    history = _dict(raw.get("history"))
    events = []
    for item in _list(raw.get("events"))[:512]:
        if not isinstance(item, dict):
            continue
        event_id = _identity(item.get("event_id"))
        if not event_id:
            continue
        events.append({
            "event_id": event_id,
            "execution_id": _identity(item.get("execution_id")),
            "sequence": _number(item.get("sequence")),
            "ticket": _number(item.get("ticket")),
            "stage": _identity(item.get("stage")),
            "kind": _identity(item.get("kind")),
            "at": _stamp(item.get("at")),
            "outcome": _identity(item.get("outcome")),
            "reason": safe_text(item.get("reason")),
        })
    return {
        "events": events,
        "history": {
            "source": _identity(history.get("source")),
            "status": _choice(history.get("status"), ("empty", "available", "missing", "unreadable")),
            "start_at": _stamp(history.get("start_at")), "end_at": _stamp(history.get("end_at")),
            "complete": history.get("complete") if type(history.get("complete")) is bool else None,
            "truncated": history.get("truncated") if type(history.get("truncated")) is bool else None,
            "gaps": [_identity(gap) for gap in _list(history.get("gaps"))[:32] if _identity(gap)],
            **{key: _number(history.get(key)) for key in (
                "bytes_read", "byte_limit", "event_limit", "retained_events")},
        },
    }


def _collection(raw: object) -> dict:
    raw = _dict(raw)
    return {
        "runtime_collected_at": _stamp(raw.get("runtime_collected_at")),
        "runtime_age_seconds": _number(raw.get("runtime_age_seconds")),
        "full_collected_at": _stamp(raw.get("full_collected_at")),
        "full_age_seconds": _number(raw.get("full_age_seconds")),
    }


def safe_fleet(fleet: dict) -> dict:
    """Bounded, allowlisted D01 semantics and project context; not another classifier."""
    result = {}
    for slug, raw in islice(_dict(fleet).items(), MAX_FACTORIES):
        if not _slug(slug) or not isinstance(raw, dict):
            continue
        table = _dict(raw.get("table"))
        port = _count(_dict(table.get("dashboard")).get("port"))
        result[slug] = {
            "table": {"disabled_at": _stamp(table.get("disabled_at"))
                      or ("disabled" if table.get("disabled_at") else None),
                      "dashboard": {"port": port if 1 <= port <= 65535 else None}},
            "snap": _snapshot(raw.get("snap")), "metrics": _metrics(raw.get("metrics")),
            "error": "dashboard unavailable or incomplete" if raw.get("error") else None,
            "activity": _activity(raw.get("activity")), "collection": _collection(raw.get("collection")),
            **_classification(raw),
        }
        omitted = result[slug]["projection"]["omitted"]
        snap = _dict(raw.get("snap"))
        omitted.update(
            tickets=max(0, len(_list(snap.get("tickets"))) - 128),
            runs=max(0, len(_list(_dict(snap.get("dispatcher")).get("runs"))) - 32),
            gate_checks=max(0, len(_list(_dict(snap.get("config")).get("gate_checks"))) - 64),
            exclusive_checks=max(0, len(_list(_dict(snap.get("config")).get("exclusive_checks"))) - 64),
            ticket_labels=sum(max(0, len(_list(_dict(ticket).get("labels"))) - 64)
                              for ticket in _list(snap.get("tickets"))[:128]),
        )
    for entry in result.values():
        omitted = entry["projection"]["omitted"]
        omitted["factories"] = max(0, len(fleet) - len(result))
        entry["projection"]["truncated"] = any(omitted.values())
    return result


def _github_url(target: str) -> str | None:
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if target.startswith(prefix):
            slug = _slug(target[len(prefix):].removesuffix("/").removesuffix(".git"))
            return add.remote_slug(target.rstrip("/")) if slug else None
    return None


def _probe(argv: list[str], cwd: Path | None = None) -> str:
    # Only fixed Git metadata queries and a field-selected GitHub query call this.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", GH_PAGER="cat", GH_HOST="github.com")
    proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, timeout=10, check=False)
    if proc.returncode or len(proc.stdout) > LIMIT:
        raise ValueError("metadata unavailable")
    return proc.stdout.strip()


def _marker(path: Path, *, dir_fd: int | None = None) -> str | None:
    """Read a small regular file only; reject symlinks, devices and oversized markers."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            raise ValueError("unsafe marker")
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("oversized marker")
    return data.decode("utf-8")


def _copy_workflows(root: Path, markers: Path) -> None:
    """Copy bounded workflow inputs without following directory or file symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        github_fd = os.open(root / ".github", flags)
    except FileNotFoundError:
        return
    try:
        try:
            workflows_fd = os.open("workflows", flags, dir_fd=github_fd)
        except FileNotFoundError:
            return
        try:
            with os.scandir(workflows_fd) as entries:
                names = [entry.name for entry in islice(entries, 65)]
            if len(names) > 64:
                raise ValueError("too many workflow entries")
            destination = markers / ".github" / "workflows"
            destination.mkdir(parents=True)
            for name in sorted(names):
                if name.endswith((".yml", ".yaml")):
                    content = _marker(Path(name), dir_fd=workflows_fd)
                    if content is not None:
                        (destination / name).write_text(content)
        finally:
            os.close(workflows_fd)
    finally:
        os.close(github_fd)


def detect(target: str) -> tuple[int, dict]:
    """Return (HTTP status, {ok, output}); bounded, read-only, authorized-local preview.

    Accept a checkout root or an exact HTTPS/SSH github.com owner/repo URL.
    Existing clones use the CLI's destination and port selection. Host settings
    are read only for these decisions, never returned. Doctor is not invoked.
    """
    if (not isinstance(target, str) or not target or len(target) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in target) or target.startswith("-")):
        return 400, {"ok": False, "output": "Enter a local Git checkout or a GitHub repository URL.\n"}
    try:
        remote = _github_url(target)
        if not remote and ("://" in target or target.startswith("git@")):
            return 400, {"ok": False, "output": "Only exact github.com owner/repo URLs are supported.\n"}
        data, existing = host.load(), {}
        checks, lifted = [], []
        root = add.clone_destination(target, data) if remote else Path(target).expanduser()
        if remote and not root.exists():
            slug, mode = remote, "clone"
        else:
            root = root.resolve(strict=True)
            if not root.is_dir() or not (root / ".git").exists():
                raise ValueError("not a checkout")
            if _probe(["git", "-c", "core.fsmonitor=false", "rev-parse", "--is-inside-work-tree"], root) != "true":
                raise ValueError("not a checkout")
            slug = _github_url(_probe(["git", "config", "--local", "--no-includes", "--get", "remote.origin.url"], root))
            if not slug:
                raise ValueError("not a GitHub checkout")
            config = _marker(root / add.CONFIG_NAME)
            mode = "adopt" if config is not None else "onboard"
            if config is not None:
                existing = tomllib.loads(config)
                _, host_keys = add.lift(config)
                lifted = [name for name in (*add.HOST_TABLES, *add.HOST_KEYS) if name in host_keys]
                checks = _list(_dict(existing.get("gate")).get("check"))
            else:
                with tempfile.TemporaryDirectory(prefix="district-detect-") as directory:
                    markers = Path(directory)
                    for name in ("Cargo.toml", "package.json", "pyproject.toml", "Makefile"):
                        content = _marker(root / name)
                        if content is not None:
                            (markers / name).write_text(content)
                    _copy_workflows(root, markers)
                    checks = add.propose_checks(markers)
        lines = ["read-only preview: nothing written", f"repo: {slug} ({mode})"]
        port = add.dashboard_port(slug, existing, data)
        lines.append(f"dashboard port: {port}" if type(port) is int and 0 < port < 65536 else "dashboard port: unavailable")
        try:
            info = _dict(json.loads(_probe(["gh", "repo", "view", slug, "--json", "isFork,parent"])))
            parent = _dict(info.get("parent"))
            parent_slug = _slug(parent.get("nameWithOwner")) or _slug(
                f"{_dict(parent.get('owner')).get('login', '')}/{parent.get('name', '')}"
            )
            lines.append(f"fork of: {parent_slug}" if info.get("isFork") is True and parent_slug else
                         "not a fork" if info.get("isFork") is False else "fork metadata unavailable")
        except (OSError, ValueError, subprocess.SubprocessError):
            lines.append("fork metadata unavailable")
        if mode == "clone":
            lines.append("checks: detected after cloning; no clone performed")
        else:
            if mode == "adopt":
                lines.append("host keys to lift: " + (", ".join(lifted) or "none"))
            lines.append("checks (committed):" if mode == "adopt" else "checks (proposed):")
            for check in checks[:64]:
                check = _dict(check)
                name = _match(check.get("name"), LABEL) or "name withheld"
                source = ""
                if mode == "onboard":
                    origin = check["source"]
                    if origin.startswith(".github/workflows/"):
                        filename, _, line = origin.removeprefix(".github/workflows/").rpartition(":")
                        origin = f".github/workflows/{filename}:{line}" if _match(filename, LABEL) and line.isdecimal() else "workflow source withheld"
                    source = f" from {origin}"
                lines.append(f"  {name}{' (exclusive)' if check.get('exclusive') is True else ''}{source}")
            if not checks:
                lines.append("  none detected" if mode == "onboard" else "  none committed")
            if len(checks) > 64:
                lines.append("  additional checks omitted")
        lines.append("Commands, paths and configuration values withheld; doctor is not run.")
        return 200, {"ok": True, "output": "\n".join(lines)[:8191] + "\n"}
    except (OSError, ValueError, TypeError, AttributeError, add.Refuse, host.DistrictError, subprocess.SubprocessError):
        return 400, {"ok": False, "output": "Cannot inspect target: use a GitHub checkout with readable, small regular configuration files.\n"}
