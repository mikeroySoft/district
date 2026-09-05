# District operations console — District ticket drafts

Status: local drafts only. These are not issued, committed, dispatched, or authorized for implementation or deployment by their existence. D01–D09 are draft IDs, not GitHub issue numbers.

Canonical contract: `OPERATIONS-CONSOLE-SPEC.md` in this repository. Every worker must read it completely before changing source. These bodies use the existing **Scope**, **Touches**, **Exit gate**, **Out of scope** template; file pointers are starting points, not requirements to introduce new modules.

## Publication and release instructions

- When the user authorizes publication, commit/publish the approved spec and draft files first so clean worktrees can read the contract. Publish each body with `needs-triage`; do not bypass triage with `ready-for-agent`.
- Replace same-repository draft dependencies with real `Blocked by: #N` references only after actual issues exist. These drafts deliberately contain no executable blocker references. Verify prerequisites met their acceptance and merged: issue closure alone is insufficient.
- Factory's body parser resolves blocker numbers in the current repository. Never encode F03 as a bare District issue number or rely on unverified cross-repository URL parsing. Keep D02 non-dispatchable until F01 → F02 → F03 are merged and an operator has deployed and verified the compatible Factory engine on the target host. A District-local release/checkpoint issue may enforce that hold when publishing; it is not an additional draft here.
- Release order: D01 → D02 → D03 → D04 → D05 → D06. D07 is independently runnable, but its shared `district/dashboard.py` changes must merge/be integrated before D02 starts. D08 depends on D05 and D07. D09 depends on D04, D06, D08, human visual approval after D04/D05 using real telemetry, and the archive/final-acceptance holds described in D09.
- Serialize the UI tickets: they own successive changes to one shell, one state model, and one evidence component. Do not split simultaneous ownership of those files. Factory produces lifecycle truth; District consumes public CLI JSON rather than imports or log reconstruction.
- Run `uv run python -m unittest discover -s tests` from District root once per completed implementation ticket, not during concurrent shared-worktree edits. Add only behavioral regression checks that defend plausible failures. UI acceptance also requires actual browser interaction and screenshots; a fixture or passing test is not visual approval. This drafting task runs no validation commands.
- Only a human grants visual signoff and deployment permission. Before source cutover, an authorized operator archives prototype sources on a reference branch and publishes a pinned pointer. Do not autonomously branch, commit, push, deploy Factory, or change production schedules to satisfy these holds.

## D01 — Give CLI and console one operational classification

**Scope**

Outcome: `district status` and dashboard consumers classify the same evidence identically, separating machinery problems from project decisions and unavailable observation. No prerequisite draft; read `OPERATIONS-CONSOLE-SPEC.md`, especially §§6–7 and 11. This ticket owns the semantic contract consumed by D02 and every view.

- Replace mixed health classification with one shared model: operating state `running`, `scheduled waiting`, `deliberately paused` only with recorded intent, `capped`, `unexpectedly stopped`, or `unknown`; execution state `stage-active`, `known wait`, `blocked`, terminal `completed`/`failed`/`interrupted`, or `unknown`; observation `fresh`, `stale`, `partial`, or `unavailable`. Keep project demand/context distinct from operational findings.
- Model supported runtime, scheduling, dependency, host configuration/policy findings. Each finding has stable identity, factory/shared scope, condition code, severity, observed time, source evidence, and impact. Retain first/last observation only within a declared available window; collection time does not invent onset. Preserve per-factory evidence when grouping.
- `ready-for-human`, review bounce, parked upstream project work, ordinary product test failure, review revision, CI/capacity/resource waits and scheduled idle do not independently imply machinery incidents. Inability to run a configured gate/reviewer is different from its legitimate negative verdict. Unsupported causes stay unknown. Duration alone does not establish a stall.
- Fetch failure means failed observation, not stopped dispatcher. Retain independently trustworthy local runtime facts if GitHub data fails. Do not claim unreported execution phases from artifacts. Freshness uses source time and documented collection cadence; define the shared input/output contract for D02, including unknown and partial sources without pretending F03 is already installed.
- Migrate every current classification callsite and affected CLI JSON/exit-code consumer deliberately. Document the resulting JSON fields and exact exit meanings, including how unknown observation is reported; remove obsolete mixed classification rather than maintaining a second health system. Keep existing meaningful project fields as context where needed, never as an intervention queue.

**Touches**

