"""`district status`: one table for the fleet, from `factory dashboard --json` per repo.

Exit 1 for operational attention, 2 for unknown observation without attention,
0 for normal operation (including an empty fleet). JSON retains project context.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from district import health, host, metrics
from district.host import run

COLUMNS = ("repo", "operating", "execution", "observation", "assessment", "version", "next", "last", "pass", "esc", "gate1", "bounce", "fails", "upstream", "evidence")


def snapshot(slug: str, table: dict, timeout: float | None = None) -> dict:
    """Read the existing full snapshot; observation errors never imply stopped machinery."""
    try:
        proc = run(["factory", "dashboard", "--json"], cwd=Path(table["path"]), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"slug": slug, "error": (
            f"factory dashboard timed out after {timeout:g}s"
            if isinstance(exc, subprocess.TimeoutExpired) and timeout is not None else str(exc))}
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

RUNTIME_INTERVAL = 5.0
FULL_INTERVAL = 60.0
COLLECTION_TIMEOUT = 4.0
COLLECTION_CONCURRENCY = 8
EVENT_LIMIT = 512


def _runtime_error(data: dict) -> str | None:
    errors = data.get("errors")
    if not isinstance(errors, list):
        return "runtime errors malformed"
    details = []
    for error in errors:
        if isinstance(error, dict) and all(isinstance(error.get(key), str) for key in ("source", "scope", "code")):
            details.append(f"{error['source']}/{error['scope']}:{error['code']}")
        else:
            details.append("malformed structured error")
    history = data.get("history")
    if isinstance(history, dict):
        gaps = history.get("gaps")
        if isinstance(gaps, list):
            details.extend(f"history:{gap}" for gap in gaps if isinstance(gap, str))
        if history.get("truncated") is True:
            details.append("history:truncated")
    return "; ".join(dict.fromkeys(details)) or None


def runtime_source(data: dict, slug: str) -> tuple[dict, dict]:
    """Adapt the pinned Factory schema-1 wire contract to D01 observations."""
    if data.get("schema_version") != 1:
        raise ValueError(f"unsupported schema {data.get('schema_version')!r}")
    required = ("generated_at", "repo", "dispatcher", "executions", "resources", "events", "history", "errors")
    if any(key not in data for key in required) or data["repo"] != slug:
        raise ValueError("malformed schema 1 runtime observation")
    dispatcher = data["dispatcher"]
    executions = data["executions"]
    resources = data["resources"]
    if not isinstance(dispatcher, dict) or not isinstance(executions, list) or not isinstance(resources, list):
        raise ValueError("malformed schema 1 runtime observation")

    normalized_executions = []
    for item in executions:
        if not isinstance(item, dict) or not isinstance(item.get("execution_id"), str):
            continue
        state = item.get("state")
        wait = item.get("wait")
        if state == "active":
            state = "blocked" if isinstance(wait, dict) and wait.get("blocking") is True else (
                "known wait" if isinstance(wait, dict) else "stage-active")
        elif state not in ("completed", "failed", "interrupted", "unknown"):
            state = "unknown"
        outcome = item.get("outcome")
        kind = ("mechanism" if outcome == "mechanism_failure" else
                "product" if outcome in ("product_feedback", "project_escalation") else
                "unknown" if state in ("failed", "blocked") else None)
        normalized_executions.append({
            "id": item["execution_id"], "state": state, "stage": item.get("stage"),
            "observed_at": item.get("observed_at"), "entered_at": item.get("entered_at"),
            "reason": item.get("reason"), "outcome_kind": kind,
            "reference": item.get("latest_event_id"),
        })

    normalized_resources = []
    for item in resources:
        descriptor = item.get("resource") if isinstance(item, dict) else None
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("id"), str):
            continue
        owner = item.get("owner")
        confirmed = item.get("ownership") == "confirmed" and isinstance(owner, dict)
        normalized_resources.append({
            "id": descriptor["id"],
            "held": True if item.get("state") == "held" else False if item.get("state") == "free" else None,
            "owner": {"factory": slug, "execution_id": owner.get("execution_id")} if confirmed else None,
            "observed_at": item.get("observed_at"),
        })
    history = data["history"]
    normalized = {
        "dispatcher": {
            "service_active": dispatcher.get("service_active"),
            "timer_active": dispatcher.get("timer_active"),
            "next_dispatch_at": dispatcher.get("next_at"),
            "expected_enabled": True,
        },
        "executions": normalized_executions,
        "resources": normalized_resources,
    }
    if isinstance(history, dict):
        normalized["history"] = {
            "start": history.get("start_at"), "end": history.get("end_at"),
            "complete": history.get("complete"), "truncated": history.get("truncated"),
        }
    source = {
        "id": "factory.runtime", "observed_at": data.get("generated_at"),
        "cadence_seconds": RUNTIME_INTERVAL, "data": normalized, "error": _runtime_error(data),
    }
    seen = set()
    events = []
    for event in data["events"] if isinstance(data["events"], list) else []:
        identity = event.get("event_id") if isinstance(event, dict) else None
        if isinstance(identity, str) and identity not in seen:
            seen.add(identity)
            events.append(event)
    activity = {
        "events": events[-EVENT_LIMIT:],
        "history": history if isinstance(history, dict) else {},
    }
    return source, activity


class FleetCollector:
    """One server-owned bounded cache; HTTP reads never collect."""

    def __init__(self, data: dict, *, runtime_interval: float = RUNTIME_INTERVAL,
                 full_interval: float = FULL_INTERVAL, timeout: float = COLLECTION_TIMEOUT,
                 concurrency: int = COLLECTION_CONCURRENCY):
        self.repos = host.repos(data)
        self.runtime_interval = runtime_interval
        self.full_interval = full_interval
        self.timeout = timeout
        self.concurrency = max(1, min(concurrency, max(1, len(self.repos))))
        self.revision = 0
        self._records = {slug: {"runtime": None, "activity": {"events": [], "history": {}},
                               "snap": None, "runtime_error": None, "full_error": None,
                               "runtime_at": 0.0, "full_at": 0.0,
                               "runtime_collected_at": None, "full_collected_at": None}
                         for slug in self.repos}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._running: dict[str, Future] = {}

    def start(self) -> None:
        if self._thread:
            return
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="district-collector")
        self._thread = threading.Thread(target=self._loop, name="district-collector", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                running = set(self._running)
                due = [
                    (slug, "runtime" if now - record["runtime_at"] >= self.runtime_interval else "full")
                    for slug, record in self._records.items()
                    if slug not in running and (
                        now - record["runtime_at"] >= self.runtime_interval or
                        now - record["full_at"] >= self.full_interval)
                ]
                for slug, kind in due:
                    future = self._pool.submit(
                        self.collect_runtime if kind == "runtime" else self.collect_full, slug)
                    self._running[slug] = future
                    future.add_done_callback(lambda _future, slug=slug: self._finished(slug))
            self._stop.wait(0.05)

    def _finished(self, slug: str) -> None:
        with self._lock:
            self._running.pop(slug, None)

    def collect_runtime(self, slug: str) -> None:
        try:
            proc = run(["factory", "dashboard", "--runtime-json"], cwd=Path(self.repos[slug]["path"]),
                       timeout=self.timeout)
            if proc.returncode:
                raise ValueError(f"unsupported runtime command (exit {proc.returncode})")
            data = json.loads(proc.stdout)
            if not isinstance(data, dict):
                raise ValueError("malformed runtime observation")
            self.accept_runtime(slug, data)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            with self._lock:
                record = self._records[slug]
                record["runtime_error"] = (
                    f"runtime timeout after {self.timeout:g}s" if isinstance(exc, subprocess.TimeoutExpired)
                    else str(exc))
                record["runtime_at"] = time.monotonic()
                record["runtime_collected_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                if record["runtime"] is None or "unsupported" in record["runtime_error"]:
                    record["runtime"] = {"id": "factory.runtime", "observed_at": None,
                                         "cadence_seconds": self.runtime_interval, "data": None,
                                         "error": record["runtime_error"]}
                else:
                    record["runtime"] = {**record["runtime"], "error": record["runtime_error"]}
                self.revision += 1

    def accept_runtime(self, slug: str, data: dict) -> None:
        try:
            source, activity = runtime_source(data, slug)
            source["cadence_seconds"] = self.runtime_interval
        except ValueError as exc:
            with self._lock:
                record = self._records[slug]
                record.update(runtime={"id": "factory.runtime", "observed_at": None,
                                       "cadence_seconds": self.runtime_interval, "data": None,
                                       "error": str(exc)}, activity={"events": [], "history": {}},
                              runtime_error=str(exc), runtime_at=time.monotonic(),
                              runtime_collected_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
                self.revision += 1
            return
        with self._lock:
            record = self._records[slug]
            record.update(runtime=source, activity=activity, runtime_error=source["error"],
                          runtime_at=time.monotonic(),
                          runtime_collected_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            self.revision += 1

    def collect_full(self, slug: str) -> None:
        result = snapshot(slug, self.repos[slug], timeout=self.timeout)
        with self._lock:
            record = self._records[slug]
            if result.get("snap") is not None:
                record["snap"] = result["snap"]
            record["full_error"] = result.get("error")
            record["full_at"] = time.monotonic()
            self.revision += 1
            record["full_collected_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def fleet(self) -> dict[str, dict]:
        with self._lock:
            records = {
                slug: {**record, "runtime": dict(record["runtime"]) if record["runtime"] else None,
                       "activity": {"events": list(record["activity"]["events"]),
                                    "history": dict(record["activity"]["history"])}}
                for slug, record in self._records.items()
            }
        result = {}
        now = datetime.now(timezone.utc)
        for slug, record in records.items():
            sources = [record["runtime"]] if record["runtime"] else [{
                "id": "factory.runtime", "observed_at": None, "cadence_seconds": self.runtime_interval,
                "data": None, "error": "runtime observation pending",
            }]
            if record["snap"] is not None or record["full_error"]:
                full_sources = health.snapshot_sources(record["snap"], record["full_error"])
                for source in full_sources:
                    source["cadence_seconds"] = self.full_interval
                sources.extend(full_sources)
            item = entry({"slug": slug, "snap": record["snap"], "error": record["full_error"],
                          "sources": sources}, self.repos[slug])
            item["activity"] = record["activity"]
            item["collection"] = {
                "runtime_collected_at": record["runtime_collected_at"],
                "runtime_age_seconds": (
                    (now - datetime.fromisoformat(record["runtime_collected_at"].replace("Z", "+00:00"))).total_seconds()
                    if record["runtime_collected_at"] else None),
                "full_collected_at": record["full_collected_at"],
                "full_age_seconds": (
                    (now - datetime.fromisoformat(record["full_collected_at"].replace("Z", "+00:00"))).total_seconds()
                    if record["full_collected_at"] else None),
            }
            result[slug] = item
        return result

    def read(self) -> tuple[int, dict[str, dict]]:
        with self._lock:
            return self.revision, self.fleet()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(self.timeout + 1)
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=True)
        self._thread = None
        self._pool = None


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
