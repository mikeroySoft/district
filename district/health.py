"""Shared operational semantics, not a Factory transport or a collector.

D02 input (JSON-compatible; optional/missing facts remain unknown):
  classify(slug, sources, table, at=None), where each source is
  {id: str, observed_at: timezone-aware ISO timestamp|null,
   cadence_seconds: positive number|null, error: str|null, data: object|null}.
  IDs identify independent observations; error with data preserves partial facts.
  data accepts only these semantic observations, NOT guessed F03 wire keys:

  dispatcher: {service_active: bool|null, timer_active: bool|null,
    next_dispatch_at?: timestamp, expected_enabled?: bool,
    pause?: {recorded_at: timestamp, reason: nonempty str},
    capped?: bool, cap_reason?: str, cap_recorded_at?: timestamp}.
    False liveness is authoritative here. An unrecorded pause is not intent.
    expected_enabled=true and both liveness values false proves unexpected stop.
  executions: [{id: str, state: "stage-active"|"known wait"|"blocked"|
    "completed"|"failed"|"interrupted"|"unknown", stage?: reported str,
    observed_at?: timestamp, entered_at?: timestamp, reason?: str,
    outcome_kind?: "product"|"mechanism"|"unknown", reference?: str}].
    observed_at means last confirmed observation, NOT stage entry time. Omit it
    to use the source observation. Mechanism means inability to execute the
    configured mechanism, never an ordinary negative verdict. A failed/blocked
    outcome without a known kind has unknown operational significance.
    [] explicitly reports no executions; omitting the key reports no telemetry.
  resources: [{id: opaque str, held: bool|null,
    owner?: {factory: str, execution_id: str}|null, observed_at?: timestamp}].
    Only an explicit owner confirms ownership; a known terminal execution clears
    its ownership claim. A held lock by itself is not a finding.
  checks: [{condition: one of CHECKS below, resource: opaque str,
    status: "failed"|"passed"|"unknown", detail: nonempty str,
    impact: nonempty str, cause?: str, reference?: str, observed_at?: timestamp,
    scope?: {kind: "factory"|"shared", id: str}, intentional?: bool,
    first_observed_at?: timestamp, last_observed_at?: timestamp}].
    Failed checks need detail AND supported operational impact. Shared scope
    must be established by the producer, never inferred from equal URLs or
    coincident failures. Intentional configuration pins are not drift.
    Within each source/scope/condition/resource, the newest check replaces older
    verdicts (including recovery); independent source evidence is preserved.
  history?: {start: timestamp, end: timestamp, complete: bool, truncated: bool}.
    First/last observations are kept only if ordered inside this declared window;
    they describe this window, not incident onset. No history is synthesized.

Output: {schema_version: 1, operating_state, execution_state, observation,
  assessment: "normal"|"attention"|"unknown", findings: [...], sources: [...],
  executions: [...], resources: [...], unknowns: [str, ...]}.
Sources expose id, observed_at, cadence_seconds, age_seconds, observation, error;
no data is recopied. Executions/resources retain the fields above plus source_id
and observation; resources always include owner and ownership (known/unknown/none).
Findings expose id, factory, scope, condition_code, resource, severity, observed_at,
impact, cause (null unless supported), and evidence [{source_id, factory,
observed_at, observation, reference, detail}]. Optional history and bounded
first_observed_at/last_observed_at are as above. IDs are canonical JSON tuples of
scope kind, scope id, condition and resource, independent of changing evidence.
group_findings(entries) groups only equal IDs, retaining whole per-factory findings
under observations; it neither diagnoses common causes nor reclassifies health.

Freshness: age is computed from source time on every read. Fresh is at most TWO
collection cadences old (one missed interval tolerated); greater age is stale.
Missing/invalid/future time or missing cadence is partial, never renewed by a
fetch. Missing data is unavailable; an empty object is partial. Mixed qualities
and malformed observations are partial; all usable observations stale is stale.
Absent execution telemetry makes otherwise fresh observations partial. Unresolved
operational meaning does not change freshness. Last known states survive observation loss.
Assessment is attention for supported findings, otherwise unknown for non-fresh
observation, unknown operating/execution state, or unresolved operational meaning;
otherwise normal. Missing runtime therefore is NOT silently normal, even at idle.
Aggregate execution precedence is active, blocked, known wait, unknown, failed,
interrupted, completed. All concurrent executions remain individually available.
Empty execution telemetry is known wait only with scheduled/paused/capped dispatch.
Operating precedence is capped, recorded pause, running, scheduled, stopped,
unknown: admission may be capped/paused while an existing execution still runs.

Legacy adapter: generated_at remains source time. Direct CLI snapshots have
unknown cadence (cadence_seconds=None); the server collector sets cadence_seconds
to its configured full-snapshot interval (60 seconds by default), without
replacing source time with collection time. True service/timer facts are usable;
false collapses failed probes and becomes unknown. Ticket phases/artifact times,
project escalations, bounce rates and parked upstream work are never execution
telemetry or findings. GitHub errors do not erase local dispatcher facts. Unit
pass failures and failed configured triage probes are scoped supported findings.
Registry disabled_at is a recorded failure cap, NOT pause or collection time.

Concrete source data examples (wrap each in the source envelope above):
  scheduled: {"dispatcher": {"service_active": false, "timer_active": true},
              "executions": []}
  pause: {"dispatcher": {"pause": {"recorded_at": "2026-09-05T12:00:00Z",
                                  "reason": "operator maintenance"}},
          "executions": []}
  stop: {"dispatcher": {"service_active": false, "timer_active": false,
                         "expected_enabled": true}, "executions": []}
  mechanism: {"dispatcher": {"service_active": true}, "executions": [
    {"id": "run-4/gate-2", "state": "failed", "stage": "gate",
     "outcome_kind": "mechanism", "reason": "configured executable missing"}]}
For example envelope id="runtime", observed_at="2026-09-05T12:00:00Z",
cadence_seconds=5, error=null is fresh at 12:00:10Z, stale at 12:00:11Z.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone


CHECKS = {
    "runtime.dispatcher_failed": "error",
    "runtime.mechanism_unavailable": "error",
    "scheduling.unexpected_stop": "error",
    "scheduling.capped": "warning",
    "dependency.unavailable": "error",
    "host.configuration_drift": "warning",
    "host.configuration_invalid": "error",
    "host.policy_violation": "error",
}
EXECUTION_STATES = ("stage-active", "blocked", "known wait", "unknown", "failed", "interrupted", "completed")
TERMINAL_STATES = {"failed", "interrupted", "completed"}


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _quality(observed_at, cadence, at: datetime, available: bool = True, error=None) -> tuple[str, float | None]:
    timestamp = _time(observed_at)
    age = (at - timestamp).total_seconds() if timestamp else None
    if not available:
        return "unavailable", age
    if (age is None or age < 0 or isinstance(cadence, bool)
            or not isinstance(cadence, (int, float)) or not math.isfinite(cadence) or cadence <= 0):
        return "partial", age
    if age > 2 * cadence:
        return "stale", age
    return ("partial" if error else "fresh"), age


def _latest(records: list[tuple[dict, dict]]) -> tuple[dict, dict] | None:
    return max(records, key=lambda pair: _time(pair[0].get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc)) if records else None


def snapshot_sources(snap: dict | None, error: str | None = None) -> list[dict]:
    """Adapt only documented legacy facts; do not claim execution/owner support."""
    if not isinstance(snap, dict):
        return [{"id": "factory.snapshot", "observed_at": None, "cadence_seconds": None,
                 "data": None, "error": error or "snapshot unavailable"}]
    timestamp = snap.get("generated_at")
    errors = snap.get("errors") or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    github_errors = [str(e) for e in errors if str(e).lower().startswith("github:")]
    other_errors = [str(e) for e in errors if str(e) not in github_errors]
    if error and error != "; ".join(map(str, errors)) and error not in errors:
        other_errors.append(error)
    dispatcher = snap.get("dispatcher")
    dispatcher = dispatcher if isinstance(dispatcher, dict) else {}
    timer = dispatcher.get("timer")
    timer = timer if isinstance(timer, dict) else {}
    data = {"dispatcher": {
        "service_active": True if dispatcher.get("service_active") is True else None,
        "timer_active": True if timer.get("active") is True else None,
        "next_dispatch_at": timer.get("next"),
    }, "checks": []}
    runs = dispatcher.get("runs")
    runs = [r for r in runs if isinstance(r, dict) and r.get("result") != "running"] if isinstance(runs, list) else []
    failures = dispatcher.get("consecutive_failures")
    if (runs and runs[-1].get("result") == "failed") or (isinstance(failures, int) and failures > 0):
        last = runs[-1] if runs else {}
        data["checks"].append({
            "condition": "runtime.dispatcher_failed", "resource": "dispatcher", "status": "failed",
            "detail": f"Dispatcher unit reported {failures or 1} consecutive failed pass(es).",
            "impact": "At least one dispatcher pass did not complete successfully; cause is unknown.",
            "observed_at": last.get("finished") or timestamp, "reference": "dispatcher.runs",
        })
    local = {"id": "factory.snapshot", "observed_at": timestamp, "cadence_seconds": None,
             "data": data, "error": "; ".join(other_errors) or None}
    sources = [local]
    if github_errors:
        sources.append({"id": "factory.github", "observed_at": timestamp, "cadence_seconds": None,
                        "data": None, "error": "; ".join(github_errors)})
    config = snap.get("config")
    triage = config.get("triage") if isinstance(config, dict) else None
    triage = triage if isinstance(triage, dict) else {}
    if triage.get("url") and triage.get("online") is False:
        data["checks"].append({
            "condition": "dependency.unavailable", "resource": str(triage["url"]), "status": "failed",
            "detail": "Configured triage endpoint probe failed for this factory.",
            "impact": "This factory could not reach its configured triage dependency.",
            "reference": "config.triage.online",
        })
    if isinstance(snap.get("gpu_lock_held"), bool):
        data["resources"] = [{"id": "legacy.gate-lock", "held": snap["gpu_lock_held"], "owner": None}]
    return sources


def classify(slug: str, sources: list[dict], table: dict, at: datetime | None = None) -> dict:
    """Classify supported observations without accessing the host or keeping history."""
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    inputs = list(sources)
    if table.get("disabled_at"):
        inputs.append({"id": "district.registry", "observed_at": None, "cadence_seconds": None,
                       "error": None, "data": {"dispatcher": {"capped": True,
                       "cap_recorded_at": table["disabled_at"], "cap_reason": table.get("disabled_reason")}}})
    metadata, dispatchers, checks, executions, resources = [], [], [], {}, {}
    findings, unknowns = {}, []
    execution_telemetry = False

    def evidence(source, detail, reference=None, timestamp=None):
        timestamp = timestamp if timestamp is not None else source["observed_at"]
        quality, _ = _quality(timestamp, source["cadence_seconds"], at, error=source["error"])
        return {"source_id": source["id"], "factory": slug, "observed_at": timestamp,
                "observation": quality, "reference": reference, "detail": detail}

    def finding(source, condition, resource, detail, impact, *, timestamp=None, scope=None, cause=None, reference=None, record=None, history=None):
        scope = scope if isinstance(scope, dict) else {"kind": "factory", "id": slug}
        if scope.get("kind") not in ("factory", "shared") or not isinstance(scope.get("id"), str) or not scope["id"].strip():
            unknowns.append("invalid finding scope")
            source["observation"] = "partial"
            return
        if scope["kind"] == "factory" and scope["id"] != slug:
            unknowns.append("finding scope does not match factory")
            source["observation"] = "partial"
            return
        scope = {"kind": scope["kind"], "id": scope["id"]}
        identity = json.dumps([scope["kind"], scope["id"], condition, resource], separators=(",", ":"))
        ev = evidence(source, detail, reference, timestamp)
        item = findings.setdefault(identity, {"id": identity, "factory": slug, "scope": scope,
            "condition_code": condition, "resource": resource, "severity": CHECKS[condition],
            "observed_at": ev["observed_at"], "evidence": [], "impact": impact, "cause": cause or None})
        if ev not in item["evidence"]:
            item["evidence"].append(ev)
        if (_time(ev["observed_at"]) or datetime.min.replace(tzinfo=timezone.utc)) >= (_time(item["observed_at"]) or datetime.min.replace(tzinfo=timezone.utc)):
            item.update(observed_at=ev["observed_at"], impact=impact, cause=cause or None)
        if isinstance(history, dict) and isinstance(record, dict):
            start, end = _time(history.get("start")), _time(history.get("end"))
            first, last = _time(record.get("first_observed_at")), _time(record.get("last_observed_at"))
            if (start and end and first and last and start <= first <= last <= end <= at
                    and isinstance(history.get("complete"), bool) and isinstance(history.get("truncated"), bool)):
                item.update(history={k: history[k] for k in ("start", "end", "complete", "truncated")},
                            first_observed_at=record["first_observed_at"], last_observed_at=record["last_observed_at"])

    for raw in inputs:
        data = raw.get("data") if isinstance(raw, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        quality, age = _quality(raw.get("observed_at"), raw.get("cadence_seconds"), at,
                                data is not None, raw.get("error"))
        source = {"id": raw.get("id"), "observed_at": raw.get("observed_at"),
                  "cadence_seconds": raw.get("cadence_seconds"), "age_seconds": age,
                  "observation": quality, "error": raw.get("error")}
        metadata.append(source)
        if not isinstance(source["id"], str) or not source["id"]:
            unknowns.append("source identity unavailable")
            source["observation"] = "partial" if data is not None else "unavailable"
            continue
        if not isinstance(data, dict):
            if data is not None:
                unknowns.append("source data malformed")
                source["observation"] = "partial"
            continue
        if not data:
            source["observation"] = "partial"
        if isinstance(data.get("dispatcher"), dict):
            dispatchers.append((source, data["dispatcher"]))
        elif "dispatcher" in data:
            unknowns.append("dispatcher observation malformed")
            source["observation"] = "partial"
        if isinstance(data.get("executions"), list):
            execution_telemetry = True
        for key, target in (("executions", executions), ("resources", resources)):
            records = data.get(key, [])
            if not isinstance(records, list):
                unknowns.append(f"{key} observation malformed")
                source["observation"] = "partial"
                continue
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                    unknowns.append(f"{key} identity unavailable")
                    source["observation"] = "partial"
                    continue
                timestamp = record.get("observed_at", source["observed_at"])
                record_quality, _ = _quality(timestamp, source["cadence_seconds"], at, error=source["error"])
                fields = (("id", "state", "stage", "entered_at", "reason", "outcome_kind", "reference")
                          if key == "executions" else ("id", "held", "owner"))
                item = {k: record[k] for k in fields if k in record}
                item.update(observed_at=timestamp, observation=record_quality, source_id=source["id"])
                previous = target.get(record["id"])
                if previous is None or (_time(timestamp) or datetime.min.replace(tzinfo=timezone.utc)) >= (_time(previous[0]["observed_at"]) or datetime.min.replace(tzinfo=timezone.utc)):
                    target[record["id"]] = (item, source)
        records = data.get("checks", [])
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict):
                    checks.append((record, source, data.get("history")))
                else:
                    unknowns.append("check observation malformed")
                    source["observation"] = "partial"
        else:
            unknowns.append("check observations malformed")
            source["observation"] = "partial"

    latest_checks = {}
    for record, source, history in checks:
        condition, resource = record.get("condition"), record.get("resource")
        scope = record.get("scope", {"kind": "factory", "id": slug})
        if (not isinstance(condition, str) or condition not in CHECKS
                or not isinstance(resource, str) or not resource.strip()
                or not isinstance(scope, dict) or scope.get("kind") not in ("factory", "shared")
                or not isinstance(scope.get("id"), str) or not scope["id"].strip()
                or (scope["kind"] == "factory" and scope["id"] != slug)):
            unknowns.append("operational check identity unsupported or invalid")
            source["observation"] = "partial"
            continue
        key = (source["id"], scope["kind"], scope["id"], condition, resource)
        timestamp = _time(record.get("observed_at", source["observed_at"])) or datetime.min.replace(tzinfo=timezone.utc)
        previous = latest_checks.get(key)
        if previous is None or timestamp >= previous[0]:
            latest_checks[key] = (timestamp, record, source, history)
    for _, record, source, history in latest_checks.values():
        condition = record.get("condition")
        if condition == "host.configuration_drift" and record.get("intentional") is True:
            continue
        if condition not in CHECKS or record.get("status") not in ("passed", "failed"):
            unknowns.append("operational check unsupported or unknown")
            if record.get("status") != "unknown":
                source["observation"] = "partial"
            continue
        if record["status"] == "passed":
            continue
        if not all(isinstance(record.get(k), str) and record[k].strip() for k in ("resource", "detail", "impact")):
            unknowns.append("failed check lacks supported evidence or impact")
            source["observation"] = "partial"
            continue
        finding(source, condition, record["resource"], record["detail"], record["impact"],
                timestamp=record.get("observed_at"), scope=record.get("scope"), cause=record.get("cause"),
                reference=record.get("reference"), record=record, history=history)

    selected = _latest(dispatchers)
    operating = "unknown"
    capped = next(((s, d) for s, d in dispatchers if s["id"] == "district.registry"), None)
    if not capped and selected and selected[1].get("capped") is True:
        capped = selected
    if capped:
        source, dispatcher = capped
        operating = "capped"
        finding(source, "scheduling.capped", "dispatcher", dispatcher.get("cap_reason") or "Dispatch cap is recorded.",
                "Automatic dispatch is disabled by its failure cap.", timestamp=dispatcher.get("cap_recorded_at"), reference="dispatcher.capped")
    elif selected:
        source, dispatcher = selected
        pause = dispatcher.get("pause")
        if isinstance(pause, dict) and _time(pause.get("recorded_at")) and _time(pause["recorded_at"]) <= at and isinstance(pause.get("reason"), str) and pause["reason"].strip():
            operating = "deliberately paused"
        elif dispatcher.get("service_active") is True:
            operating = "running"
        elif dispatcher.get("timer_active") is True:
            operating = "scheduled waiting"
        elif dispatcher.get("service_active") is False and dispatcher.get("timer_active") is False and dispatcher.get("expected_enabled") is True:
            operating = "unexpectedly stopped"
            finding(source, "scheduling.unexpected_stop", "dispatcher", "Service and timer are confirmed inactive despite enabled intent.",
                    "Automatic dispatch cannot start.", reference="dispatcher")

    for item, source in executions.values():
        if item.get("state") not in EXECUTION_STATES or (item.get("state") == "stage-active" and not item.get("stage")):
            item["state"] = "unknown"
            source["observation"] = "partial"
        if item["state"] == "known wait" and not item.get("reason"):
            item["state"] = "unknown"
            source["observation"] = "partial"
        if item["state"] in ("failed", "blocked"):
            if item.get("outcome_kind") == "mechanism":
                finding(source, "runtime.mechanism_unavailable", item.get("stage") or item["id"],
                        item.get("reason") or "Configured mechanism could not execute.",
                        f"Execution {item['id']} cannot complete its configured mechanism.",
                        timestamp=item["observed_at"], reference=item.get("reference") or item["id"])
            elif item.get("outcome_kind") != "product":
                unknowns.append(f"execution {item['id']}: operational significance of outcome unknown")
        if item["state"] == "unknown":
            unknowns.append(f"execution {item['id']}: state unknown")

    states = {item["state"] for item, _ in executions.values()}
    execution = next((s for s in EXECUTION_STATES if s in states), "unknown")
    if not execution_telemetry:
        unknowns.append("execution telemetry unavailable")
    elif not executions and operating in ("scheduled waiting", "deliberately paused", "capped"):
        execution = "known wait"
    for item, _ in resources.values():
        owner = item.get("owner")
        valid_owner = isinstance(owner, dict) and all(isinstance(owner.get(k), str) and owner[k] for k in ("factory", "execution_id"))
        if valid_owner and owner["factory"] == slug and owner["execution_id"] in executions:
            valid_owner = executions[owner["execution_id"]][0]["state"] not in TERMINAL_STATES
        item["owner"] = {k: owner[k] for k in ("factory", "execution_id")} if valid_owner and item.get("held") is True else None
        item["ownership"] = "known" if item["owner"] else "none" if item.get("held") is False else "unknown"
        if not isinstance(item.get("held"), bool):
            item["held"] = None

    qualities = [s["observation"] for s in metadata]
    usable = [q for q in qualities if q != "unavailable"]
    if not usable:
        observation = "unavailable"
    elif all(q == "stale" for q in usable):
        observation = "stale"
    elif all(q == "fresh" for q in qualities) and execution_telemetry and all(item["observation"] == "fresh" for item, _ in [*executions.values(), *resources.values()]):
        observation = "fresh"
    else:
        observation = "partial"
    assessment = "attention" if findings else "unknown" if (observation != "fresh" or operating == "unknown" or execution == "unknown" or unknowns) else "normal"
    return {"schema_version": 1, "operating_state": operating, "execution_state": execution,
            "observation": observation, "assessment": assessment, "findings": list(findings.values()),
            "sources": metadata, "executions": [item for item, _ in executions.values()],
            "resources": [item for item, _ in resources.values()], "unknowns": list(dict.fromkeys(unknowns))}


def group_findings(entries: dict[str, dict]) -> list[dict]:
    """Group only evidence-declared identical scope/condition/resource identities."""
    groups = {}
    for entry in entries.values():
        for finding in entry["findings"]:
            group = groups.setdefault(finding["id"], {k: finding[k] for k in ("id", "scope", "condition_code", "resource")})
            group.setdefault("observations", []).append(finding)
    return list(groups.values())