`district/health.py` (`level`, `LEVELS`, old bounce rule), `district/status.py` (`entry`, `row`, `main`), classification consumers in `district/atlas.py`, and any apply/CLI callers found by searching those symbols. Update relevant behavioral cases in `tests/test_district.py` and existing status/JSON documentation. Inspect the existing `health`/`reasons` contracts before migrating them; no unrelated ownership changes.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Exercise the actual `district status` and `district status --json` paths on controlled evidence covering scheduled idle, recorded pause, capped dispatch, authoritative unexpected stop, missing/stale observation, normal gate failure/project escalation, and evidenced mechanism failure. Capture output/exit status and compare the dashboard classification for the same evidence. Unknowns are explicit and are never silently healthy or stopped.
- Behavioral checks demonstrate that project feedback does not manufacture an incident; partial GitHub failure does not erase usable runtime facts; unknown resource ownership and unrecorded pause intent remain unknown. Changing evidence changes a finding without losing its stable identity/scope; source ages do not reset on reread.
- Report the final shared semantic/CLI contract and migrated consumers so D02 can use it without reclassifying in its collector. No browser redesign is required here; if existing rendered status changes, exercise that surface in a real browser and capture the changed labels.

**Out of scope**

Factory lifecycle instrumentation, the runtime collector, new UI layouts, scheduling/locking policy, automatic remediation, arbitrary stall thresholds, project inboxes, or persistent incident storage. D02 supplies the new transport; this ticket does not fabricate runtime telemetry to compensate for its absence.

## D02 — Serve independently refreshed, source-timestamped fleet observations

**Scope**

Outcome: all browser clients read one bounded collector/cache; a slow factory or partial source cannot freeze or falsify the fleet. Dependencies: D01 accepted/merged and D07's shared dashboard-server mutation integrated before starting. Cross-repository release hold: F01 → F02 → F03 accepted/merged **and the operator has deployed and verified the compatible installed engine**, not merely closed F03. Read `OPERATIONS-CONSOLE-SPEC.md` §§6–8 and 11 and F03's published, pinned serialized contract before implementing.

- Use public `factory dashboard --runtime-json` as the lightweight local runtime source. Required top-level schema-1 fields are `schema_version: 1`, `generated_at`, `repo`, `dispatcher`, `executions`, `resources`, `events`, `history`, `errors`. Consume documented F01/F02 identities and source times; `history` specifies retained interval, completeness and truncation. Nested field names/invariants come from the completed F03 contract, not guesses. The command performs no GitHub/network probes.
- Unsupported versions or installed older engines are explicitly unsupported/unknown. Handle empty history, malformed/truncated event tails and structured partial errors without fictitious activity. No private Factory imports, artifact-age phase inference or worker-log parsing.
- Start one bounded collector with the dashboard server and shut it down cleanly. Publish each factory result as it arrives with coherent snapshot revision metadata; apply per-factory timeouts, bound concurrency, and prevent overlapping work for a factory. Slow/hung sources do not hold publication of other factories.
- Target approximately five-second runtime collection; measure on the target host. Keep full `factory dashboard --json` GitHub/config observations on a documented slower, separately timestamped/cache schedule. Browser requests/refreshes only read cache; they never trigger full collection. Existing hourly repository metrics remain outside the hot path.
- Preserve bounded last-known data and original source times through errors. Expose observed/source/collection ages and quality so D01 classifies stale/partial/unavailable correctly; repeatedly fetching old data does not refresh it. Missing GitHub facts do not erase runtime evidence.
- Deduplicate events by stable identity. Declare cache/history limits and reconnect gaps/truncation. Supply sufficient cursor/window/revision information for clients to establish a baseline and highlight only newly observed work, without pretending durable total event replay. Keep secrets/raw host configuration out of browser payloads; retain only bounded, sanitized evidence fields needed by the views.
- Reuse D01 classification for API and CLI consumers rather than computing health in the collector. Migrate existing request-time fleet callers to the shared server cache; preserve useful legacy access while eliminating its request-amplified collection.

**Touches**

