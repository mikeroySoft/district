"""`district metrics [slug] [--refresh] [--max-age 1h]`: the PRD §5.7 per-factory metrics.

Everything comes from `git` and `gh`; nothing is estimated. Results are cached
under `$XDG_CACHE_HOME/district/metrics/<slug>.json` with a timestamp, and the
hourly `district-metrics.timer` refreshes them so page loads never wait on GitHub.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from district import host
from district.apply import hours
from district.host import DistrictError, run

# Issue labels agent-factory creates (config.py LABELS); factory-approved is a PR label.
FACTORY_LABELS = ("needs-triage", "needs-info", "ready-for-agent", "ready-for-human", "wontfix")
LANGUAGES = {
    ".rs": "Rust", ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".html": "HTML", ".css": "CSS", ".md": "Markdown", ".toml": "TOML", ".yml": "YAML", ".yaml": "YAML",
    ".json": "JSON", ".sh": "Shell", ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".go": "Go", ".lock": "Lockfile",
    ".svg": "SVG", ".sql": "SQL", ".txt": "Text",
}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|testing)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "district" / "metrics"


def cache_path(slug: str) -> Path:
    return cache_dir() / f"{slug}.json"


def read(slug: str) -> dict | None:
    """The cached metrics, or None when never collected."""
    p = cache_path(slug)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def git(root: Path, *args: str) -> str:
    return run(["git", *args], cwd=root, check=True).stdout


def size(root: Path) -> dict:
    """Tracked LOC by language, files, and the share under test paths. Binary files count as 0 lines."""
    by_lang: Counter = Counter()
    files = test_files = test_loc = total = 0
    for rel in git(root, "ls-files", "-z").split("\0"):
        if not rel:
            continue
        p = root / rel
        if not p.is_file():
            continue  # submodule or deleted-but-tracked
        files += 1
        data = p.read_bytes()
        lines = 0 if b"\0" in data[:8000] else data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        total += lines
        by_lang[LANGUAGES.get(p.suffix.lower(), "other")] += lines
        if TEST_PATH.search(rel):
            test_files += 1
            test_loc += lines
    return {
        "loc": total, "files": files, "languages": dict(by_lang.most_common()),
        "test_loc": test_loc, "test_files": test_files,
    }


def people(root: Path) -> dict:
    authors = Counter(git(root, "log", "--format=%aN", "HEAD").splitlines())
    commits = sum(authors.values())
    top3 = sum(n for _, n in authors.most_common(3))
    return {
        "contributors": len(authors), "commits": commits,
        "top3_share": round(top3 / commits, 3) if commits else None,
        "commits_30d": int(git(root, "rev-list", "--count", "--since=30.days", "HEAD")),
        "commits_7d": int(git(root, "rev-list", "--count", "--since=7.days", "HEAD")),
        "head": git(root, "rev-parse", "--short", "HEAD").strip(),
    }


def gh_json(*args: str) -> dict | list:
    return json.loads(run(["gh", *args], check=True).stdout)


def traffic(slug: str, kind: str) -> dict | str:
    """14-day {count, uniques}; `unavailable` when the token lacks push access (401/403) or the call fails."""
    proc = run(["gh", "api", f"repos/{slug}/traffic/{kind}"])
    if proc.returncode != 0:
        return "unavailable"
    try:
        data = json.loads(proc.stdout)
        return {"count": int(data["count"]), "uniques": int(data["uniques"])}
    except (ValueError, KeyError, TypeError):
        return "unavailable"


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def has_bug(issue: dict) -> bool:
    return any("bug" in (label.get("name") or "").lower() for label in issue.get("labels", []))


def github(slug: str, at: datetime) -> dict:
    since = (at - timedelta(days=30)).strftime("%Y-%m-%d")
    repo = gh_json("repo", "view", slug, "--json", "stargazerCount,forkCount,watchers")
    open_issues = gh_json("issue", "list", "--repo", slug, "--state", "open", "--limit", "1000", "--json", "labels")
    closed = gh_json("issue", "list", "--repo", slug, "--state", "closed", "--limit", "500", "--json", "createdAt,closedAt,labels",
                     "--search", f"closed:>={since}")
    open_prs = gh_json("pr", "list", "--repo", slug, "--state", "open", "--limit", "1000", "--json", "number")
    merged = gh_json("pr", "list", "--repo", slug, "--state", "merged", "--limit", "500", "--json", "headRefName,mergedAt",
                     "--search", f"merged:>={since}")
    by_label: Counter = Counter(
        label["name"] for issue in open_issues for label in issue.get("labels", []) if label.get("name") in FACTORY_LABELS
    )
    days = [(parse_ts(i["closedAt"]) - parse_ts(i["createdAt"])).total_seconds() / 86400 for i in closed if i.get("closedAt")]
    return {
        "stars": repo["stargazerCount"], "forks": repo["forkCount"], "watchers": repo["watchers"]["totalCount"],
        "open_issues": len(open_issues), "open_by_label": dict(by_label), "open_bugs": sum(map(has_bug, open_issues)),
        "open_prs": len(open_prs),
        "merged_prs_30d": len(merged), "agent_prs_30d": sum(1 for p in merged if p["headRefName"].startswith("agent/")),
        "closed_issues_30d": len(closed), "closed_bugs_30d": sum(map(has_bug, closed)),
        "median_days_to_close": round(statistics.median(days), 2) if days else None,
        "traffic": {"views": traffic(slug, "views"), "clones": traffic(slug, "clones")},
    }


def collect(slug: str, table: dict) -> dict:
    """The §5.7 metrics for one factory, straight from git and gh (no cache)."""
    root = Path(table["path"])
    at = datetime.now(timezone.utc)
    return {"collected_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), **size(root), **people(root), **github(slug, at)}


def cached(slug: str, table: dict, max_age_h: float, refresh: bool = False) -> dict:
    """Cache hit when younger than max_age_h hours; otherwise collect and write."""
    have = None if refresh else read(slug)
    if have and datetime.now(timezone.utc) - parse_ts(have["collected_at"]) < timedelta(hours=max_age_h):
        return have
    data = collect(slug, table)
    p = cache_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)
    return data


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district metrics", description=__doc__.split("\n", 1)[0])
    parser.add_argument("slug", nargs="?", help="one repo (owner/name or basename); default all")
    parser.add_argument("--refresh", action="store_true", help="collect now even if the cache is fresh")
    parser.add_argument("--max-age", default="1h", help="reuse a cache younger than this (default 1h)")
    args = parser.parse_args(argv)
    max_age = hours(args.max_age, "--max-age")
    out, code = {}, 0
    for slug, table in host.select(host.load(), args.slug).items():
        try:
            out[slug] = cached(slug, table, max_age, args.refresh)
        except DistrictError as exc:
            print(f"{slug}: {exc}", file=sys.stderr)
            out[slug] = {"error": str(exc)}
            code = 1
    print(json.dumps(out, indent=2))
    return code
