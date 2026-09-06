# District operations console

Status: approved specification, published with implementation issues.
Date: 2026-09-05. The operator authorized publication and first-wave release; later waves, production deployment, and human visual signoff remain gated.
 
### Publication and first-wave release

The immutable design and prototype source revision is [`164663b1a3e5e78b4fde3b127cf013d87b7e3c28`](https://github.com/mikeroySoft/district/commit/164663b1a3e5e78b4fde3b127cf013d87b7e3c28). Issue bodies pin that specification. This release record supersedes the original draft-only publication wording below; design requirements and operator checkpoints are unchanged.

| Draft ID | Published issue | Initial release |
| --- | --- | --- |
| F01 | [Factory #26 — Execution lifecycle](https://github.com/mikeroySoft/factory/issues/26) | First wave: normal triage |
| F02 | [Factory #27 — Waits and resource ownership](https://github.com/mikeroySoft/factory/issues/27) | Held; F01 required |
| F03 | [Factory #28 — Runtime JSON](https://github.com/mikeroySoft/factory/issues/28) | Held; F02 required |
| D01 | [District #23 — Operational classification](https://github.com/mikeroySoft/district/issues/23) | First wave: normal triage |
| D07 | [District #24 — Read-only LAN policy](https://github.com/mikeroySoft/district/issues/24) | First wave: normal triage |
| D02 | [District #25 — Shared collector](https://github.com/mikeroySoft/district/issues/25) | Held; #23, #24 and verified installed F03 |
| D03 | [District #26 — Scoped shell](https://github.com/mikeroySoft/district/issues/26) | Held; #25 required |
| D04 | [District #27 — Graphical Overview](https://github.com/mikeroySoft/district/issues/27) | Held; #26 required |
| D05 | [District #28 — Flows and evidence](https://github.com/mikeroySoft/district/issues/28) | Held; #27 required |
| D06 | [District #29 — Brief](https://github.com/mikeroySoft/district/issues/29) | Held; #28 required |
| D08 | [District #30 — Management workflows](https://github.com/mikeroySoft/district/issues/30) | Held; #28 and #24 required |
| D09 | [District #31 — Cutover](https://github.com/mikeroySoft/district/issues/31) | Held; #27, #29, #30 and operator acceptance/archive checkpoints |

Only F01, D01 and D07 were released with `needs-triage`; no direct `ready-for-agent` bypass. The other nine issues carry `factory-held` and no intake label. `factory-held` is a visible operator marker, not a new dispatcher feature: exclusion is enforced by absence of `needs-triage`/`ready-for-agent`. Later releases require explicit dependency/checkpoint verification and removal of the hold before normal triage. Same-repository issue bodies contain concrete `Blocked by: #N` references; F03 installation and human approval remain explicit operator holds, not guessed cross-repository issue parsing.

The normal Factory and District timer services were started for first-wave intake. This records release, not implementation completion; current execution state belongs to GitHub and the Factory dashboards.

## 1. Purpose and precedence

One console answers: **Are the factories operating correctly, is work flowing as expected, and does their machinery need operator attention?**

Overview is where the operator watches; Flows is where the operator investigates; Brief is where the operator catches up. These are projections of one state model, not separate dashboards. Project-level decisions remain in Factory.

This specification supersedes the Atlas-as-primary-surface and mixed project/operational health requirements in PRD.md §§5.6–5.7 for this work. PRD.md and PLAN.md remain historical implementation records, not files to overwrite wholesale. Their host/repository ownership constraints still apply. Changes here do not authorize unrelated pipeline behavior changes.

### Intended operator

One operator, one Linux host, approximately three to ten managed factories. The default screen stays visible on a desktop while factories run; navigation down to evidence and back must be effortless. Network viewing is required. No macOS-style traffic-light window chrome.

### Reference prototypes

- `district/prototype-fleet.html`: comparative visibility, attention filter, dependency context.
- `district/prototype-flow.html`: graphical stages and upstream/downstream distinction.
- `district/prototype-brief.html`: readable verdict, evidence, recovery narrative.
- Launcher: `python district/prototype_dashboard.py`; port 8761, sample data only.

These are design references, not production implementations or telemetry evidence. Their single-position factory lanes and simulated stage timings must not be copied as reality. Archive the references before removing them at cutover; do not remove them before human visual review.

## 2. Overview: graphical desktop watch surface

Keep stable, equally weighted positions for factories; do not size by LOC or reorder on every update. Each factory is a compact flow diagram showing name, operating state, dispatcher state, stage occupancy, active/configured capacity, latest observed transition, next dispatch when known, freshness, and operational findings.

A factory can have several concurrent executions in different stages. Stage occupancy is an aggregate, never a claim that the entire factory occupies one stage. Scheduling/admission is distinct from execution and merge eligibility. Queued tickets are demand, not proof that a dispatcher is running or stalled.

Supporting surfaces:

- A compact strip counting normal operation, operational attention, and unknown observation. Unknowns are not silently counted healthy.
- Shared dependencies/resources, only with supported scope and evidence. A lock held does not prove its owner; one factory's failed probe does not prove a fleet-wide outage.
- A bounded recent-activity rail: observed transitions, completions, runtime failures, recoveries. Declare its actual history window.
- Watch mode hides secondary controls without hiding names, state, freshness, or alerts. It is not another route or product.

### Motion contract

| Motion | Meaning |
| --- | --- |
| Gentle active-stage pulse | Fresh observation confirms execution is active there |
| Brief transition movement | A distinct stage transition was observed |
| Completion highlight | A newly observed completion, not a redraw of an old snapshot |
| Quiet countdown | Confirmed next scheduled dispatch |
| Persistent labeled alert | An operational finding, not an endlessly flashing alarm |
| Stationary muted diagram | Stale/unavailable observation; show last known state and age |

No perpetual decorative circulation. Polls are not events. Initial load, reconnect, duplicate records and tab revisits must not replay old work as new. If intermediate transitions are missing, do not invent them. Prefer gentle pulse/highlight to literal blinking; no rapid flashes. Reduced-motion and pause-motion controls preserve all facts; pausing animation does not falsely imply pausing factories. Stop unnecessary animation in hidden tabs. Maintain focus and scroll while data updates.

## 3. Flows and reusable evidence

Overview factory selection opens Flows scoped to that factory; selecting a stage also selects the stage filter. All-factories scope supports comparison. Expand compact Overview diagrams into concurrent execution lanes, dispatcher context, known stage entry times, wait reasons, retries/review loops, merge eligibility, dependencies, and a bounded operational timeline.

Show execution phases only when reported. Distinguish scheduling, triage, worker, gate, review and merge rather than forcing every execution through a fictional straight line. Existing PRs may be revisited in another dispatcher pass; identifiers must distinguish executions/retries. Show terminal outcomes and interrupted observations. Elapsed time is not a progress percentage or ETA.

A code test failure is not itself a broken gate runner. Normal review revision, a project escalation, waiting for CI, capacity, a scheduled pass, or a legitimately held resource are not automatically District incidents. An upstream problem can prevent downstream work without making every downstream stage independently failed. Duration alone is insufficient to diagnose a stall.

One evidence panel serves every view:

1. Observed condition and affected scope.
2. Impact on execution, separate from inferred cause.
3. Source timestamps and observation quality.
4. Bounded source excerpt/reference.
5. Evidence-supported next step.
6. Link to the relevant Factory surface.

Reuse Factory operations (`/#ops`), ticket (`/#t=N`) and supported stage (`/#s=stage`) links. Do not guess report-specific routes. Keep District open when linking out. If the Factory web dashboard is unavailable, keep District evidence readable and explain the unavailable link. Use a reachable browser hostname plus registered port; do not send LAN browsers to their own localhost. Escape remote text and validate constructed link destinations.

## 4. Brief

A deterministic reading view of the same operational state:

- Current verdict, including unknown/partial observations.
- Operational attention grouped by supported cause and affected factories.
- Recent operational changes and recoveries within an explicit evidence window.
- Maintenance: configuration/engine drift, applicable doctor findings, host policy.

Every statement links to its scope/evidence. Do not calculate independent health or maintain a second incident store. Coincident failures are not proof of common cause. Intentional version pins are not drift. Do not claim history since yesterday or since last visit unless that history is actually available. No LLM narrative or project decision inbox is required.

## 5. Shared navigation and visual system

Primary navigation: **Overview / Flows / Brief**. Shared scope: all factories or one factory. Example URLs:

- `/?view=overview`
- `/?view=flows&factory=mikeroySoft/gpuflo`
- `/?view=flows&factory=mikeroySoft/gpuflo&stage=gate`
- `/?view=brief&factory=mikeroySoft/gpuflo`

Changing views preserves visible factory scope. Changing scope clears invalid execution/stage selection rather than displaying mismatched evidence. Browser Back restores prior scope, filter, panel and scroll; refresh/deep links preserve valid selection. Gracefully handle removed factories and expired execution evidence. Keep ordinary navigation out of permanent modal dialogs.

One typography, spacing, status-color and focus system across all three views. Use A's operational density and graphical evolution, B's flow explanation, C's reading hierarchy; do not ship three unrelated themes. Do not reproduce decorative city architecture. Status must be legible without color and accessible by keyboard. At narrow widths use readable stacked factories/lanes and a full-width evidence view; preserve all information rather than shrinking text.

Management is secondary to viewing. Repository analytics (LOC, language, stars, traffic) and architecture citations leave the operating path. Do not build replacement analytics screens as part of this scope. Retain a separate legacy Atlas/reference route if needed to preserve existing access; it must not remain a competing operations home.

## 6. Operational state and findings

Use one classification shared by CLI status and all views; remove duplicated/mixed classification callsites.

| Dimension | Values/meaning |
| --- | --- |
| Operating state | running, scheduled waiting, deliberately paused (only when intention is recorded), capped, unexpectedly stopped, unknown |
| Execution state | stage-active, known wait, blocked, completed/failed/interrupted, unknown |
| Findings | runtime, scheduling, dependency, host configuration/policy problems with supported impact |
| Observation | fresh, stale, partial, unavailable; source times retained |
| Project context | demand/occupancy and Factory links; not the District intervention queue |

Every finding carries stable identity, factory/shared scope, condition code, severity, observed time, source evidence and impact. Retain first-observed/last-observed only over a declared available window; do not fabricate onset from collection time. Grouping preserves per-factory observations.

`ready-for-human`, review bounce percentage and parked project work must not independently mark the machinery unhealthy. A parked upstream workflow is project context unless evidence establishes an operational failure. Classification of gate/review outcomes must distinguish ordinary product feedback from inability to run the configured mechanism; unknown causes remain unknown, not guessed.

Snapshot fetch failure is an observation failure, not proof the dispatcher stopped. Missing GitHub data does not erase locally observed runtime facts. Freshness thresholds derive from documented collection cadences and source timestamps. Re-fetching old data does not renew its source freshness. Existing documented CLI JSON/exit semantics affected by this change must be migrated and documented deliberately; consumers are not silently left on old health logic.

### 6.1 D01 shared semantic and CLI contract

`district.health.classify(slug, sources, table, at=None)` is the sole classifier.
`at` is a timezone-aware evaluation time, defaulting to current UTC. It performs
no I/O and retains no state. D02 supplies evidence, not a second classification.
The JSON-compatible normalized input is **District's semantic seam, not F03's
wire schema**. The complete field contract and runnable-shaped examples live in
the module docstring in `district/health.py`.

Each source has `id`, `observed_at` (timezone-aware source timestamp or null),
`cadence_seconds` (documented positive collection interval or null), `data`
(object or null), and `error` (string or null). Data can independently contain:

- `dispatcher`: authoritative `service_active`/`timer_active` booleans or null,
  optional `expected_enabled`, `next_dispatch_at`, recorded
  `pause: {recorded_at, reason}`, and `capped`/`cap_reason`/`cap_recorded_at`.
  Inactive service **and** timer plus enabled intent establish unexpected stop.
  An inactive timer alone establishes neither a deliberate pause nor a stop.
- `executions`: records with stable `id`, reported `state`, optional reported
  `stage`, `observed_at`, `entered_at`, `reason`, `outcome_kind`, and `reference`.
  `outcome_kind: product` is a legitimate verdict; `mechanism` means inability to
  execute the configured mechanism. Unspecified significance stays unknown.
  Missing `executions` means unsupported observation; `[]` means observed empty.
- `resources`: opaque `id`, `held`, optional source time and explicitly confirmed
  `owner: {factory, execution_id}`. Output ownership is `known`, `unknown`, or
  `none`; a held lock does not establish its owner or create an incident.
- `checks`: `condition`, opaque `resource`, `status: passed|failed|unknown`,
  evidence `detail`, supported `impact`, optional `cause`, `reference`, source
  time and `scope: {kind: factory|shared, id}`. Default scope is this factory.
  Shared scope must be established by the producer, not inferred from a URL.
  Within one source/scope/condition/resource the newest verdict replaces older
  checks, including recovery. Independent source evidence remains separate.
- Optional `history: {start, end, complete, truncated}`. Check
  `first_observed_at`/`last_observed_at` are retained only when ordered within
  that declared window. They do not claim incident onset or persistent history.

Supported condition codes: `runtime.dispatcher_failed`,
`runtime.mechanism_unavailable`, `scheduling.unexpected_stop`,
`scheduling.capped`, `dependency.unavailable`, `host.configuration_drift`,
`host.configuration_invalid`, and `host.policy_violation`. Cap and configuration
drift have warning severity; the other supported conditions have error severity.
Intentional pins are not configuration drift. Unsupported conditions or failed
checks without evidence/impact remain unknown, not fabricated findings.

The classifier returns `schema_version: 1`, `operating_state`, `execution_state`,
`observation`, `assessment`, `findings`, `sources`, `executions`, `resources`,
and `unknowns`. All concurrent executions survive the aggregate execution-state
projection. Aggregate precedence: stage-active, blocked, known wait, unknown,
failed, interrupted, completed. Observed empty executions imply known wait only
with scheduled, deliberately paused, or capped dispatch; absent telemetry does
not. Operating precedence is capped, recorded pause, running, scheduled waiting,
unexpectedly stopped, unknown: admission may be stopped while existing work runs.

Each finding includes `id`, `factory`, `scope`, `condition_code`, `resource`,
`severity`, `observed_at`, `impact`, `cause` (null unless supported), and
`evidence` records containing `source_id`, `factory`, `observed_at`, `observation`,
`reference`, and `detail`. Stable identity is the canonical JSON tuple
`[scope.kind, scope.id, condition_code, resource]`; changing evidence does not
change identity. `group_findings(entries)` groups only equal identities and
retains complete per-factory findings under `observations`.

Source output retains `id`, `observed_at`, `cadence_seconds`, `age_seconds`,
`observation`, and `error`, without recopying source data. Freshness uses source
time: up to two collection intervals is fresh (one missed interval tolerated);
older is stale. Missing/invalid/future timestamps or unknown cadence are partial.
Missing/null data is unavailable; an empty object is partial. Mixed qualities
and malformed observations are partial; all usable sources stale yields stale.
Missing execution telemetry prevents fresh overall observation. Unresolved
operational meaning does not change observation freshness; it changes assessment.
Source age is recomputed on reread without changing source time.
Reported last-known states remain visible with their observation quality.

`assessment` is derived here, never in a view: any supported finding means
`attention`; otherwise non-fresh observation, unknown operating/execution state,
or unresolved operational meaning means `unknown`; otherwise `normal`.
Unknown lock ownership remains explicit but is not independently an incident.

Current transport remains `factory dashboard --json`, adapted by
`health.snapshot_sources(snap, error)`. Its `generated_at` is retained as source
time. It has no documented periodic District collection cadence, so cadence is
null, and it lacks confirmed execution telemetry: normally partial/unknown,
even when running or scheduled. Positive service/timer observations are usable;
legacy false booleans also encode failed probes and cannot prove a stop.
GitHub failure preserves local runtime evidence. Failed dispatcher-unit runs and
configured triage probes are scoped findings with unknown cause; ordinary gate
verdicts, review revisions, escalations, bounce and parked project work are not.
The installed Factory producer's `dashboard.parse_journal` derives run results
only from systemd `Starting`/`Finished`/`Failed` journal brackets; application
output remains run lines. `consecutive_failures` counts those unit results,
not ticket gate verdicts.
Existing registry `disabled_at`/`disabled_reason` records a cap, never a pause.
No F03 support, local log parsing, runtime collector or pause-recording mechanism
is introduced by D01.

`status.entry` returns the classification alongside existing `table`, `snap`,
`error`, and cached project `metrics`. Its optional `result["sources"]`
accepts normalized D02 observations instead of applying the legacy adapter.
`district status --json` remains an object keyed by repository slug; `health`
and `reasons` are removed. Empty fleets serialize as `{}`. Text status includes
operating, execution, observation and assessment columns and retains project
metrics as context. Atlas and `/api/fleet` consume these same entries verbatim.

Exact **status** exit codes, for both text and JSON:

- **0**: every factory is normal, or no repositories are registered.
- **1**: at least one factory has operational attention, regardless of other
  unknown observations. Unknowns remain visible in its JSON entries.
- **2**: no attention, but at least one factory has unknown state/observation.
  Standard command-usage errors also use exit 2.

Migrated consumers: `status.entry/row/main`, CLI help, Atlas factory blocks,
inspector and fleet tally, dashboard `/api/fleet` through `status.fleet`, tests,
website status/JSON documentation and the repository District skill. `apply`
does not call the former health classifier: its reconciliation failures,
failure-cap writes and reset behavior retain their separate existing contract.

## 7. Factory/District contract

### Current evidence and gaps

Source inspection: Factory `factory/dashboard.py` reports dispatcher/timer state, active locks/count, configuration/capacity, triage probe, gate-lock occupancy, ticket state and events. `phase_of()` derives a phase from newest artifact time: this is not confirmed execution telemetry. Gate-lock held/free is not ownership. District `status.snapshot()` currently invokes full `factory dashboard --json`; full Factory snapshots perform GitHub work. District `health.level()` currently conflates project escalations and operation health.

A read-only `factory dashboard --json` invocation during planning succeeded on Factory 0.2.0 and exposed dispatcher fields, ticket stages, active count and gate-lock held state. No running stage was observed in that invocation; this is not validation of new telemetry.

### Agreed implementation seam

Factory owns execution truth, events and resource observations. District consumes public CLI JSON, never imports Factory internals or reconstructs execution by parsing worker logs. Reuse existing event recording and locks; do not introduce a generic tracing framework or a second pipeline controller.

Factory tickets implement these shared contracts in order:

- **F01:** execution lifecycle records using existing event storage. Each has schema version, stable unique event identity/order within its execution, dispatcher-run identity, execution identity, ticket identity when applicable, attempt/review round when applicable, stage, event kind, UTC source timestamp, outcome/reason when known. Include dispatcher/triage activity even when no ticket is assigned. Stages emit entry/exit and terminal outcomes, with interrupted-process handling based on authoritative liveness/locks rather than artifact age.
- **F02:** extend the same lifecycle with known wait/resource observations. Distinguish requested/acquired/released, opaque resource identity and confirmed holder where instrumentable. Ownership unknown is explicit. No stale owner after termination and no new locking/scheduling behavior.
- **F03:** expose `factory dashboard --runtime-json`, a read-only, versioned lightweight projection with no GitHub or network probes. Top-level contract: `schema_version: 1`, `generated_at`, `repo`, `dispatcher`, `executions`, `resources`, `events`, `history`, `errors`. Executions/resources/events use F01/F02 identities and source times. `history` declares retained interval/completeness and truncation; a bounded event window is sufficient. Structured partial errors preserve usable facts. Support empty histories, malformed/truncated event tails and unsupported/missing data without fictitious activity. Keep full snapshot functionality for slower project/configuration observations; runtime reporting does not change dispatch behavior.

Precise serialized keys within these objects and lifecycle invariants must be documented and tested in F01–F03 before District D02 consumes them. A schema version denotes actual support; installed older engines return unsupported/unknown in District, never a fabricated fallback. Existing ticket/dashboard consumers are migrated if their contracts change. No private Python coupling across repositories.

## 8. Collection and refresh

D02 owns a single bounded collector/cache for all dashboard clients, not one full collection per HTTP request. Publish fresh factory results independently; a slow/hung factory has a timeout and cannot freeze the fleet. Prevent overlapping collections for one factory. Preserve source freshness and bounded last-known data through partial failures. Shut down collector work cleanly with the server.

- Runtime target: approximately five seconds, measured on the target host after F03 exists.
- Full GitHub/config observations: slower, separately timestamped/cached; never executed merely because a browser requested a refresh.
- Existing hourly repository metrics remain outside the runtime hot path.
- Browser polling reads the shared cache; no WebSocket, database, or event broker requirement.
- Ten factories and multiple tabs should not multiply underlying work by tab count.

Deduplicate lifecycle events by identity, not text or wall-clock proximity. First connection establishes a baseline; reconnect gaps/truncation are visible. New completions can highlight once per client observation without implying total durable event replay. Short-lived cache/history is sufficient; no speculative persistent incident system.

## 9. Management and network safety

Viewing over the LAN and permission to modify the host are distinct. Binding to 0.0.0.0 must not itself grant mutation authority. The current custom action header prevents some cross-origin requests but is not authentication. D07 establishes a trusted authorization policy before D08 exposes management in the new console: unauthenticated network clients are read-only, and mutating endpoints fail closed; local authorized operations retain CSRF/origin protections. Do not add a full account system for one operator. Remote management, if later required, needs explicit authentication design rather than an insecure opt-in.

D08 presents existing District-owned add/adopt, reconcile, upgrade, reset and remove operations with exact target/effects, deliberate confirmation, streaming result and refreshed postcondition. Do not wrap arbitrary CLI argument arrays as a public management interface. Conflicting operations must not race. Failure/disconnect must not display success. Reset is recommended after cause repair, not as a universal fix. Factory ticket/PR/label/gate/review decisions stay in Factory. Destructive removal does not delete repository content. Sanitize bounded error output; do not expose credentials via snippets or config rendering.

## 10. Delivery and ticket dependency plan

Local drafts (not GitHub issues):

- `OPERATIONS-CONSOLE-FACTORY-TICKETS.md`: F01 lifecycle, F02 waits/resources, F03 runtime projection.
- `OPERATIONS-CONSOLE-DISTRICT-TICKETS.md`: D01 semantics, D02 collection, D03 shell, D04 graphical Overview, D05 Flows/evidence, D06 Brief, D07 authorization, D08 management, D09 cutover.

Dependency graph:

```
F01 → F02 → F03
D01 + F03 installed + D07 integrated → D02 → D03 → D04 → D05 → D06
D05 + D07 → D08
D04 + D06 + D08 + human visual/final acceptance + verified archive → D09
```

Serialize UI ownership because these tickets share the console shell/state/evidence. D07 can run independently before UI work; reconcile any shared dashboard-server mutation before D02 starts. Once F03 merges, the operator must deploy/verify the compatible Factory runtime before releasing D02; a closed upstream issue alone is not proof the installed engine supports it.

### Publishing/release procedure

These files are drafts; no fake GitHub issue numbers or automated dependency syntax using draft IDs. Commit/publish the approved spec and drafts before issuing work so clean worktrees can read them. Factory-repo ticket bodies must include the relevant contract and a pinned accessible District spec URL; do not assume a sibling checkout exists on a worker.

Current Factory `open_blockers()` supports local `Blocked by: #N` body references. Native GitHub dependency API results are also checked, but body references are resolved against the current repository. Do not encode cross-repository dependencies as bare numbers or assume full URLs are parsed correctly.

When authorized to publish:

1. Assign real issue numbers to same-repository draft dependencies; use the template's four sections and `needs-triage` (not `ready-for-agent` bypass).
2. Keep cross-repository-blocked work non-dispatchable until its prerequisites are merged, the installed engine is verified, and same-repository blockers are ready. Use a repository-local release/checkpoint issue if necessary; do not depend on unverified cross-repo body parsing.
3. Release eligible waves through triage → worker → deterministic gate → review → merge. Closure for any reason is not completion: verify prerequisite acceptance/merged implementation before release.
4. Human visual review follows D04/D05 with real telemetry. D09 remains held for that review and final desktop/LAN acceptance. Factory handles implementation, not autonomous visual signoff or permission to deploy itself.
5. Archive prototype sources on an authorized reference branch with a pinned pointer before cutover removes production-accessible prototype surfaces. No branch/commit/push is performed by this drafting task.

## 11. Acceptance and final proof

The first visible milestone is D04: live graphical Overview distinguishing running, legitimately waiting, broken and observation-unknown factories. It is not done with canned sample data alone. New behavior must have plausible regression checks; UI acceptance additionally requires actual browser evidence, not source-text assertions.

End-to-end scenarios:

- Two concurrent executions in one factory occupy different stages without overwriting each other.
- Worker → gate → review and a revision loop produce distinct, deduplicated transitions.
- Normal gate failure/project escalation does not create a machinery incident; inability to execute the mechanism has supported operational evidence.
- Scheduled idle, capacity/resource wait, capped dispatch, unexpected stop and stale observation remain distinct.
- Interrupted processes stop appearing active; orphan event tails do not animate forever.
- Partial GitHub failure retains trustworthy runtime facts; a slow factory does not freeze others.
- Reconnect, duplicate records and history truncation never replay old motion as current.
- Overview → Flows → evidence → Factory → back preserves context and correct scope; stale/removed targets are explained.
- Brief counts/findings agree with Overview and Flows at the same snapshot revision.
- Several browser clients share collection work; measure ten-factory cadence and cost without modifying production factory schedules.
- Keyboard, narrow viewport, reduced motion and animation pause retain information. Watch mode stays readable in a prolonged desktop session.
- LAN viewing works while unauthorized mutations are refused server-side; confirmations and post-action evidence cover authorized management.

Repository gates grounded in current configuration:

- Factory: `python -m unittest discover -s tests` from Factory root.
- District: `uv run python -m unittest discover -s tests` from District root.

Workers add targeted behavioral checks only where plausible regressions warrant them. Run existing gates once per completed ticket, not in concurrent shared-worktree edits. UI workers report browser screenshots and interaction evidence. Human approves the full visual design and final operational acceptance; passing unit tests cannot substitute.

## 12. Explicit non-goals

No project inbox, log/report viewer clone, automatic project remediation, multi-host/team management, speculative dashboards for analytics, architectural city animation, generic observability platform, LLM briefing, arbitrary stall heuristic, new scheduling policy, or production rollout during specification drafting. Preserve existing source ownership and unrelated user work.