`district/status.py` collection/public projection helpers, `district/dashboard.py` server lifecycle and `/api/fleet`/page paths, `district/host.py` subprocess boundary where required, and the existing `district/atlas.py` payload consumer. Prefer one small collector integrated with these modules; introduce a separate module only if it makes ownership clearer. Update relevant tests and existing collection/schema/freshness documentation. Do not change the hourly `district/metrics.py` ownership model.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Launch the actual dashboard and open several browser tabs against it. Observe cache revisions and source ages. Instrument underlying command invocations in a controlled ten-factory run: tab count and repeated HTTP requests must not multiply collection work; report measured cadence, subprocess/collection cost, timeout behavior and configured bounds without altering production schedules.
- With one controlled factory hung, other factory revisions continue to publish. With GitHub/full snapshot failure, fresh local runtime remains visible. With an old repeated source timestamp, age increases to stale rather than being renewed. Observe explicit unsupported/unknown for an older engine.
- Exercise empty/malformed/truncated history, duplicate event records, reconnect gaps and partial errors. Counts/evidence remain usable; no invented transitions or completions result. Stop the server during collection and show that its subprocess/collector work is bounded and shuts down cleanly.
- Record installed Factory version/schema verification and actual-host runtime measurement in the handoff. Fixture-only parser coverage does not discharge the cross-repository deployment hold. Browser evidence verifies served data/freshness, not the later graphical design.

**Out of scope**

Factory instrumentation/deployment, a database/event broker/WebSocket service, per-client collection, permanent incident history, telemetry reconstructed from logs, or the graphical redesign owned by D03–D06. No schedule changes to create activity for a demonstration.

## D03 — Provide a working, scoped console shell over real fleet state

**Scope**

Outcome: an operator can navigate Overview / Flows / Brief with a visible consistent factory scope and return to the same context, already seeing real minimal fleet state rather than an empty shell. Dependency: D02 accepted/merged. Read `OPERATIONS-CONSOLE-SPEC.md` §§1 and 5, plus collection/state contracts from D01/D02. Keep existing root/legacy access until D09; explicit `/?view=...` URLs can expose the new console before cutover.

- Implement one navigation, typography, spacing, labeled status colors and keyboard-focus system. Scope is all factories or a registered factory. Support `/?view=overview`, `/?view=flows&factory=mikeroySoft/gpuflo`, optional `&stage=gate`, and `/?view=brief&factory=mikeroySoft/gpuflo`; encode actual slugs safely rather than hard-coding the examples.
- Every primary destination renders a usable minimal projection of the selected **real cached** fleet observation: factory names, operational state/findings, observation quality/source age and useful navigation to the selected factory. No blank route, fake counters, sample-data fallback or placeholder-only page satisfies this ticket. This is the completed navigation milestone, not a claim to have finished the graphical Overview, investigative Flows or authored Brief.
- View changes preserve visible scope. Scope changes clear invalid execution/stage/panel selection. Deep links and refresh restore valid selection; Browser Back restores previous scope, stage/execution selection, panel state and scroll. Implement the shared selection/history mechanism now for D05's evidence content. Explain removed factories and expired evidence instead of showing a different target's facts.
- Polling updates keep focus, selection and scroll stable. Ordinary navigation is not trapped in permanent modals. Narrow layouts stack content readably and support the later full-width evidence view; facts remain readable without color, at keyboard-only use and without shrinking text.
- Preserve existing management access on a secondary legacy route until D08 replaces it, subject to D07 authorization. Keep legacy analytics/reference reachable separately as needed; do not copy city architecture, style switchers, LOC-sized buildings or traffic-light window chrome into the operational shell. Ordinary viewing remains primary.

**Touches**

`district/dashboard.py` routing/page serving, existing `district/atlas.py`/`district/atlas.html` routing boundaries, and the smallest shared console HTML/CSS/JS surface needed. Reuse the actual package asset-serving pattern and D02 API rather than a second server. Update existing navigation documentation and relevant route/selection behavioral checks; retain prototype files unchanged for visual comparison.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Launch the real dashboard with nonempty real collector data, then use a browser to navigate all three explicit routes, switch all-fleet/single-factory scope and verify real state/freshness on every route. A route scaffold alone fails this gate.
- Exercise a stage/execution deep link, refresh, view switch, scope change, Browser Back and scroll restoration; demonstrate removed-factory and expired-selection messages. Poll several times while a keyboard-focused control and scrolled region remain stable.
- Capture desktop and narrow-viewport screenshots and keyboard interaction evidence. Status labels remain understandable without color; legacy management remains reachable but not the primary navigation. No human visual signoff of the full design is claimed at this intermediate milestone.

**Out of scope**

The completed graphical Overview (D04), detailed flow/evidence content (D05), deterministic reading narrative (D06), new management UI (D08), new classification/collection policy, prototype removal or changing the default home before D09.

