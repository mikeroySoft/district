"""`district status`: one table for the fleet, from `factory dashboard --json` per repo.

Exit 1 for operational attention, 2 for unknown observation without attention,
0 for normal operation (including an empty fleet). JSON retains project context.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from district import health, host, metrics
from district.host import run

COLUMNS = ("repo", "operating", "execution", "observation", "assessment", "version", "next", "last", "pass", "esc", "gate1", "bounce", "fails", "upstream", "evidence")


def snapshot(slug: str, table: dict) -> dict:
    """Read the existing full snapshot; observation errors never imply stopped machinery."""
    try:
        proc = run(["factory", "dashboard", "--json"], cwd=Path(table["path"]))
    except OSError as exc:
        return {"slug": slug, "error": str(exc)}
    error = None
    if proc.returncode != 0:
        error = f"factory dashboard exited {proc.returncode}: {(proc.stderr.strip() or '?').splitlines()[-1]}"
    try:
        snap = json.loads(proc.stdout)
        if not isinstance(snap, dict):
            raise ValueError("snapshot must be an object")
    except ValueError:
        return {"slug": slug, "error": error or "malformed snapshot"}
    errors = snap.get("errors") or []
    if errors:
        detail = "; ".join(str(e) for e in errors)
        error = f"{error}; {detail}" if error else detail
    return {"slug": slug, "snap": snap, "error": error}


def rel(ts: str | None, at: datetime | None = None) -> str:
    if not ts:
        return "-"
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "?"
        delta = parsed - (at or datetime.now(timezone.utc))
    except (ValueError, TypeError, AttributeError):
        return "?"
    s = int(abs(delta.total_seconds()))
    span = f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m" if s >= 60 else f"{s}s"
    return f"in {span}" if delta.total_seconds() > 0 else f"{span} ago"


def pct(x: float | None) -> str:
    return "-" if not isinstance(x, (int, float)) or isinstance(x, bool) else f"{round(100 * x)}%"


def entry(result: dict, table: dict) -> dict:
    """Shared classified record for CLI, Atlas and D02's normalized source transport."""
    snap = result.get("snap")
    sources = result.get("sources")
    if sources is None:
        sources = health.snapshot_sources(snap, result.get("error"))
    return {
        "table": table, "snap": snap, "error": result.get("error"),
        "metrics": metrics.read(result["slug"]),
        **health.classify(result["slug"], sources, table),
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
    snap = e["snap"] or {}
    d, up, sm, config = [snap.get(k) if isinstance(snap.get(k), dict) else {} for k in ("dispatcher", "upstream", "metrics", "config")]
    timer = d.get("timer") if isinstance(d.get("timer"), dict) else {}
    runs = d.get("runs") if isinstance(d.get("runs"), list) else []
    finished = [r for r in runs if isinstance(r, dict) and r.get("result") != "running"]
    tickets = snap.get("tickets") if isinstance(snap.get("tickets"), list) else None
    if config.get("upstream"):
        blocker = up.get("blocker") if isinstance(up.get("blocker"), dict) else {}
        upstream = f"behind {up.get('behind', '?')}" + (f", parked #{blocker['number']}" if "number" in blocker else "")
    else:
        upstream = "-"
    return {
        "repo": slug, "operating": e["operating_state"], "execution": e["execution_state"],
        "observation": e["observation"], "assessment": e["assessment"],
        "version": str(snap.get("version", "?")),
        "next": rel(timer.get("next")), "last": rel(timer.get("last")),
        "pass": str(finished[-1].get("result", "?")) if finished else "-",
        "esc": str(sum(t.get("stage") == "escalated" for t in tickets if isinstance(t, dict))) if tickets is not None and not e["error"] else "?",
        "gate1": pct(sm.get("first_pass")), "bounce": pct(sm.get("bounce_rate")),
        "fails": str(d.get("consecutive_failures", "?")), "upstream": upstream,
        "evidence": "; ".join(f["condition_code"] for f in e["findings"]) or e["error"] or "-",
    }


def table(rows: list[dict]) -> str:
    widths = {c: max(len(c), *(len(r.get(c, "")) for r in rows)) for c in COLUMNS}
    fmt = lambda r: "  ".join(r.get(c, "").ljust(widths[c]) for c in COLUMNS).rstrip()  # noqa: E731
    return "\n".join([fmt({c: c for c in COLUMNS}), *(fmt(r) for r in rows)])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district status", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="dump shared operational classification, evidence, snapshot and project metrics keyed by slug")
    args = parser.parse_args(argv)
    entries = fleet(host.load())
    if not entries and not args.json:
        print("no repositories registered (district add)")
        return 0
    if args.json:
        print(json.dumps(entries, indent=2))
    else:
        print(table([row(slug, e) for slug, e in entries.items()]))
    if any(e["assessment"] == "attention" for e in entries.values()):
        return 1
    return 2 if any(e["assessment"] == "unknown" for e in entries.values()) else 0
