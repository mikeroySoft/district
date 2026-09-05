"""Dashboard-only projections; never expose the host registry or raw CLI diagnostics.

`safe_fleet` retains Atlas's operational fields, not arbitrary snapshot/config keys.
`detect` is for an already-authorized local operator: it reads Git metadata and
bounded repository markers, never clones, runs doctor, or executes gate commands.
Its text contains only repository identity, onboarding mode, check names/sources,
exclusivity and host-owned key names. Paths, commands and configuration values
are deliberately withheld. CLI/status consumers keep their existing full data.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

from district import add, metrics

SLUG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SECRET = re.compile(r"gh[pousr]_|github_pat_|sk-[A-Za-z0-9]|AKIA[0-9A-Z]{16}")
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)")
HEAD = re.compile(r"[0-9a-fA-F]{7,64}")
VERSION = re.compile(r"\d{1,5}(?:\.\d{1,5}){1,3}(?:[-+][A-Za-z0-9.-]{1,32})?")
LANGUAGES = frozenset((*metrics.LANGUAGES.values(), "other"))


def _stamp(value: object) -> str | None:
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value).isoformat(timespec="seconds").replace("+00:00", "Z")
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
    return value if type(value) in (int, float) and 0 <= value <= maximum else None


def _labels(value: object) -> list[str]:
    return [label for item in _list(value) if (label := _match(item, LABEL))]


def _reason(value: object) -> str:
    if not isinstance(value, str):
        return "health details withheld"
    for prefix, text in (
        ("timer disabled by District:", "timer disabled by District"),
        ("dashboard unreachable", "dashboard unreachable"),
        ("snapshot errors:", "snapshot unavailable or incomplete"),
        ("doctor WARN:", "doctor warnings reported"),
    ):
        if value.startswith(prefix):
            return text
    if value in ("timer inactive", "last pass failed") or re.fullmatch(
        r"\d{1,10} consecutive failed pass\(es\)|\d{1,10} open ready-for-human \(#[\d, #]{1,256}\)|"
        r"upstream sync parked on #\d{1,10}|bounce rate \d{1,3}%", value
    ):
        return value
    return "health details withheld"


def _metrics(raw: object) -> dict | None:
    if not isinstance(raw, dict) or not raw:
        return None
    traffic = _dict(raw.get("traffic"))
    return {
        **{key: _count(raw.get(key)) for key in COUNTS},
        "head": _match(raw.get("head"), HEAD),
        "collected_at": _stamp(raw.get("collected_at")) or "unknown",
        "languages": {key: _count(value) for key, value in _dict(raw.get("languages")).items()
                      if key in LANGUAGES},
        "open_by_label": {key: _count(value) for key, value in _dict(raw.get("open_by_label")).items()
                          if key in metrics.FACTORY_LABELS},
        "top3_share": _number(raw.get("top3_share"), 1),
        "median_days_to_close": _number(raw.get("median_days_to_close")),
        "traffic": {kind: {key: _count(traffic[kind].get(key)) for key in ("count", "uniques")}
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
        "config": {
            "upstream": _match(config.get("upstream"), LABEL),
            "gate_checks": _labels(config.get("gate_checks")),
            "exclusive_checks": _labels(config.get("exclusive_checks")),
        },
        "dispatcher": {
            "timer": {"active": timer.get("active") is True,
                      "next": _stamp(timer.get("next")), "last": _stamp(timer.get("last"))},
            "service_active": dispatcher.get("service_active") is True,
            "consecutive_failures": _count(dispatcher.get("consecutive_failures")),
            "runs": [{"result": run.get("result") if run.get("result") in ("running", "done", "failed") else "unknown",
                      **{key: _stamp(run.get(key)) for key in ("started_at", "finished_at") if key in run}}
                     for run in _list(dispatcher.get("runs")) if isinstance(run, dict)],
        },
        "tickets": [{"number": _count(ticket.get("number")),
                     "state": ticket.get("state") if ticket.get("state") in ("OPEN", "CLOSED") else "UNKNOWN",
                     "stage": _match(ticket.get("stage"), LABEL) or "unknown",
                     "labels": [label for label in _labels(ticket.get("labels")) if label in metrics.FACTORY_LABELS]}
                    for ticket in _list(raw.get("tickets")) if isinstance(ticket, dict)],
        "metrics": {"first_pass": _number(quality.get("first_pass"), 1),
                    "bounce_rate": _number(quality.get("bounce_rate"), 1),
                    "escalations": _count(quality.get("escalations"))},
        "upstream": {"repo": _slug(upstream.get("repo")), "ahead": _count(upstream.get("ahead")),
                     "behind": _count(upstream.get("behind")),
                     "blocker": {"number": _count(blocker.get("number"))} if blocker else None},
        "errors": ["snapshot unavailable or incomplete"] if raw.get("errors") else [],
    }


def safe_fleet(fleet: dict) -> dict:
    """Fresh allowlisted Atlas-compatible data; strings are identifiers, not HTML or diagnostics."""
    result = {}
    for slug, raw in _dict(fleet).items():
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
            "health": raw.get("health") if raw.get("health") in ("healthy", "attention", "failing") else "failing",
            "reasons": list(dict.fromkeys(_reason(reason) for reason in _list(raw.get("reasons")))),
        }
    return result


def _github_url(target: str) -> str | None:
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if target.startswith(prefix):
            return _slug(target[len(prefix):].removesuffix("/").removesuffix(".git"))
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


def _marker(path: Path) -> str | None:
    """Read a small regular file only; reject symlinks, devices and oversized markers."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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