## D04 — Make Overview a live, truthful graphical watch surface

**Scope**

Outcome: the operator can leave Overview visible and distinguish running, legitimately waiting, operationally broken and observation-unknown factories from live, concurrent graphical state. Dependency: D03 accepted/merged. Read `OPERATIONS-CONSOLE-SPEC.md` §§1–2, 6, 8 and 11. This is the first complete visible milestone; sample data alone cannot complete it.

- Replace the minimal Overview body with stable, equally weighted factory positions and compact flow diagrams. Never resize by LOC or reorder on each poll. Show name, operating state, dispatcher/admission state, aggregate stage occupancy, active/configured capacity, latest observed transition, confirmed next dispatch, freshness and operational findings.
- One factory can occupy multiple stages concurrently: aggregate its distinct executions without assigning the entire factory one moving position. Scheduling/admission and merge eligibility are distinct from execution. Queue demand is not dispatcher liveness. Only show reported stages/edges; gaps never become invented intermediate movement.
- Add a compact count strip for normal operation, operational attention and unknown observation using D01 semantics; unknowns are not healthy. Show supported dependency/resource scope and evidence, explicitly unknown ownership where appropriate. One failed factory probe is not a fleet outage.
- Add bounded recent activity for observed transitions, completions, runtime failures and recoveries, with its actual history window and gaps. Select a factory into scoped Flows; selecting a stage also selects its filter via D03 navigation. Keep evidence references available for D05's shared panel.
- Implement motion as evidence: gentle pulse only for fresh confirmed active stages; brief motion for a distinct newly observed transition; one highlight for a newly observed completion; quiet countdown only for a known dispatch schedule; persistent labeled findings rather than flashing alarms. Stale/unavailable diagrams are stationary and muted with last known state and age; interrupted/orphan observations cannot animate forever.
- Baseline initial connection; deduplicate by event identity and do not replay old work on poll, reconnect, duplicate record, route revisit or tab return. Expose history gaps. No decorative perpetual circulation, progress percentages, inferred ETAs or rapid flashes.
- Watch mode hides secondary controls, not factory names/state/freshness/alerts; it is a mode of this view, not another route. Reduced-motion and pause-motion preserve facts and make clear that animation pause does not pause factories. Stop unnecessary animation in hidden tabs; retain focus/scroll on updates and readable stacked diagrams at narrow widths.

**Touches**

The shared console surface introduced by D03; D01/D02 projection fields only where required to expose already-supported facts. Reuse event/window metadata and navigation rather than a second client incident store. `district/prototype-fleet.html` and `district/prototype-flow.html` are visual references only, not production telemetry code. Update existing watch/motion documentation and targeted motion/state behavior checks.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Launch the actual console against installed F03 telemetry. In the browser, demonstrate two simultaneous executions in one factory at different stages, a real observed transition/completion and distinctions between running, scheduled/resource/capacity waiting, capped/unexpectedly stopped and stale/unknown observation. Controlled real Factory runs may supply activity; label fixture-only fault cases separately. Record source timestamps and actual screenshots, not prototype captures.
- Exercise worker → gate → review and revision-loop observations without conflating executions; duplicate records/poll/reconnect/revisit must not replay old movement. A stalled source or interrupted process becomes stationary, not endless active motion. Verify count strip/findings against the same D01/D02 snapshot.
- Exercise factory/stage selection into Flows and back, watch mode during a prolonged desktop session (report actual duration), pause/resume animation, hidden tab, reduced-motion, keyboard and narrow viewport. Record screenshots/interactions showing labels, alerts, focus and scroll retained.
- Submit real-telemetry visual evidence for human review with D05. Passing this implementation gate is not human approval; D09 remains blocked until the human has accepted the combined Overview/Flows design.

**Out of scope**

Decorative city/architecture maps, repository analytics, sample-only completion, reconstructed phases, stall heuristics, new scheduling behavior, durable event replay, management controls or production cutover.

## D05 — Investigate concurrent flows through one reusable evidence panel

**Scope**

Outcome: an operator can investigate an Overview condition through scoped concurrent execution flows and bounded evidence, then return without losing context. Dependency: D04 accepted/merged. Read `OPERATIONS-CONSOLE-SPEC.md` §§3, 5–8 and 11. This ticket owns the evidence component used by Overview, Flows and subsequently Brief.

