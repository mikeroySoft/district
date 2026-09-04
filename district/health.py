"""One health level per factory from its `factory dashboard --json` snapshot (PRD §5.7).

failing   timer inactive or District-disabled · last pass failed · snapshot errors
          · consecutive_failures >= 1 · dashboard unreachable
attention open ready-for-human · upstream sync parked · bounce rate > 33% · doctor WARN
healthy   none of the above
"""

from __future__ import annotations

LEVELS = ("failing", "attention", "healthy")
BOUNCE_LIMIT = 0.33


def level(snap: dict | None, table: dict, doctor_rows: list[dict] | None = None) -> tuple[str, list[str]]:
    """(level, reasons). `snap` is None when the dashboard could not be read; `table` is the registry entry."""
    failing: list[str] = []
    attention: list[str] = []
    if table.get("disabled_at"):
        failing.append("timer disabled by District: " + (table.get("disabled_reason") or "see host file"))
    if snap is None:
        failing.append("dashboard unreachable")
    else:
        d = snap["dispatcher"]
        if not d["timer"]["active"] and not table.get("disabled_at"):
            failing.append("timer inactive")
        finished = [r for r in d["runs"] if r["result"] != "running"]
        if finished and finished[-1]["result"] == "failed":
            failing.append("last pass failed")
        if snap.get("errors"):
            failing.append("snapshot errors: " + "; ".join(snap["errors"]))
        if d["consecutive_failures"] >= 1:
            failing.append(f"{d['consecutive_failures']} consecutive failed pass(es)")
        human = [t["number"] for t in snap["tickets"] if t["state"] == "OPEN" and "ready-for-human" in t["labels"]]
        if human:
            attention.append(f"{len(human)} open ready-for-human (#{', #'.join(map(str, human))})")
        blocker = (snap.get("upstream") or {}).get("blocker")
        if blocker:
            attention.append(f"upstream sync parked on #{blocker['number']}")
        bounce = snap["metrics"].get("bounce_rate")
        if bounce is not None and bounce > BOUNCE_LIMIT:
            attention.append(f"bounce rate {round(100 * bounce)}%")
    warns = [r["label"] for r in doctor_rows or [] if r.get("status") == "WARN"]
    if warns:
        attention.append("doctor WARN: " + ", ".join(warns))
    if failing:
        return "failing", failing
    if attention:
        return "attention", attention
    return "healthy", []