class _Markers:
    """Bounded in-memory marker view for add.propose_checks's read-only Path operations."""

    def __init__(self, files: dict[str, str | None], name: str = "") -> None:
        self.files, self.name = files, name

    def __truediv__(self, name: str) -> _Markers:
        return _Markers(self.files, name)

    def exists(self) -> bool:
        return self.files[self.name] is not None

    def read_text(self) -> str:
        return self.files[self.name] or ""


def detect(target: str) -> tuple[int, dict]:
    """Return (HTTP status, {ok, output}); bounded, read-only, authorized-local preview.

    Accept a checkout root or an exact HTTPS/SSH github.com owner/repo URL. URL
    previews query identity/fork metadata only, leaving checks until a clone exists.
    No host configuration is loaded. Errors never include inputs or CLI output.
    """
    if (not isinstance(target, str) or not target or len(target) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in target) or target.startswith("-")):
        return 400, {"ok": False, "output": "Enter a local Git checkout or a GitHub repository URL.\n"}
    try:
        remote = _github_url(target)
        if not remote and ("://" in target or target.startswith("git@")):
            return 400, {"ok": False, "output": "Only exact github.com owner/repo URLs are supported.\n"}
        checks, lifted = [], []
        if remote:
            slug, mode = remote, "clone"
        else:
            root = Path(target).expanduser().resolve(strict=True)
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
                markers = _Markers({name: _marker(root / name)
                                    for name in ("Cargo.toml", "package.json", "pyproject.toml", "Makefile")})
                checks = add.propose_checks(markers)
        lines = ["read-only preview: nothing written", f"repo: {slug} ({mode})"]
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
                source = f" from {check['source']}" if mode == "onboard" else ""
                lines.append(f"  {name}{' (exclusive)' if check.get('exclusive') is True else ''}{source}")
            if not checks:
                lines.append("  none detected" if mode == "onboard" else "  none committed")
            if len(checks) > 64:
                lines.append("  additional checks omitted")
        lines.append("Commands, paths and configuration values withheld; doctor is not run.")
        return 200, {"ok": True, "output": "\n".join(lines)[:8191] + "\n"}
    except (OSError, ValueError, TypeError, AttributeError, add.Refuse, subprocess.SubprocessError):
        return 400, {"ok": False, "output": "Cannot inspect target: use a GitHub checkout with readable, small regular configuration files.\n"}