- Expand Flows into all-factory comparison or one-factory concurrent execution lanes. Show dispatcher/scheduling context, only reported triage/worker/gate/review/merge phases, known stage entry times, wait reasons, retries/review rounds, merge eligibility, resources/dependencies and a bounded operational timeline.
- Use stable execution/dispatcher/ticket/attempt identities from Factory. An existing PR revisited in another dispatcher pass is not the same execution. Display terminal outcomes and interrupted observations; elapsed time is duration, not progress percentage or ETA. Keep resource wait distinct from failure and distinguish unknown holder from a confirmed holder.
- Reuse D01 findings: normal review revision, project escalation, product gate failure, waiting for CI/capacity/scheduled pass or legitimately held resources are not automatically incidents. Show supported upstream impact without marking every idle downstream stage independently failed or guessing a common cause from timing.
- Implement one evidence panel used from Overview and Flows, with an API/component that D06 can reuse: observed condition and affected scope; execution impact separate from inferred cause; source timestamps and observation quality; bounded source excerpt/reference; evidence-supported next step; relevant Factory link. Unknown evidence/expired history is explained rather than replaced with invented detail. Deduplicate lifecycle entries by identity and declare window/truncation.
- Reuse supported Factory operations `/#ops`, ticket `/#t=N` and stage `/#s=stage` destinations; validate allowed stage/ticket values and constructed destinations. Use a reachable browser hostname plus the registered dashboard port, not a LAN viewer's localhost or an unvalidated forwarded/Host value. Escape all remote text and bound/sanitize excerpts so secrets are not exposed.
- Keep District open when linking out. If Factory dashboard is unavailable, District evidence remains readable with a clear unavailable-link explanation; do not guess report-specific routes. Preserve D03 scope/filter/panel/scroll across Overview → Flows → evidence → Factory and back, refresh/deep links and invalidated targets. Keyboard and narrow layouts retain the whole evidence panel without a permanent modal navigation trap.

**Touches**

Shared console routing/selection and Overview/Flows content from D03/D04; evidence rendering and Factory destination construction currently spread through `district/status.py` and `district/atlas.py` as needed. Use D02's bounded public evidence rather than worker logs or full private config. Update targeted link/escaping/selection/flow behavior checks and existing navigation/evidence docs.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- In an actual browser using real F03 lifecycle data, open an Overview stage, inspect two concurrent executions, follow a worker/gate/review/revision sequence and distinguish a later dispatcher revisit from an earlier attempt. Verify known waits, terminal/interrupted state, source age, evidence window and unknown ownership against the emitted observations.
- Exercise normal product gate failure versus inability to invoke the configured gate; only the latter produces a supported machinery finding. A supported upstream failure shows downstream impact without inventing separate stage failures. Use controlled fault inputs for unsafe/unavailable cases and identify those explicitly in the handoff.
- Complete Overview → scoped Flows → evidence → Factory → District return on desktop and LAN. Verify `/#ops`, `/#t=N` and supported stage links, reachable LAN hostname/port, preserved scope/filter/panel/scroll, refresh, removed target, expired evidence and unavailable Factory dashboard. Browser text remains safe for malicious remote markup and invalid link data.
- Capture keyboard and narrow-layout evidence plus real-telemetry desktop screenshots. Present D04/D05 together for explicit human visual approval; record approval when supplied externally, never substitute worker/test signoff. D09 stays held until that approval exists.

**Out of scope**

A cloned Factory report/log viewer or decision inbox, guessed report URLs, private Factory imports, lifecycle reconstruction, LLM diagnosis, independent health calculations, project actions or archive/cutover.

## D06 — Read a deterministic Brief from the same snapshot and evidence

**Scope**

Outcome: Brief lets the operator catch up on the actual available operational window without disagreeing with Overview or Flows. Dependency: D05 accepted/merged. Read `OPERATIONS-CONSOLE-SPEC.md` §§4–6 and 11. Reuse D01 classification, D02 observations and D05 evidence; this view owns no independent health or incident store.

- Replace the minimal Brief body with a clear current verdict including unknown/partial observation; supported operational attention grouped by cause and affected factories; recent operational changes/recoveries within an explicitly displayed available window; and maintenance covering configuration/engine drift, applicable doctor findings and host policy.
- Group only where evidence establishes shared cause/scope; preserve the per-factory observations beneath groups. Coincident failures do not prove a common outage. Intentional version pins are not drift. Surface unknown/missing maintenance observations rather than infer absence of problems.
- Generate deterministically from one snapshot revision. Every statement links to its affected scope and D05 evidence; navigation uses D03's same scope/context model. The current verdict/counts/findings agree with Overview and Flows at that revision.
- Recent change/recovery wording must be supported by D02's actual retained interval and completeness. Display gaps/truncation; do not say “since yesterday” or “since last visit” unless that exact history exists. Keep readable narrative hierarchy within the shared visual system, at desktop and narrow widths.

