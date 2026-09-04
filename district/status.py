"""`district status`: one table for the fleet, from `factory dashboard --json` per repo.

Exit 1 when any repo is failing (health.py), so a prompt or cron can use it as
one exit code. `--json` adds health, reasons, and the cached metrics per slug.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from district import health, host, metrics
from district.host import run

COLUMNS = ("repo", "health", "version", "next", "last", "pass", "active", "esc", "gate1", "bounce", "fails", "upstream")


def snapshot(slug: str, table: dict) -> dict:
    """{'slug', 'snap' | 'error'}: a nonzero exit, bad JSON, or snapshot errors mark the repo unhealthy."""
    proc = run(["factory", "dashboard", "--json"], cwd=Path(table["path"]))
    if proc.returncode != 0:
        return {"slug": slug, "error": f"factory dashboard exited {proc.returncode}: {(proc.stderr.strip() or '?').splitlines()[-1]}"}
    try:
        snap = json.loads(proc.stdout)
        if snap.get("errors"):
            return {"slug": slug, "error": "; ".join(snap["errors"]), "snap": snap}
        snap["tickets"], snap["dispatcher"]["runs"]  # shape check
    except (ValueError, KeyError, TypeError):
        return {"slug": slug, "error": "malformed snapshot"}
    return {"slug": slug, "snap": snap}


def rel(ts: str | None, at: datetime | None = None) -> str:
    if not ts:
        return "-"
    delta = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - (at or datetime.now(timezone.utc))
    s = int(abs(delta.total_seconds()))
    span = f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m" if s >= 60 else f"{s}s"
    return f"in {span}" if delta.total_seconds() > 0 else f"{span} ago"


def pct(x: float | None) -> str:
    return "-" if x is None else f"{round(100 * x)}%"


def entry(result: dict, table: dict) -> dict:
    """One `status --json` record: registry table, snapshot (or error), health, reasons, cached metrics."""
    snap = result.get("snap")
    lvl, reasons = health.level(snap, table)
    if snap is None:
        reasons = [f"dashboard unreachable ({result['error']})" if r == "dashboard unreachable" else r for r in reasons]
    return {
        "table": table, "snap": snap, "error": result.get("error"),
        "health": lvl, "reasons": reasons, "metrics": metrics.read(result["slug"]),
    }


def fleet(data: dict) -> dict[str, dict]:
    """{slug: entry} for every registered repo; snapshots are taken in parallel."""
    repos = host.repos(data)
    if not repos:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        results = list(pool.map(lambda kv: snapshot(*kv), repos.items()))
    return {r["slug"]: entry(r, repos[r["slug"]]) for r in results}


def row(slug: str, e: dict) -> dict:
    cells = {"repo": slug, "health": e["health"]}
    if e["error"]:
        return {**cells, "version": "?", "pass": "UNHEALTHY", "upstream": e["error"][:60]}
    snap = e["snap"]
    d = snap["dispatcher"]
    finished = [r for r in d["runs"] if r["result"] != "running"]
    up = snap.get("upstream") or {}
    if snap["config"].get("upstream"):
        upstream = f"behind {up.get('behind', '?')}" + (f", parked #{up['blocker']['number']}" if up.get("blocker") else "")
    else:
        upstream = "-"
    return {
        **cells,
        "version": snap.get("version", "?"),
        "next": rel(d["timer"].get("next")),
        "last": rel(d["timer"].get("last")),
        "pass": finished[-1]["result"] if finished else "-",
        "active": "DISABLED" if e["table"].get("disabled_at") else ("yes" if d["timer"]["active"] else "NO"),
        "esc": str(sum(1 for t in snap["tickets"] if t["stage"] == "escalated")),
        "gate1": pct(snap["metrics"]["first_pass"]),
        "bounce": pct(snap["metrics"]["bounce_rate"]),
        "fails": str(d["consecutive_failures"]),
        "upstream": upstream,
    }


def table(rows: list[dict]) -> str:
    widths = {c: max(len(c), *(len(r.get(c, "")) for r in rows)) for c in COLUMNS}
    fmt = lambda r: "  ".join(r.get(c, "").ljust(widths[c]) for c in COLUMNS).rstrip()  # noqa: E731
    return "\n".join([fmt({c: c for c in COLUMNS}), *(fmt(r) for r in rows)])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district status", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="dump snapshot, health, reasons, and metrics keyed by slug")
    args = parser.parse_args(argv)
    entries = fleet(host.load())
    if not entries:
        print("no repositories registered (district add)")
        return 0
    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        print(table([row(slug, e) for slug, e in entries.items()]))
    return 0 if all(e["health"] != "failing" for e in entries.values()) else 1
