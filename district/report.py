"""`district report`: print one read-only Markdown review of the observed fleet."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone

from district import host, status


GITHUB_LIMIT_NOTE = "GitHub query limits: merged PRs 500; open PRs 1000; caches have no completeness flag."
UNAVAILABLE_AGGREGATE = "unavailable (incompatible or unspecified producer windows/cohorts)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _percent(value) -> str:
    return f"{round(value * 100)}%" if _number(value) is not None else "unavailable"


def _source_times(entry: dict) -> list[str]:
    times = []
    for source in entry.get("sources") or []:
        if isinstance(source, dict) and isinstance(source.get("observed_at"), str):
            times.append(source["observed_at"])
    return sorted(set(times))


def _window(metrics: dict, name: str) -> object | None:
    value = metrics.get(f"{name}_window", metrics.get("window"))
    if isinstance(value, (dict, list, str)):
        return value
    return None


def _window_text(metrics: dict, name: str) -> str:
    window = _window(metrics, name)
    if window is None:
        return "producer window/cohort unspecified"
    if isinstance(window, dict):
        start, end = window.get("start"), window.get("end")
        if start or end:
            return f"producer window {start or 'unspecified'} to {end or 'unspecified'}"
    return f"producer window {json.dumps(window, sort_keys=True, separators=(',', ':'))}"


def _rate(metrics: dict, name: str, label: str) -> tuple[str, tuple | None]:
    value = _number(metrics.get(name))
    count_name = name.removesuffix("_rate")
    numerator = _integer(metrics.get(f"{name}_numerator", metrics.get(f"{count_name}_numerator")))
    denominator = _integer(metrics.get(f"{name}_denominator", metrics.get(f"{count_name}_denominator")))
    window = _window(metrics, name)
    if denominator == 0:
        return f"{label}: unavailable (zero denominator)", None
    if value is None:
        return f"{label}: unavailable", None
    if numerator is not None and denominator is not None:
        return f"{label}: {_percent(value)} ({numerator}/{denominator}; {_window_text(metrics, name)})", (numerator, denominator, window)
    return f"{label}: {_percent(value)} (denominator unavailable; {_window_text(metrics, name)})", None


def _cache_quality(metrics: dict, at: datetime) -> str:
    collected = metrics.get("collected_at")
    if not isinstance(collected, str):
        return "partial (collection time unavailable)"
    try:
        parsed = datetime.fromisoformat(collected.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        return "partial (invalid collection time)"
    return "stale" if at - parsed > timedelta(hours=1) else "available"


def _human_lines(snapshot_metrics: dict) -> list[str]:
    lines = []
    for key in sorted(snapshot_metrics):
        if not (key.startswith("human_") or key.startswith("intervention")):
            continue
        value = snapshot_metrics[key]
        label = key.replace("_", " ").capitalize()
        if isinstance(value, dict):
            names = sorted(value, key=lambda name: (name != "count", name != "attribution", name))
            detail = "; ".join(f"{name.replace('_', ' ')} {value[name]}" for name in names)
        else:
            detail = str(value)
        lines.append(f"- {label}: {detail}")
    return lines


def _repository(slug: str, entry: dict, at: datetime) -> tuple[list[str], dict]:
    snap = _mapping(entry.get("snap"))
    cached = _mapping(entry.get("metrics"))
    snapshot_metrics = _mapping(snap.get("metrics"))
    tickets = snap.get("tickets")
    current_escalated = (sum(item.get("stage") == "escalated" for item in tickets if isinstance(item, dict))
                         if isinstance(tickets, list) else None)
    timestamps = _source_times(entry)
    lines = [f"## {slug}", "", f"- Operational assessment: {entry.get('assessment', 'unknown')}",
             f"- Observation quality: {entry.get('observation', 'unavailable')}",
             f"- Source timestamps: {', '.join(timestamps) if timestamps else 'unavailable'}"]
    source_errors = [f"{source.get('id', 'source')}: {source['error']}" for source in entry.get("sources") or []
                     if isinstance(source, dict) and source.get("error")]
    if entry.get("error") and not source_errors:
        source_errors.append(str(entry["error"]))
    lines.append(f"- Source notes: {'; '.join(source_errors) if source_errors else 'none'}")
    lines.append(f"- Factory snapshot: {'available' if snap else 'unavailable'}")
    lines.append(f"- Cached GitHub metrics: {_cache_quality(cached, at) if cached else 'unavailable'}"
                 + (f"; collected {cached.get('collected_at', 'unavailable')}" if cached else ""))
    lines += ["", "### Measurements", ""]
    if cached:
        lines += [
            f"- Merged PRs (trailing 30 days, bounded at 500): {cached.get('merged_prs_30d', 'unavailable')}",
            f"- Merged PRs from `agent/` branches (trailing 30 days): {cached.get('agent_prs_30d', 'unavailable')}",
            f"- Current open PRs (bounded at 1000): {cached.get('open_prs', 'unavailable')}",
        ]
    else:
        lines += ["- Merged PRs (trailing 30 days, bounded at 500): unavailable",
                  "- Merged PRs from `agent/` branches (trailing 30 days): unavailable",
                  "- Current open PRs (bounded at 1000): unavailable"]
    first_text, first_pool = _rate(snapshot_metrics, "first_pass", "First-gate pass")
    bounce_text, bounce_pool = _rate(snapshot_metrics, "bounce_rate", "Review-bounce rate")
    lines += [f"- {first_text}", f"- {bounce_text}",
              f"- Current escalated tickets: {current_escalated if current_escalated is not None else 'unavailable'}",
              f"- Trailing-seven-day escalations: {snapshot_metrics.get('escalations_7d', 'unavailable')}",
              f"- Producer escalation total/window: {snapshot_metrics.get('escalations', 'unavailable')} (window unspecified)"
              if "escalations" in snapshot_metrics else "- Producer escalation total/window: unavailable"]
    human = _human_lines(snapshot_metrics)
    lines += human or ["- Human resolution/intervention measurements: unavailable"]
    lines.append("")
    counts = tuple(_integer(cached.get(key)) for key in ("merged_prs_30d", "agent_prs_30d", "open_prs")) if cached else (None,) * 3
    return lines, {"slug": slug, "counts": counts, "current_escalated": current_escalated,
                   "first_pass": first_pool, "bounce_rate": bounce_pool, "timestamps": timestamps}


def _aggregate_rate(items: list[dict], name: str, label: str, registered: int) -> str:
    values = [item[name] for item in items]
    if registered == 0 or len(values) != registered or any(value is None for value in values):
        return f"{label} aggregate: {UNAVAILABLE_AGGREGATE}"
    windows = {json.dumps(value[2], sort_keys=True, separators=(",", ":")) for value in values}
    if len(windows) != 1 or "null" in windows:
        return f"{label} aggregate: {UNAVAILABLE_AGGREGATE}"
    numerator = sum(value[0] for value in values)
    denominator = sum(value[1] for value in values)
    if denominator == 0:
        return f"{label} aggregate: unavailable (zero denominator)"
    return f"{label} aggregate: {_percent(numerator / denominator)} ({numerator}/{denominator})"


def render(entries: dict[str, dict], generated_at: datetime | None = None) -> str:
    """Render supplied observations without collecting or mutating anything."""
    at = generated_at or _now()
    registered = len(entries)
    lines = ["# District fleet review", "", f"Generated: {_stamp(at)}",
             f"Registered repositories: {registered}",
             "Observation: one bounded, non-atomic fleet round; per-source times are retained.",
             f"Coverage: every registered repository is listed; unavailable fields are not zero. {GITHUB_LIMIT_NOTE}", ""]
    summaries = []
    for slug in sorted(entries):
        section, summary = _repository(slug, entries[slug], at)
        lines += section
        summaries.append(summary)
    covered = sum(all(value is not None for value in item["counts"]) for item in summaries)
    totals = [sum(item["counts"][index] for item in summaries if item["counts"][index] is not None) for index in range(3)]
    times = sorted({timestamp for item in summaries for timestamp in item["timestamps"]})
    rate_windows = "; ".join(
        f"{item['slug']}={json.dumps((item['first_pass'] or item['bounce_rate'] or (None, None, None))[2], sort_keys=True)}"
        for item in summaries
    ) or "none"
    partial = covered != registered
    lines += ["## Fleet summary", "", f"Comparable count coverage: {covered}/{registered} repositories",
              "Totals below are partial because comparable cached metrics are unavailable for one or more repositories."
              if partial else "Totals below cover every registered repository but remain bounded by the disclosed GitHub query limits.",
              f"- Merged PRs (trailing 30 days): {totals[0]}",
              f"- Merged PRs from `agent/` branches (trailing 30 days): {totals[1]}",
              f"- Current open PRs: {totals[2]}",
              f"- Observation-time range: {times[0]} to {times[-1]}" if times else "- Observation-time range: unavailable",
              f"- Producer rate windows: {rate_windows}",
              f"- {_aggregate_rate(summaries, 'first_pass', 'First-gate pass', registered)}",
              f"- {_aggregate_rate(summaries, 'bounce_rate', 'Review-bounce', registered)}",
              f"- {GITHUB_LIMIT_NOTE}", ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district report", description=__doc__.split("\n", 1)[0])
    parser.parse_args(argv)
    entries = status.fleet(host.load())
    print(render(entries, _now()))
    if any(entry.get("assessment") == "attention" for entry in entries.values()):
        return 1
    return 2 if any(entry.get("assessment") == "unknown" for entry in entries.values()) else 0