**Touches**

Brief body in the shared console, reusable evidence/navigation, and existing maintenance projection fields from `district/status.py`, `district/host.py` and `district/apply.py` only as needed to expose supported observations. Use slower cached config/doctor/engine data from D02; rendering Brief must not run new network probes or management commands. Update relevant deterministic grouping/drift/window checks and existing Brief documentation.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Launch the actual console and inspect Brief in a browser with the same snapshot revision as Overview and Flows. Compare normal/attention/unknown counts and every displayed finding across views; repeated rendering of unchanged data yields the same substantive statements/grouping.
- Exercise partial/stale observation, independently coincident failures, an evidenced shared resource problem, recovery, history gap/truncation, intentional version pin versus actual drift, applicable doctor finding and missing maintenance data. Each statement exposes correct scope, source quality and bounded supporting evidence; no invented onset or catch-up period.
- Follow every category of Brief link into evidence/Flows and back while retaining factory scope. Capture desktop/narrow screenshots and keyboard interaction. Actual browser evidence proves the reading view; fixtures alone only prove deterministic edge cases.

**Out of scope**

LLM-generated narrative, a second classifier or incident/history database, a project decision inbox, analytics screens, speculative common-cause inference, automatic repair, management execution or cutover.

## D07 — Make LAN viewing read-only and host mutations fail closed

**Scope**

Outcome: network viewing cannot accidentally grant host-management authority. No semantic/UI prerequisite; this ticket can run independently, but its `district/dashboard.py` changes must be accepted/merged and integrated **before D02 starts**. D08 may not expose new management controls before this policy is complete. Read `OPERATIONS-CONSOLE-SPEC.md` §9 and the existing action/detect routes.

- Remove the rule that a non-loopback `--host` binding authorizes remote actions. Binding `0.0.0.0` or another LAN address only enables viewing. Unauthenticated network clients remain read-only and all mutation paths reject them server-side, even if they know/send `X-District-Act: 1` or call endpoints directly.
- Establish and document the minimal one-operator trusted-local authorization policy: use the actual loopback peer boundary and a validated local request destination/origin with existing CSRF protection. A custom header is a CSRF signal, never authentication. Do not trust forwarded client addresses/hosts to turn remote traffic into local authority; ambiguous/reverse-proxied contexts fail closed. Reject cross-origin or invalid Host/Origin requests, including DNS-rebinding-style requests, before invoking commands. No insecure remote-management toggle.
- Apply the same policy to every current and future mutation route, including legacy management, rather than merely hiding buttons. Preserve legitimate local authorized operations with CSRF/origin checks; expose enough non-secret capability information for D08 to explain read-only mode.
- Inspect `/api/detect` and read payloads separately: read-only access does not entitle a client to arbitrary host paths/configuration or credential-bearing command output. Validate inputs, bound/sanitize returned diagnostics and keep read surfaces from becoming another mutation/secret-disclosure path. Preserve safe viewing and existing add/adopt detect behavior for the authorized local operator.
- Update current help/install/network documentation that says explicit `--host` opens actions. Keep authorization centralized so D08's typed management requests consume it, not a parallel policy.

**Touches**

`district/dashboard.py` (`act_allowed`, `Handler.do_POST`, read/detect boundaries, `main`, server capability response and help); legacy controls in `district/atlas.html` only as necessary to honor/read the policy. Relevant security behavior in `tests/test_district.py` and existing LAN/management documentation. Preserve the host/repository ownership rules.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Launch the actual dashboard bound for LAN viewing. Use a genuine non-loopback client plus browser evidence to show readable fleet state and refusal of every mutating route with/without the custom header, direct requests and spoofed forwarded headers. Record that no CLI mutation was invoked, not just that a button was disabled.
- Exercise trusted-local authorized requests in an isolated disposable management environment; correct local origin/CSRF succeeds, absent/wrong header, cross-origin, invalid Host and rebinding-like requests fail before execution. Verify the behavior for default loopback and explicit LAN bindings.
- Exercise detect/read responses for invalid targets, remote text and credential-like diagnostics: safe bounded output, no arbitrary host/config disclosure. Capture read-only/local capability presentation in the existing browser surface. Report the exact supported local deployment topology and fail-closed behavior; there is no implicit reverse-proxy trust.

