"""`district status`: one table for the fleet, from `factory dashboard --json` per repo.

Exit 1 when any repo is unhealthy, its last pass failed, or its timer is
inactive or District-disabled, so a prompt or cron can use it as one exit code.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from district import host
from district.host import run

COLUMNS = ("repo", "version", "next", "last", "pass", "active", "esc", "gate1", "bounce", "fails", "upstream")


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


def row(result: dict, table: dict) -> tuple[dict, bool]:
    """(cells, healthy)."""
    slug = result["slug"]
    disabled = bool(table.get("disabled_at"))
    if "error" in result:
        return {"repo": slug, "version": "?", "pass": "UNHEALTHY", "upstream": result["error"][:60]}, False
    snap = result["snap"]
    d = snap["dispatcher"]
    finished = [r for r in d["runs"] if r["result"] != "running"]
    last = finished[-1]["result"] if finished else "-"
    up = snap.get("upstream") or {}
    if snap["config"].get("upstream"):
        upstream = f"behind {up.get('behind', '?')}" + (f", parked #{up['blocker']['number']}" if up.get("blocker") else "")
    else:
        upstream = "-"
    active = "DISABLED" if disabled else ("yes" if d["timer"]["active"] else "NO")
    cells = {
        "repo": slug,
        "version": snap.get("version", "?"),
        "next": rel(d["timer"].get("next")),
        "last": rel(d["timer"].get("last")),
        "pass": last,
        "active": active,
        "esc": str(sum(1 for t in snap["tickets"] if t["stage"] == "escalated")),
        "gate1": pct(snap["metrics"]["first_pass"]),
        "bounce": pct(snap["metrics"]["bounce_rate"]),
        "fails": str(d["consecutive_failures"]),
        "upstream": upstream,
    }
    healthy = last != "failed" and active == "yes"
    return cells, healthy


def table(rows: list[dict]) -> str:
    widths = {c: max(len(c), *(len(r.get(c, "")) for r in rows)) for c in COLUMNS}
    fmt = lambda r: "  ".join(r.get(c, "").ljust(widths[c]) for c in COLUMNS).rstrip()  # noqa: E731
    return "\n".join([fmt({c: c for c in COLUMNS}), *(fmt(r) for r in rows)])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district status", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="dump the snapshots keyed by slug")
    args = parser.parse_args(argv)
    data = host.load()
    repos = host.repos(data)
    if not repos:
        print("no repositories registered (district add)")
        return 0
    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        results = list(pool.map(lambda kv: snapshot(*kv), repos.items()))
    if args.json:
        print(json.dumps({r["slug"]: r.get("snap") or {"error": r["error"]} for r in results}, indent=2))
    rows, ok = [], True
    for result in results:
        cells, healthy = row(result, repos[result["slug"]])
        rows.append(cells)
        ok &= healthy
    if not args.json:
        print(table(rows))
    return 0 if ok else 1
