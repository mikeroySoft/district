# Operations console — Factory ticket drafts

Status: published ticket source for [Factory #26](https://github.com/mikeroySoft/factory/issues/26) (F01), [#27](https://github.com/mikeroySoft/factory/issues/27) (F02), and [#28](https://github.com/mikeroySoft/factory/issues/28) (F03). F01 was released through normal triage on 2026-09-05; F02/F03 remain held. The original draft instructions below are retained as provenance; the publication record in `OPERATIONS-CONSOLE-SPEC.md` and live issue bodies govern release. Publication does not assert telemetry is implemented or authorize deployment/visual signoff.

## Dependency and release table

| Draft | Same-repository prerequisite | Release condition |
| --- | --- | --- |
| F01 | None | Approved specification published and pinned contract reference included in the issue body. |
| F02 | F01 | F01 implementation merged and lifecycle acceptance satisfied; consume its documented identities/invariants. |
| F03 | F02 (transitively F01) | F02 implementation merged and resource/wait acceptance satisfied; finalize and demonstrate schema 1. |

Publication instructions, for a separately authorized publishing step:

- Publish the approved District specification and drafts first. Insert an accessible, commit-pinned URL to District's `OPERATIONS-CONSOLE-SPEC.md` in **each issue's Scope** before publication; verify a clean Factory worktree can obtain it. The contracts reproduced below travel with the issue. A worker must not need a sibling District checkout, an unpinned branch tip, or this local file.
- Replace same-repository dependency draft IDs with actual Factory issue numbers and add the supported `Blocked by: #N` body references. The notation here is explanatory, not a real blocker number. Use `needs-triage`, not `ready-for-agent`; release through triage → worker → deterministic gate → review → merge. Check merged implementation and acceptance, not closure alone.
- **Cross-repository hold:** F03 merge does not release District D02 by itself. The operator must install/deploy the compatible Factory version and verify `factory dashboard --runtime-json` schema 1 on the managed host. D02 also requires District D01 and reconciliation of D07's shared dashboard-server changes. Keep D02 non-dispatchable until these conditions hold; use a District-local checkpoint issue if needed. Bare issue numbers resolve within the current repository; do not rely on cross-repository URL parsing for a blocker.
- **Visual/cutover hold:** Factory telemetry completion is not human visual approval. District D04/D05 need human review with real telemetry, and D09 remains held for D04, D06, D08, that human approval, and final desktop/LAN acceptance. These Factory tickets neither approve the UI nor grant workers permission to deploy themselves.

## F01 — Record authoritative execution lifecycle transitions

Proposed label: `needs-triage`  
Local dependency: none.

**Scope**

Make Factory's existing event trail report distinct execution lifecycles, so a consumer can identify what actually entered/exited a stage and distinguish completion from interruption without inspecting worker logs or artifact modification times.

Contract source: the publisher must insert the accessible commit-pinned District `OPERATIONS-CONSOLE-SPEC.md` URL here before publication. Read §§3, 6–7, 10–11 at that revision. Required contract, reproduced for a standalone Factory worker:

- Reuse `.factory/events.jsonl` and existing event recording. Define and document precise serialized keys, types, nullability, versioning, and lifecycle invariants. Every lifecycle record has a schema version, stable unique event identity, ordering within its execution, dispatcher-run identity, execution identity, ticket identity when applicable, attempt/review round when applicable, stage, event kind, UTC source timestamp, and outcome/reason when known. IDs/order survive repeated reads; wall-clock time is not an event identity or a total-order substitute.
- Instrument real boundaries of dispatcher/scheduling, triage, worker, gate, review, and merge activity where they actually execute. Include dispatcher and triage runs that never receive a ticket. Define the run association for independently invoked triage rather than inventing a ticket or a dispatcher run that did not occur. Existing-PR revisits in later dispatcher passes are distinguishable from the earlier worker execution; retries/review rounds remain attributable without overwriting earlier stage entries.
- Emit stage entry/exit and terminal outcomes with causal identities. Simultaneous executions in different stages remain independently observable. A skipped or never-entered stage does not receive fictitious active time, and approval is not a fabricated merge completion. Keep scheduling/admission separate from execution and merge eligibility.
- Preserve the distinction between ordinary gate check failure/review revision/project escalation and inability to run the configured mechanism. Record supported outcome/reason evidence; where the cause is not known, expose uncertainty instead of classifying it as runtime failure.
- Define interruption reconciliation using authoritative process liveness and existing locks, with process identity safe against reuse. Cover failure before the ordinary cleanup block, normal exceptions, and abrupt process death that cannot append a final event. A remaining child process or a held lock must not be dismissed solely because its parent ended; an old artifact or a stale PID alone cannot prove activity. If authoritative evidence is unavailable, report unknown rather than active or interrupted with invented certainty. Any reconciled terminal observation has its actual observation time, not an invented exact crash time, and a stable identity so repeated reconciliation does not manufacture repeated transitions.
- Keep lifecycle recording safe for overlapping producers in the existing event store. Document the treatment of legacy/unversioned events and interrupted writes. Existing `stats`, `learn`, dashboard, and ticket event consumers retain correct outcomes/counts; migrate consumers if their contracts change. Update existing public documentation for the event contract and affected CLI semantics.

**Touches**

Start with `factory/dispatch.py`: `record`, `main`, `process_ticket`, `worker_round`, `run_worker`, `run_gate`, `review`, `land_pass`, `merge_pass_locked`, and existing terminal paths. Inspect `factory/triage.py` entry/execution paths and `factory/gate.py` where a subprocess boundary needs lifecycle context. Integrate authoritative lifecycle interpretation in the existing Factory observation path rather than treating `factory/dashboard.py:phase_of`'s newest-artifact heuristic as telemetry. Audit readers in `factory/dashboard.py`, `factory/stats.py`, `factory/learn.py`, related tests under `tests/`, and existing contract documentation. File pointers are guidance, not permission for an unrelated refactor.

**Exit gate**

- Exercise real local entry points against a disposable repository with controlled external-command stand-ins, not production tickets. Show a worker → gate → review → revision → worker → gate → review sequence with distinct ordered events, correct attempt/round association, and no repeated event identity across distinct transitions. Show a later dispatcher pass revisiting its PR with a distinct execution identity and an attributable terminal outcome.
- Show two overlapping executions in different stages remain separate, and a no-ticket dispatcher pass plus a no-ticket triage invocation still produce attributable lifecycle evidence. Concurrent appends retain complete readable records.
- Show ordinary failing code checks and `REVISE` remain product feedback while a missing configured executable is distinguishable as mechanism failure; unsupported failure causes remain unknown. Show skipped stages do not appear entered.
- Terminate an instrumented disposable process abruptly. Re-observation no longer leaves that execution permanently active, repeated reconciliation does not emit new copies of the same interruption, and the recorded time does not pretend to be a known crash time. Cover missing liveness evidence and a still-live child/held lock without false completion. Legacy or truncated rows do not become new lifecycle activity.
- Preserve dispatch ordering, claim/lock behavior, concurrency limits, retry/review counts, gate/merge prerequisites, subprocess exit behavior, and side-effect-free dry-run behavior. Add focused behavioral regression checks for identity/interruption boundaries and update existing contract-dependent checks; avoid tests that merely match source text or echo field assignments.
- From the Factory repository root, run the existing gate once after the ticket is complete: `python -m unittest discover -s tests`. Report the lifecycle smoke observations and the gate result. Do not change production scheduling to obtain evidence.

**Out of scope**

Wait/resource telemetry belongs to F02; the lightweight CLI projection belongs to F03. No new scheduling policy, lock ownership mechanism that changes exclusion, worker/reviewer behavior, generic tracing framework, second controller, District UI, network probes, project remediation, or production rollout. Do not infer stages from logs/artifact age or diagnose stalls from duration alone.

## F02 — Report known waits and evidenced resource ownership

Proposed label: `needs-triage`  
Local dependency: F01; publisher must translate to its real same-repository blocker.

**Scope**

Extend F01's lifecycle with actual wait and resource observations so consumers can distinguish legitimate waiting from failed execution and a held resource from a confirmed holder.

Contract source: the publisher must insert the accessible commit-pinned District `OPERATIONS-CONSOLE-SPEC.md` URL here before publication. Read §§3, 6–7, 10–11 and the merged F01 contract in the Factory repository. Required contract:

- Use F01 event/run/execution identities, event ordering, source timestamps, and interruption rules; extend that contract rather than add an independent telemetry stream. Document all added serialized keys, reason values, ownership certainty, and state transitions.
- Report known waits where Factory already observes them: admission/capacity limits, a confirmed scheduled next pass, ticket/merge lock contention, exclusive gate resources, and observed merge eligibility waits such as pending CI. Emit observations at existing decision/acquisition points; recording a known CI wait must not add a CI query or a new polling loop. If a reason or schedule is unavailable, preserve unknown. Demand alone does not establish a running dispatcher, and a held resource or elapsed duration alone does not establish an incident.
- For instrumentable resources, distinguish requested, acquired, and released, with an opaque stable resource identity and the confirmed holder's execution/run identity. State the supported scope of each resource identity so consumers do not group unrelated resources or claim fleet-wide impact from one repository's observation. Resources held outside instrumented Factory execution have explicit unknown ownership; lock held/free is not itself an ownership claim.
- Respect real lock lifetimes. The exclusive gate lock is acquired by the gate subprocess, not also by its dispatcher parent. A nonblocking merge-lock miss remains a skipped pass/retry-next-pass observation, not a newly blocking wait. A pending request is not an acquisition, and releasing one holder cannot erase a newer holder's ownership.
- Reconcile interrupted holders using F01 authoritative liveness/lock evidence. After termination, stale metadata must not name a dead execution as the confirmed current owner; preserve an unknown holder when the resource is still held but attribution cannot be proved. Observe release/ownership change with truthful times; do not invent an exact release time for unobserved process death.
- Keep waits, execution stage occupancy, and merge eligibility distinct. Project escalation or ordinary review revision is project context, not automatically a machinery incident. Update affected public contract documentation and event consumers.

**Touches**

Start with `factory/dispatch.py` admission paths in `main`, `process_ticket`, `land_pass`, and merge eligibility branches in `merge_pass_locked`; reuse `ticket_lock`, `lock_held`, and F01 recording/liveness paths. Instrument the actual exclusive acquisition/release in `factory/gate.py:main`. Use the existing local dispatcher/timer observation seam in `factory/dashboard.py` for known schedule evidence; do not add a network probe. Extend relevant behavioral checks under `tests/` and the F01 event contract documentation.

**Exit gate**

- In a disposable process scenario, one gate holds the existing exclusive resource while another requests it. Observe requester/confirmed-holder distinction, then acquisition only after release; the configured exclusive checks remain serialized with unchanged lock lifetime. The ordinary check execution/result remains unchanged.
- Hold the same resource from an uninstrumented process. The observation reports held with unknown owner, not an inferred ticket, factory, or failed dependency. Observe a terminated holder followed by another holder without carrying the old identity forward or clearing the new one when older release evidence is read.
- Exercise unchanged admission/merge decision paths: capacity reached, nonblocking merge-lock miss, pending CI, and a confirmed scheduled idle pass produce distinct known reasons, not failed stages or additional polling/dispatch. Missing timer/liveness evidence yields unknown; an ordinary empty frontier does not manufacture a scheduled or paused intention.
- Kill a waiting requester and a holding process in isolated scenarios. Neither remains indefinitely stage-active or a confirmed live holder after authoritative reconciliation; a resource still held with unavailable attribution retains unknown ownership. Repeated observations preserve stable identities and do not invent repeated wait/release transitions.
- Show lock acquisition order, blocking/nonblocking modes, capacity accounting, next-pass behavior, gate outcomes, and merge prerequisites are unchanged. Add targeted regression checks for contention and stale-ownership boundaries; no duration-based incident heuristic.
- From the Factory repository root, run the existing gate once after the ticket is complete: `python -m unittest discover -s tests`. Report the isolated contention/interruption observations and the gate result. No production scheduling changes or production resource contention experiments.

**Out of scope**

F03 owns the runtime CLI. No new locks or scheduling policy, centralized resource broker, fairness/retry changes, inferred resource owners, dependency outage diagnosis, wait ETAs/progress percentages, District classification/UI, telemetry network calls, or production deployment.

## F03 — Expose a bounded network-free runtime JSON projection

Proposed label: `needs-triage`  
Local dependency: F02, transitively F01; publisher must translate F02 to its real same-repository blocker.

**Scope**

Add the public read-only command `factory dashboard --runtime-json` that emits one cheap, versioned runtime observation and exits. District can consume it through CLI JSON without importing Factory internals, parsing worker logs, or triggering the full GitHub-backed snapshot.

Contract source: the publisher must insert the accessible commit-pinned District `OPERATIONS-CONSOLE-SPEC.md` URL here before publication. Read §§6–8, 10–11 and the merged F01/F02 serialization contracts in Factory. Required schema 1 top-level fields are:

```json
{
  "schema_version": 1,
  "generated_at": "UTC projection-generation timestamp",
  "repo": "owner/repository",
  "dispatcher": {},
  "executions": [],
  "resources": [],
  "events": [],
  "history": {},
  "errors": []
}
```

This is a proposed shape, not an observed sample payload. The empty objects above do not excuse an empty implementation. Finalize and document exact nested keys/types, required versus nullable fields, supported values, source timestamps, identity/order semantics, bounds, partial-error behavior, CLI argument compatibility, and exit semantics before the ticket is complete:

- `dispatcher` carries supported local dispatcher/run/timer evidence, active/configured capacity, and next dispatch only when known. It must retain observation quality and time. Intentionally paused is reported only when intention is recorded; an unavailable service query does not become a stopped dispatcher.
- `executions`, `resources`, and `events` project the actual F01/F02 identities and facts, preserving concurrent executions, stage-entry source times, attempts/review loops, terminal/interrupted outcomes, known waits, requested/acquired/released resources, and explicit unknown ownership. Latest transition means an observed event, not the latest artifact. Preserve active execution facts even when their entry event precedes the returned bounded event window, or explicitly report the affected partial state rather than invent an entry time.
- `history` declares the retained interval, completeness, and truncation/gaps. A bounded event window is sufficient. Define empty history and unknown interval semantics, deterministic ordering, and whether a bound clips the beginning/end. Do not imply an exhaustive lifetime history or fabricate missing intermediate transitions. Rereading unchanged storage returns unchanged event identities/source times even though `generated_at` changes.
- `errors` contains bounded structured partial errors with affected source/scope and machine-readable codes; usable facts survive independent source failures. Missing data, unsupported event versions, unreadable storage, and malformed/truncated JSONL tails are distinguishable from an empty valid history. Preserve valid records around recoverable corruption and explicitly mark the gap. Document fatal invocation/configuration behavior separately from a partial successful projection. Bound and sanitize diagnostics; do not dump credentials, raw configuration, or arbitrary logs.
- Schema version 1 denotes implemented support, not a speculative advertisement. Older/unversioned event records remain explicitly unsupported/unknown for lifecycle purposes; do not promote artifact-derived guesses into confirmed activity. Document how consumers recognize unsupported CLI/schema on an older installed engine; District owns that handling, not a false Factory fallback.
- Use only bounded local reads and bounded local process/system queries. Make the read path actually read-only, including first invocation with no state directory: no event appends, ownership rewrite, state/lock-file creation, dispatch, lock takeover, or repair. It may project F01/F02 interruption reconciliation without persisting new events. Avoid loading/scanning unbounded logs or the whole lifetime event history. Bound external local commands so missing/hung system services cannot hang the runtime endpoint indefinitely.
- The runtime path never calls GitHub, `gh`, the triage model probe, remote Git operations, or any other network endpoint, even as a fallback. Keep `factory dashboard --json` and normal dashboard functionality for slower project/configuration observations; do not implement runtime JSON by filtering a full snapshot after it has already done network work. Preserve full-snapshot consumers or migrate any deliberately changed public contract. Update existing CLI/reference documentation with a real schema 1 example and limitations.

**Touches**

Start with `factory/dashboard.py:main`, its argument handling, local dispatcher/timer observation paths, and F01/F02 lifecycle/resource readers. `snapshot` currently performs GitHub work and `triage_llm_online` performs a network probe; neither belongs in the runtime path. Inspect `configure` and lock helpers for incidental writes before reusing them. CLI dispatch is routed through `factory/cli.py`; keep the existing command structure. Extend relevant checks under `tests/` and existing public CLI/schema documentation. No District private-Python integration.

**Exit gate**

- Run the actual `factory dashboard --runtime-json` CLI in a disposable repository with instrumented F01/F02 local evidence. Parse schema 1 and observe two executions in distinct stages, a review round/retry, a known wait, confirmed and unknown resource ownership, and a terminal/interrupted execution with correct source identities/times. A second unchanged read advances generation time without creating transitions/completions or refreshing source timestamps.
- Exercise empty/missing history, a bounded window whose first event is mid-execution, duplicate event identities, a malformed row, an interrupted last-line write, unsupported event versions, and unavailable local service evidence. Each produces truthful interval/completeness/errors while preserving unaffected runtime facts; no fictional stage, owner, completion, or history interval appears. Define and demonstrate duplicate handling so a duplicated stored event cannot become two transitions.
- Demonstrate this is a **network-free endpoint**, not merely a successful offline request: run the real CLI with Python network access guarded to fail/report attempts and external command invocation observed/guarded against `gh`, remote Git, probes, and other network-capable calls. Verify zero network attempts; a caught network failure does not satisfy this requirement. Also exercise missing tools/services and unavailable GitHub credentials without discarding valid local facts. Full snapshot behavior remains separately available.
- Demonstrate read-only behavior by comparing disposable state before/after normal, absent-state, and corruption/partial runs, including event contents and lock/state directory creation. Confirm bounded output and bounded work with a large event history; report input size, retained/output size, elapsed time, and local command counts. A deliberately hung local query terminates under the documented bound with a partial observation. Report repeated invocation cost on the target host when available; District D02 owns the later approximately-five-second, ten-factory shared-collector measurement, not an invented benchmark result here.
- Add focused behavioral checks for no-network/read-only, identity/freshness, and corrupt/partial-history boundaries. From the Factory repository root, run the existing gate once after the ticket is complete: `python -m unittest discover -s tests`. Report real CLI smoke evidence and the gate result.
- Supply the finalized nested schema documentation and real example needed by a clean District D02 worker. **Release handoff:** F03 acceptance/merge alone does not assert the installed engine is compatible. Record the version/revision containing support; leave deployment and installed-host schema verification to the separately authorized operator release checkpoint. District D02 remains held until that verification and its District prerequisites pass.

**Out of scope**

No District collector/UI, HTTP streaming service, WebSocket, database, event broker, persistent incident store, generic observability layer, log/report viewer, GitHub polling, scheduling/execution behavior change, new lock policy, production rollout, or autonomous visual approval. Runtime reporting does not replace the slower full snapshot, fabricate fallback telemetry for old engines, or grant authority to mutate the repository or host.