**Out of scope**

A full account system, remote authentication/remote management, new business operations, D08's replacement forms, arbitrary CLI command exposure, networking policy for Factory itself, or changes to production accounts/services during validation.

## D08 — Preserve existing management through explicit, safe operator workflows

**Scope**

Outcome: the console's secondary management surface safely performs every existing District-owned operation with an exact target/effects, deliberate confirmation, streamed result and verified postcondition. Dependencies: D05 and D07 accepted/merged. Read `OPERATIONS-CONSOLE-SPEC.md` §9; D07 authorization is a mandatory server-side boundary, not an optional UI check.

- Migrate the current control inventory without losing capability: path/GitHub-URL Detect and Add / adopt; exclusive-hardware check names; fleet Apply/reconcile; fleet Upgrade engine; selected-factory Apply/reconcile; Reset capped timer; Remove. Preserve explicit no-editor onboarding behavior by showing the detected proposal and obtaining confirmation before execution rather than silently hiding what will be written. Existing Atlas view/camera style controls are legacy reference features, not operations to reproduce.
- Use bounded, typed action requests for these operations with server validation and an allowlisted mapping to existing District CLI commands. Remove arbitrary public CLI argument arrays and migrate every caller, including any retained legacy surface; no insecure old endpoint remains as a compatibility bypass. Selected slugs must match registered factories; onboarding input/options are validated independently.
- Confirm the exact target set and effects immediately before action, distinguishing fleet-wide from one-factory operations. Show detected slug/path/port/check proposal and exclusive settings for add/adopt; upgrade effects on the shared engine/timers/dashboards; reset's cause-repair prerequisite and dispatcher pass; removal's unit/registry effects with repository content retained. Cancellation executes nothing. New management is secondary, not a primary nav destination.
- Serialize conflicting mutations server-side across browser tabs, including fleet/one-factory overlap; disabling one page's buttons is insufficient. Keep operations on existing add/apply/rm ownership paths. Respect current active-pass/removal refusal or explicitly authorized wait semantics rather than forcing termination.
- Stream bounded sanitized output and explicit command completion. Nonzero exit, rejected request or stream disconnect never displays success. Refresh relevant cached state after completion to show the observed postcondition without converting ordinary browser polls into collection triggers. If completion/postcondition cannot be observed, say unknown rather than retrying a possibly completed mutation automatically.
- Read-only LAN clients get a clear explanation and no misleading enabled controls; direct calls remain refused by D07. Retain all evidence/navigation during an operation and after failures. Reset is an option after repairing the cause, not a universal recommendation. Escape remote output and never expose credentials in logs/config previews.

**Touches**

Shared console secondary management UI, `district/dashboard.py` action validation/serialization/streaming, legacy `district/atlas.html` management callers, and existing `district/add.py`, `district/apply.py`, `district/rm.py` boundaries only where necessary for truthful result handling. Reuse D02 cache refresh and D05 evidence; update targeted operation/race/error checks and existing management docs.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Launch the actual console in an isolated disposable host/registry environment. In a browser exercise Detect; new add and existing-config adopt with exclusive checks; fleet and one-factory reconcile; engine upgrade; reset after a repaired cause; and removal. Capture each confirmation's exact scope/effects, streamed result and freshly observed postcondition. Where system-level actions require authorization, obtain it for the disposable target before execution; production changes are not a test shortcut.
- Cancel confirmation and verify no operation. Submit invalid/unknown action options/slugs, concurrent conflicting requests from two tabs, CLI failure and a disconnected stream: no arbitrary argument execution, racing mutation or false success. Verify reset only clears the cap after its pass succeeds, and removal leaves repository files intact and handles an active pass according to existing CLI behavior.
- Demonstrate server refusal from an unauthenticated LAN client even with the old custom header and old argument-array body. Check the retained legacy route cannot bypass policy/typed requests. Capture keyboard and narrow-layout workflow evidence, safe output rendering and return to the same scoped evidence after the action.
- Report a control-by-control migration inventory showing the listed existing operations remain available to the authorized operator. Unit tests alone do not prove the management workflow.

**Out of scope**

Factory ticket/PR/label/gate/review decisions, arbitrary CLI APIs, new recovery/scheduling policy, remote management authorization, repository deletion, automatic action retries, full account management or prototype/default-route cutover.

## D09 — Cut over to the approved console while preserving archived references

**Scope**

Outcome: the reviewed operational console becomes the sole operations home without losing management or legacy reference access, and prototype sources remain recoverable from a pinned archive. Dependencies: D04, D06 and D08 accepted/merged (therefore the entire UI chain and D07). Read `OPERATIONS-CONSOLE-SPEC.md` §§1, 5, 10–12. This is a held integration/cutover ticket, not permission for a worker to deploy.

Release holds, all mandatory before this ticket is dispatchable for source cutover:

- Human visual approval of D04/D05 using real Factory telemetry is recorded, with any required corrections accepted. Automated review, tests and prototype screenshots are not that approval.
- The completed D06/D08 console has human final desktop/LAN operational acceptance against the scenarios below. If acceptance requests changes, keep this ticket held until they land; do not waive them as follow-up work.
- An explicitly authorized operator has archived `district/prototype-fleet.html`, `district/prototype-flow.html`, `district/prototype-brief.html` and `district/prototype_dashboard.py` on a reference branch and published a pinned commit/file pointer. Verify those sources are retrievable from the published pointer **before** removing any production-accessible prototype sources/routes. An unpushed branch name or promise to archive later is insufficient.

After these holds are satisfied, make the ordinary root/default launch open Overview, with Flows and Brief the only peer operations views. Keep explicit deep links/context working. Remove production-accessible prototype launch/routes/assets only after archive verification; retain a clearly secondary legacy Atlas/reference route if needed to preserve existing analytics/architecture access. It must not compete as an operations home. Existing management inventory must be available through D08, with D07 policy applied everywhere.

Update existing README/help/service/default-route documentation and release notes with the new navigation, CLI semantic migration, network read-only policy, Factory schema prerequisite, archive pointer and intentional legacy access. Preserve PRD.md/PLAN.md historical records; add precise supersession references rather than overwrite their unrelated requirements. Remove obsolete production callsites/styles/comments for replaced paths, but retain unrelated user work and historical references. Source completion does not authorize installation, service restarts or production rollout; deployment remains a separate explicit operator action after final review.

**Touches**

`district/dashboard.py` default/routing/install help, shared console assets, `district/atlas.py`/`district/atlas.html` legacy exposure, the four named prototype files only after the archive hold, and existing README/PRD/PLAN/release documentation. Update route/CLI consumers and behavior checks affected by clean cutover. No branch/commit/push is performed merely because this draft exists.

**Exit gate**

- Run `uv run python -m unittest discover -s tests` from District root after implementation.
- Before source removal, record the human approvals and verify the pinned remote archive contains all four complete prototype sources. After cutover, launch the actual dashboard normally: root is live graphical Overview, explicit Overview/Flows/Brief deep links work, legacy reference is secondary/reachable if retained, and production prototype routes are absent. The archive must remain retrievable.
- Capture final actual-browser desktop/LAN evidence covering: two concurrent stages in one factory; worker → gate → review with revision and distinct deduplicated transitions; ordinary project failure versus supported machinery failure; scheduled idle, capacity/resource wait, cap, unexpected stop and stale/unknown distinction; interrupted/orphan observations ceasing activity; partial GitHub failure retaining runtime and one slow source not freezing others; reconnect/duplicate/truncated-history motion behavior.
- Complete Overview → scoped Flows → evidence → Factory → back including removed/expired/unavailable targets; compare Brief counts/findings at the same revision; verify keyboard, narrow viewport, reduced-motion, animation pause and a prolonged watch session (record duration). Reuse documented D02 ten-factory/multi-tab cadence/cost evidence and re-exercise shared cache behavior on the integrated server without changing production schedules.
- Recheck LAN viewing with all unauthorized mutations refused server-side; exercise authorized disposable-target management confirmations/result/postcondition and account for every D08 migrated control. Record the final human acceptance decision and any remaining deployment prerequisite explicitly; a worker cannot sign it on the human's behalf.
- Handoff distinguishes accepted source cutover from production deployment, names the verified installed Factory schema and pinned prototype archive, and lists the exact browser/operational scenarios exercised. No sample-only UI or test-only proof passes this gate.

**Out of scope**

Autonomous visual approval, archive publication or deployment without explicit authorization, deleting prototypes before verified archival, rewriting historical PRD/PLAN wholesale, replacement analytics products, project remediation, new scheduling policy, multi-host/team support or unrelated cleanup.
