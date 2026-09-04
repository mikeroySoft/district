"""The District Atlas: `data(fleet)` renders the DATA JS block from the live fleet, `page(fleet)` the page.

The engine (projection, camera, legend, inspector, three themes) lives in
atlas.html, carved from the codebase-atlas template. Blocks that describe code
(engine, GitHub, host runtime, state, District) are constants below with their
citations; only the factory blocks, the FACTORIES plate, the per-factory roads,
and the KPI strip are generated. Height: h = 12 + 108·√(LOC / max LOC).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path

TEMPLATE = Path(__file__).with_name("atlas.html")
AF_SHA = "8f9baad"  # agent-factory commit the engine citations point at
DC_SHA = "f413e8c"  # district commit the District citations point at
AF = f"mikeroySoft/factory@{AF_SHA}/agent_factory/"
DC = f"mikeroySoft/district@{DC_SHA}/district/"
DISPATCH = [15.1, 7.5]  # road origin: dispatch.py
UPSTREAM = [21.6, 1.3]  # road origin: the fork-parent pad
FACTORY_X0, FACTORY_STEP, FACTORY_Y = 8.0, 6.5, 16.2

HEAD = r"""'use strict';
const GH = 'https://github.com/';
const CAT = {
  factory:  { name: 'Factories (managed repos)',        color: '#E8493E' },
  engine:   { name: 'agent-factory engine',             color: '#E3A83B' },
  district: { name: 'District (host control)',         color: '#5B8DD6' },
  runtime:  { name: 'Host runtime (agents, model, lock)', color: '#4CBB6C' },
  ext:      { name: 'GitHub',                           color: '#9B7BD8' },
  state:    { name: 'Factory state on disk',            color: '#8A93A5' },
};
const KIND = {
  control: { name: 'Control path',     color: '#E8A87C', dash: null,  label: 'timer ticks, subprocess spawns, gh mutations' },
  data:    { name: 'Evidence / records', color: '#62D6E8', dash: '7 5', label: 'reports, verdicts, labels, snapshots' },
  dep:     { name: 'Config layering',  color: '#55617A', dash: '2 4', label: 'host file → effective per-repo config' },
  build:   { name: 'Fork upstream sync', color: '#9B7BD8', dash: '7 5', label: 'upstream main merged under the host gate' },
};
const HEALTH_COLOR = { healthy: '#4CBB6C', attention: '#E3A83B', failing: '#E8493E' };
"""

# Buildings that describe code, not fleet state. gx,gy grid position; w,d footprint; h height px; kind = silhouette.
STATIC_BLOCKS = r"""  // GitHub (back row)
  { id:'issues', name:'Issues & labels', cat:'ext', gx:7.5, gy:0.3, w:2.2, d:2, h:8, kind:'pad',
    blurb:'The ticket queue for every factory. Six fixed labels carry state: needs-triage → ready-for-agent (or needs-info) → ready-for-human on escalation; factory-approved marks a reviewed PR.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/config.py',21,'the six label constants'], ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',150,'frontier(): ready-for-agent, unassigned, unblocked'] ],
    conn:'Claimed and escalated by the dispatcher; labelled by triage.' },
  { id:'prs', name:'Pull requests + CI', cat:'ext', gx:14, gy:0.3, w:2.2, d:2, h:8, kind:'pad',
    blurb:'One PR per ticket on branch agent/<n>. A merge needs four independent pieces of evidence: gate PASS in the body, the factory-approved label, green GitHub checks, and a head that already contains main.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',632,'land_pass(): the four-way merge precondition'], ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',561,'approve_pr(): durable APPROVE as a label'] ],
    conn:'Written by the reviewer step; merged by the merge stage.' },
  { id:'upstream', name:'Fork parents', cat:'ext', gx:20.5, gy:0.3, w:2.2, d:2, h:8, kind:'pad',
    blurb:'A factory that is a fork tracks its parent: every pass fetches upstream main and, when it moved, merges it into the fork’s main under the host gate. A conflict or gate failure parks the stage on one ready-for-human issue until a human closes it.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',469,'sync_pass(): one merge per pass, conflicts open a ready-for-human issue'], ['mikeroySoft/factory@8f9baad/agent_factory/config.py',49,'[repo].upstream is repo-owned'] ],
    conn:'Feeds the ringed factories only; net-new repos have no sync loop.' },

  // Host runtime (right)
  { id:'llm', name:'Local triage model', cat:'runtime', gx:27.5, gy:13, w:2.2, d:2, h:8, kind:'pad',
    blurb:'An OpenAI-compatible chat endpoint on this host running an open model. Only triage talks to it: a deterministic lint first (body ≥ 80 chars, acceptance criteria present), then one JSON completion decides the label. wontfix is only ever proposed.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/triage.py',108,'call_llm(): one chat completion per issue'], ['mikeroySoft/factory@8f9baad/agent_factory/triage.py',203,'decision → label'] ],
    conn:'Endpoint and model name come from the host file, not the repos.' },
  { id:'gpulock', name:'Host lock (exclusive checks)', cat:'runtime', gx:32, gy:13.2, w:2, d:1.6, h:10, kind:'gate',
    blurb:'One GPU, many worktrees. Gate checks marked exclusive serialise on a host-wide flock so two factories never run clippy/tests on the GPU at once; every check has a timeout so a wedged run fails instead of holding the lock.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/gate.py',58,'run(): flock + timeout per check'], ['mikeroySoft/factory@8f9baad/agent_factory/config.py',50,'gate.lock is host-owned'] ],
    conn:'Taken by the gate for every check marked exclusive.' },
  { id:'worker', name:'Worker agents (omp · droid)', cat:'runtime', gx:27.5, gy:17, w:2.6, d:2.6, h:26, kind:'plant',
    blurb:'The coding agents. The dispatcher spawns the configured CLI in the ticket’s worktree with the issue, its comments, standing rules, the repo’s lessons file, and — on retries — the last gate report or review findings. Ticket label picks the worker: chore → droid, else omp. They run unsandboxed on this host today (PRD §6a).',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',217,'run_worker(): argv, worktree, log'], ['mikeroySoft/factory@8f9baad/agent_factory/config.py',37,'DEFAULT_WORKER / DEFAULT_CHORE_WORKER'] ],
    conn:'Spawned per attempt; finishes by running factory gate.' },
  { id:'reviewer', name:'Reviewer (codex exec)', cat:'runtime', gx:32, gy:17.4, w:2, d:2, h:18, kind:'kiosk',
    blurb:'A second model reviews the diff against the issue and the gate report. Every finding must cite path:line; the output ends VERDICT: APPROVE or REVISE. REVISE bounces once to the worker, then escalates.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',318,'review(): two-axis prompt, verdict parse'], ['mikeroySoft/factory@8f9baad/agent_factory/config.py',39,'DEFAULT_REVIEWER'] ],
    conn:'Its APPROVE becomes the factory-approved label on the PR.' },

  // Engine (middle band) — LOC-scaled at 8f9baad
  { id:'config', name:'config.py (host layer)', cat:'engine', gx:6.8, gy:6, w:2.2, d:2, h:16, kind:'slab',
    blurb:'267 lines: the single source of every repo-specific value. Loads the committed .factory.toml over the host file: [defaults] < [repo."owner/name"] < repo. Only host-owned tables (triage, workers, review, install, dashboard.port, gate.lock) may come from the host; gate checks and upstream never do, so a clone elsewhere runs the same gate.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/config.py',218,'load(): layered merge'], ['mikeroySoft/factory@8f9baad/agent_factory/config.py',49,'HOST_TABLES / HOST_KEYS'] ],
    conn:'Read by every command; reads the host file District writes.' },
  { id:'triage', name:'triage.py', cat:'engine', gx:10.2, gy:6, w:2, d:2, h:16, kind:'hall',
    blurb:'289 lines. Labels every needs-triage issue: lint, then the local model. Outcomes: ready-for-agent with an agent brief, needs-info with the question, ready-for-human, or a wontfix proposal comment.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/triage.py',247,'main()'], ['mikeroySoft/factory@8f9baad/agent_factory/triage.py',30,'endpoint from host config (FACTORY_LLM_URL override)'] ],
    conn:'Talks to the model and to Issues.' },
  { id:'dispatch', name:'dispatch.py', cat:'engine', gx:13.6, gy:6, w:3, d:3, h:20, kind:'hall',
    blurb:'981 lines, the pipeline. One stateless pass: upstream sync → merge stage (≤1 PR) → claim up to max_active tickets → worker → gate → PR → review → bounce or escalate. Every transition is appended to .factory/events.jsonl with its evidence pointers.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',929,'main(): one pass'], ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',815,'process_ticket(): attempts, gate, review'], ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',289,'escalate(): ready-for-human + handoff comment'], ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',204,'record(): the audit trail'] ],
    conn:'Runs inside each factory checkout, fired by that factory’s timer.' },
  { id:'gate', name:'gate.py', cat:'engine', gx:18, gy:6, w:2, d:2, h:15, kind:'gate',
    blurb:'163 lines. Deterministic evidence: conflict-markers, the repo’s [[gate.check]] list in order, then a leak scan of added lines. Writes a Markdown report the reviewer and the PR body both carry.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/gate.py',98,'main()'], ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',266,'run_gate(): the report of record'] ],
    conn:'Run by workers, re-run by the dispatcher.' },
  { id:'dashboard', name:'dashboard.py + html', cat:'engine', gx:21.5, gy:6, w:2.6, d:2.4, h:24, kind:'sensor',
    blurb:'2,716 lines (1,012 py · 1,158 html · 546 atlas). One HTTP server per factory: tickets by stage, gate reports, worker logs, journal heartbeat, upstream drift, and an action inbox. --json is the contract District reads: version, metrics (first-gate pass, bounce), consecutive failed passes.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/dashboard.py',738,'snapshot()'], ['mikeroySoft/factory@8f9baad/agent_factory/dashboard.py',595,'metrics(): fleet KPIs'], ['mikeroySoft/factory@8f9baad/agent_factory/dashboard.py',554,'consecutive_failures()'] ],
    conn:'One instance per factory runs as a persistent user service; District polls them.' },
  { id:'onboard', name:'onboard.py (init · doctor · install)', cat:'engine', gx:10.2, gy:9.2, w:2, d:1.6, h:16, kind:'hall',
    blurb:'335 lines. init writes the config, labels, issue template; doctor checks tools, auth, remotes, drift and speaks --json; install renders and converges the systemd units with defaults from the host file.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/onboard.py',245,'units(): timer + service + dashboard'], ['mikeroySoft/factory@8f9baad/agent_factory/onboard.py',143,'doctor(): drift checks'], ['mikeroySoft/factory@8f9baad/agent_factory/onboard.py',276,'install(): convergent'] ],
    conn:'Called by district add and district apply.' },
  { id:'learn', name:'learn.py · stats.py', cat:'engine', gx:18, gy:9.2, w:2, d:1.6, h:16, kind:'kiosk',
    blurb:'291 lines. learn distils the last N tickets’ trails into ≤10 repo lessons the workers carry; stats prints attempts, review rounds, hours-to-merge per ticket.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/learn.py',88,'main()'], ['mikeroySoft/factory@8f9baad/agent_factory/stats.py',165,'main()'] ],
    conn:'Operator-run; not on the timer.' },

  // Factory state on disk
  { id:'state', name:'.factory/ (events · worktrees · logs)', cat:'state', gx:9, gy:12.6, w:4, d:1.4, h:8, kind:'slab',
    blurb:'Gitignored per-repo state: events.jsonl audit trail, wt-<n> worktrees kept for forensics, per-attempt logs, per-ticket locks.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/dispatch.py',204,'record() appends events.jsonl'], ['mikeroySoft/factory@8f9baad/agent_factory/config.py',91,'Config.factory: <root>/.factory'] ],
    conn:'Written by the dispatcher; read by the dashboard and factory learn.' },

  // District (front-left)
  { id:'systemd', name:'systemd user timers', cat:'district', gx:0.4, gy:12.2, w:2.6, d:1.8, h:14, kind:'sensor',
    blurb:'Per factory: a timer firing a oneshot dispatch service, plus a persistent dashboard service. Units carry the operator PATH and the supply-chain policy env (npm min-release-age, uv exclude-newer) so workers never install a package younger than the policy.',
    files:[ ['mikeroySoft/factory@8f9baad/agent_factory/onboard.py',252,'unit templates'], ['mikeroySoft/district@f413e8c/district/apply.py',23,'POLICY_ENV'] ],
    conn:'Rendered by factory install; converged and capped by district apply.' },
  { id:'district', name:'district (add · apply · status · rm)', cat:'district', gx:0.6, gy:15.8, w:2.2, d:2.2, h:21, kind:'mast',
    blurb:'The control tower: onboards a repo in one command (fork detection, gate proposal, port allocation), converges every factory to the host file, upgrades the engine fleet-wide with stop → wait → reinstall → restore, disables a timer after 10 consecutive failed passes, and prints the fleet table with a health exit code. Never commits to a factory; never imports the engine.',
    files:[ ['mikeroySoft/district@f413e8c/district/add.py',299,'add: onboard / adopt'], ['mikeroySoft/district@f413e8c/district/apply.py',92,'upgrade(): stop → wait → reinstall'], ['mikeroySoft/district@f413e8c/district/apply.py',181,'failure cap → timer disabled'], ['mikeroySoft/district@f413e8c/district/status.py',49,'row(): health per factory'] ],
    conn:'Writes the host file and drives factory; reads dashboard --json.' },
  { id:'hostcfg', name:'Host file (registry + defaults)', cat:'district', gx:0.4, gy:19.2, w:2.6, d:1.4, h:8, kind:'slab',
    blurb:'~/.config/agent-factory/config.toml — the registry is the config. Per factory only path and dashboard port; everything the fleet agrees on (triage endpoint, install cadence, bind host, policy env) is promoted into [defaults]. Nothing here is ever committed to a repo.',
    files:[ ['mikeroySoft/district@f413e8c/district/host.py',83,'dedupe(): promote agreed values'], ['mikeroySoft/district@f413e8c/district/host.py',75,'next_port()'], ['mikeroySoft/factory@8f9baad/agent_factory/config.py',218,'read by config.load'] ],
    conn:'Written by district; read by every factory command.' },
  { id:'install', name:'Engine install (uv tool, one snapshot)', cat:'district', gx:0.4, gy:21.2, w:2.6, d:1.2, h:12, kind:'depot',
    blurb:'One frozen agent-factory install on the host, shared by every factory. Every unit’s ExecStart points at its python. Upgrading is explicit and fleet-wide; per-factory engine pinning is the next surface (PRD §5.8).',
    files:[ ['mikeroySoft/district@f413e8c/district/apply.py',101,'uv tool install --reinstall'], ['mikeroySoft/factory@8f9baad/agent_factory/onboard.py',246,'ExecStart = sys.executable -m agent_factory'] ],
    conn:'Replaced by district apply --upgrade; executed by every timer tick.' },
"""

STATIC_PATHS = r"""  { id:'p1', kind:'control', from:'systemd', to:'dispatch',
    label:'timer → dispatch',
    pts:[[1.7,13.1],[13.0,12.3],[15.1,7.5]], lt:0.5, ldy:-10,
    what:'factory-<repo>.timer activates factory-<repo>.service, which runs one stateless dispatch pass in the factory checkout with the unit’s PATH and policy env.',
    cite:[[AF+'onboard.py',252,'unit templates'],[AF+'dispatch.py',929,'main()']],
    payload:{r:3.2, dur:5, kind:'control'} },
  { id:'p3', kind:'control', from:'dispatch', to:'issues',
    label:'claim · escalate',
    pts:[[15.1,7.5],[11.6,4.0],[9.6,2.6]], lt:0.5, ldy:-10,
    what:'The dispatcher re-reads the issue, assigns itself and takes a per-ticket flock; on budget/gate/review exhaustion it drops the assignee, swaps the label to ready-for-human, and comments the reason plus the worker’s handoff notes.',
    cite:[[AF+'dispatch.py',150,'frontier()'],[AF+'dispatch.py',289,'escalate()']],
    payload:{r:3, dur:4.6, kind:'control'} },
  { id:'p4', kind:'control', from:'dispatch', to:'worker',
    label:'spawn worker',
    pts:[[15.1,7.5],[24.6,10.4],[25.4,13.8],[28.8,18.3]], lt:0.55, ldy:-10,
    what:'argv template from the host file ({prompt}, {cwd}); stdout/err to .factory/logs/<n>-attempt-<k>.log; up to max_attempts rounds within budget_min.',
    cite:[[AF+'dispatch.py',217,'run_worker()'],[AF+'config.py',37,'worker argv defaults']],
    payload:{r:3, dur:5, kind:'control'} },
  { id:'p5', kind:'control', from:'worker', to:'gate',
    label:'factory gate → report',
    pts:[[28.8,18.3],[26.6,16.4],[26.6,11.8],[21.4,11.8],[21.4,9.0],[19,7]], lt:0.5, ldy:16,
    what:'The worker ends every attempt by running the gate itself; the dispatcher re-runs it as the evidence of record and puts the report in the PR body.',
    cite:[[AF+'dispatch.py',266,'run_gate()'],[AF+'gate.py',98,'gate main()']],
    payload:{r:2.6, dur:4.6, kind:'control'} },
  { id:'p6', kind:'control', from:'gate', to:'gpulock',
    label:'flock host lock',
    pts:[[19,7],[20.4,8.8],[24.8,10.0],[25.6,12.0],[31.2,12.0],[33,14]], lt:0.75, ldy:-10,
    what:'Checks marked exclusive wait on a host-wide flock so the GPU is single-tenant across factories.',
    cite:[[AF+'gate.py',58,'run(): flock + timeout']],
    payload:null },
  { id:'p7', kind:'control', from:'dispatch', to:'reviewer',
    label:'review → VERDICT',
    pts:[[15.1,7.5],[16.6,11.0],[25.0,11.0],[25.0,20.6],[31,20.6],[33,18.4]], lt:0.6, ldy:0,
    what:'The review prompt carries the diff, the ticket, and the gate report; the answer must end VERDICT: APPROVE or REVISE. REVISE goes back to the worker with the findings, up to review_rounds times.',
    cite:[[AF+'dispatch.py',318,'review()']],
    payload:{r:3, dur:5.4, kind:'control'} },
  { id:'p8', kind:'data', from:'reviewer', to:'prs',
    label:'APPROVE → label',
    pts:[[33,18.4],[30.8,16.2],[26.0,15.4],[20.8,12.2],[20.8,3.2],[17.4,3.2],[15.1,1.3]], lt:0.8, ldy:-10,
    what:'The verdict is made durable on the PR as a label, not in memory; red CI later removes it.',
    cite:[[AF+'dispatch.py',561,'approve_pr()']],
    payload:{r:2.6, dur:5, kind:'data'} },
  { id:'p9', kind:'control', from:'dispatch', to:'prs',
    label:'merge stage (4 conditions)',
    pts:[[15.1,7.5],[15.1,1.3]], lt:0.5, ldy:-10,
    what:'At most one PR per pass. Behind main → rebase, re-gate on this host, force-push, merge next pass. A human blocks any merge by requesting changes.',
    cite:[[AF+'dispatch.py',632,'land_pass()']],
    payload:{r:3, dur:4.2, kind:'control'} },
  { id:'p11', kind:'control', from:'triage', to:'llm',
    label:'chat completion',
    pts:[[11.2,7],[11.2,5.3],[26.2,5.3],[26.2,12.9],[28.6,14]], lt:0.2, ldy:-10,
    what:'OpenAI-style JSON request to the host-configured endpoint; the model returns one of four decisions.',
    cite:[[AF+'triage.py',108,'call_llm()']],
    payload:{r:2.6, dur:5.6, kind:'control'} },
  { id:'p12', kind:'data', from:'triage', to:'issues',
    label:'triage → label',
    pts:[[11.2,7],[6.6,5.0],[6.6,2.6],[8.6,1.3]], lt:0.5, ldy:-10,
    what:'Deterministic lint first (short body, missing acceptance criteria → needs-info with a question), then the model’s label. wontfix is proposed as a comment, never applied.',
    cite:[[AF+'triage.py',203,'decision → label']],
    payload:{r:2.6, dur:4.2, kind:'data'} },
  { id:'p13', kind:'data', from:'dispatch', to:'state',
    label:'events.jsonl',
    pts:[[15.1,7.5],[12.6,11.9],[11,13.3]], lt:0.5, ldy:-10,
    what:'claimed, attempt (exit, gate result, seconds), pr-opened, review, approved, refreshed, merged, escalate, upstream-sync — one row each.',
    cite:[[AF+'dispatch.py',204,'record()']],
    payload:{r:2.4, dur:3.6, kind:'data'} },
  { id:'p14', kind:'dep', from:'hostcfg', to:'config',
    label:'host layer merge',
    pts:[[1.7,19.9],[-0.3,19.0],[-0.3,10],[4.4,7],[7.9,7]], lt:0.5, ldy:0,
    what:'The host layer supplies only host-owned tables; the committed file always wins and may never be overridden on gate checks or upstream.',
    cite:[[AF+'config.py',218,'load()'],[AF+'config.py',49,'HOST_TABLES']],
    payload:null },
  { id:'p15', kind:'control', from:'district', to:'hostcfg',
    label:'register · port · dedupe',
    pts:[[1.7,16.9],[1.7,19.9]], lt:0.5, ldy:-6,
    what:'Registry entry first (path + port), then host-owned keys lifted out of the repo file and promoted to [defaults] when every factory agrees.',
    cite:[[DC+'add.py',325,'port allocation'],[DC+'host.py',83,'dedupe()']],
    payload:{r:2.4, dur:3.2, kind:'control'} },
  { id:'p16', kind:'control', from:'district', to:'systemd',
    label:'install units · failure cap',
    pts:[[1.7,16.9],[1.7,13.1]], lt:0.35, ldy:4,
    what:'Re-renders every unit from the host file (policy env included); disables a timer after 10 consecutive failed passes; --reset re-enables only after a hand-run pass succeeds.',
    cite:[[DC+'apply.py',143,'repo_pass()'],[DC+'apply.py',181,'cap → disable']],
    payload:{r:2.4, dur:3.2, kind:'control'} },
  { id:'p17', kind:'control', from:'district', to:'install',
    label:'upgrade engine',
    pts:[[1.7,16.9],[3.6,18.8],[3.6,21.4],[1.7,21.8]], lt:0.55, ldy:12,
    what:'Refuses a dirty engine checkout; disables timers, waits for running passes, reinstalls the uv tool, restarts dashboards, re-enables timers — restoring them on any failure.',
    cite:[[DC+'apply.py',92,'upgrade()']],
    payload:null },
  { id:'p18', kind:'data', from:'dashboard', to:'district',
    label:'dashboard --json',
    pts:[[22.8,7.2],[22.8,11.7],[6.2,11.7],[3.6,15.8],[1.7,16.9]], lt:0.55, ldy:-10,
    what:'The only thing District reads from a factory. A nonzero exit, malformed JSON, or snapshot errors mark the factory unhealthy — never “zero escalations”.',
    cite:[[DC+'status.py',21,'snapshot()'],[DC+'status.py',49,'row(): health']],
    payload:{r:2.6, dur:5.8, kind:'data'} },
"""


# ---------------------------------------------------------------- helpers


def n(x: int | float | None, suffix: str = "") -> str:
    return "?" if x is None else f"{x:,}{suffix}"


def pct(x: float | None) -> str:
    return "-" if x is None else f"{round(100 * x)}%"


def dot(level: str) -> str:
    return f'<span class="health {level}"></span>'


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def block_id(slug: str) -> str:
    return "f_" + re.sub(r"[^a-z0-9]", "", slug.rsplit("/", 1)[-1].lower())


def height(loc: int, max_loc: int) -> float:
    return round(12 + 108 * sqrt(loc / max_loc), 1) if max_loc else 12.0


def traffic_pair(t: dict | str | None) -> str:
    return "n/a" if not isinstance(t, dict) else f"{n(t['count'])} ({n(t['uniques'])} unique)"


# ---------------------------------------------------------------- factory blocks


def factory_kpis(slug: str, e: dict) -> list[list[str]]:
    m, snap, table = e["metrics"] or {}, e["snap"], e["table"]
    rows = [["health", dot(e["health"]) + e["health"] + (" — " + "; ".join(e["reasons"]) if e["reasons"] else "")]]
    if snap:
        d = snap["dispatcher"]
        finished = [r["result"] for r in d["runs"] if r["result"] != "running"]
        timer = "DISABLED by District" if table.get("disabled_at") else ("timer active" if d["timer"]["active"] else "timer INACTIVE")
        rows.append(["engine", f"{snap.get('version', '?')} · {timer} · last pass {finished[-1] if finished else '-'} · "
                     f"{d['consecutive_failures']} consecutive failures"])
    else:
        rows.append(["engine", "dashboard unreachable: " + esc(e["error"] or "?")])
    if m:
        langs = " · ".join(f"{k} {n(v)}" for k, v in list(m["languages"].items())[:3])
        rows += [
            ["codebase", f"{n(m['loc'])} LOC · {n(m['files'])} files · {langs} · tests {n(m['test_loc'])} in {n(m['test_files'])} files"],
            ["people", f"{n(m['contributors'])} contributors · top 3 authors = {pct(m['top3_share'])} of {n(m['commits'])} commits"],
            ["velocity", f"{n(m['commits_30d'])} commits / 30d ({n(m['commits_7d'])} / 7d) · {n(m['merged_prs_30d'])} PRs merged / 30d, "
                         f"{n(m['agent_prs_30d'])} by agents"],
        ]
    labels = ", ".join(f"{v} {k}" for k, v in (m.get("open_by_label") or {}).items())
    work = f"{n(m.get('open_issues'))} open issues" + (f" ({labels})" if labels else "") + f" · {n(m.get('open_prs'))} open PRs"
    if snap:
        work += f" · {len(snap['tickets'])} tickets tracked"
    rows.append(["work", work])
    if snap:
        sm = snap["metrics"]
        quality = ("no gate history yet" if sm.get("first_pass") is None else f"first-gate pass {pct(sm['first_pass'])} · bounce {pct(sm['bounce_rate'])}")
        rows.append(["quality", f"{quality} · {n(sm.get('escalations'))} escalations all-time · {n(m.get('open_bugs'))} open bug-labelled"])
    if m:
        med = "" if m["median_days_to_close"] is None else f" · median {m['median_days_to_close']} days"
        rows += [
            ["resolution", f"{n(m['closed_issues_30d'])} issues closed / 30d ({n(m['closed_bugs_30d'])} bug-labelled){med}"],
            ["traffic 14d", f"{traffic_pair(m['traffic']['clones'])} clones · {traffic_pair(m['traffic']['views'])} views · "
                            f"{n(m['stars'])} stars · {n(m['forks'])} forks · {n(m['watchers'])} watchers"],
            ["collected", m["collected_at"]],
        ]
    else:
        rows.append(["metrics", "not collected yet — district metrics --refresh"])
    if snap and snap["config"].get("upstream"):
        up = snap.get("upstream") or {}
        parked = f" · parked on #{up['blocker']['number']}" if up.get("blocker") else ""
        rows.append(["upstream", f"{esc(up.get('repo') or '?')}: {n(up.get('ahead'))} ahead · {n(up.get('behind'))} behind{parked}"])
    port = table.get("dashboard", {}).get("port")
    if port:
        rows.append(["dashboard", f'<a href="http://127.0.0.1:{port}/" target="_blank" rel="noopener">127.0.0.1:{port}</a>'])
    return rows


def factory_block(i: int, slug: str, e: dict, max_loc: int) -> dict:
    m, snap = e["metrics"] or {}, e["snap"]
    loc = m.get("loc", 0)
    w = round(2.4 + 1.2 * sqrt(loc / max_loc), 2) if max_loc else 2.4
    h = height(loc, max_loc)
    gx, gy = round(FACTORY_X0 + FACTORY_STEP * i, 2), round(FACTORY_Y + (3.6 - w) / 2, 2)
    fork = bool(snap and snap["config"].get("upstream"))
    checks = [c for c in (snap or {}).get("config", {}).get("gate_checks", []) if c not in ("conflict-markers", "leak-scan")]
    exclusive = set((snap or {}).get("config", {}).get("exclusive_checks", []))
    gate = " · ".join(c + ("*" if c in exclusive else "") for c in checks) or "?"
    ref = f"{slug}@{m['head']}/.factory.toml" if m.get("head") else f"{slug}/blob/main/.factory.toml"
    if m:
        top = next(iter(m["languages"].items()), ("-", 0))
        size = f"{n(m['loc'])} tracked lines ({top[0]} {n(top[1])}), {n(m['contributors'])} contributors, {n(m['commits_30d'])} commits in 30 days"
    else:
        size = "metrics not collected yet"
    blurb = f"{'Fork' if fork else 'Net-new repository'}: {size}. Gate: {gate}{' (* exclusive)' if exclusive else ''}. "
    blurb += f"{e['health'].capitalize()}" + (": " + "; ".join(esc(r) for r in e["reasons"]) + "." if e["reasons"] else ".")
    return {
        "id": block_id(slug), "slug": slug, "name": slug.rsplit("/", 1)[-1], "cat": "factory",
        "gx": gx, "gy": gy, "w": w, "d": w, "h": h, "kind": "tower3" if h >= 60 else "hall", "ring": fork,
        "health": e["health"], "blurb": blurb, "kpis": factory_kpis(slug, e),
        "files": [[ref, 1, f"[[gate.check]] {gate}"]] + ([[ref, 1, f"upstream = \"{snap['config']['upstream']}\""]] if fork else []),
        "conn": "Fed by its fork parent; dispatched by its own timer." if fork else "Net-new: no upstream sync loop.",
    }


def factory_paths(b: dict) -> list[dict]:
    cx, cy = round(b["gx"] + b["w"] / 2, 2), round(b["gy"] + b["d"] / 2, 2)
    paths = [{
        "id": "p2_" + b["id"], "kind": "control", "from": "dispatch", "to": b["id"], "label": "worktree agent/<n>",
        "pts": [DISPATCH, [cx, 11.7], [cx, cy]], "lt": 0.5, "ldy": -10,
        "what": f"The claimed ticket gets a git worktree inside the {b['name']} checkout; the worker only ever pushes agent/<n>. Only the merge stage moves main.",
        "cite": [[AF + "dispatch.py", 815, "process_ticket()"]], "payload": {"r": 2.6, "dur": round(4.4 + 0.4 * (cx / 6.5), 1), "kind": "control"},
    }]
    if b["ring"]:
        paths.append({
            "id": "p10_" + b["id"], "kind": "build", "from": "upstream", "to": b["id"], "label": "upstream sync",
            "pts": [UPSTREAM, [21.6, 4.2], [17.3, 4.2], [17.3, 12.2], [cx - 1.0, 15.3], [cx, cy]], "lt": 0.55, "ldy": 0,
            "what": "When upstream moved, a detached worktree merges it into the fork’s main, runs the gate (leak scan skipped: upstream is public) and pushes. "
                    "Conflict or gate failure opens one ready-for-human issue and parks the stage until it closes.",
            "cite": [[AF + "dispatch.py", 469, "sync_pass()"]], "payload": {"r": 3, "dur": 6.5, "kind": "build"},
        })
    return paths


# ---------------------------------------------------------------- KPI strip


def kpis(fleet: dict) -> list[list[str]]:
    es = list(fleet.values())
    ms = [e["metrics"] for e in es if e["metrics"]]
    snaps = [e["snap"] for e in es if e["snap"]]
    tally = {lvl: sum(e["health"] == lvl for e in es) for lvl in ("healthy", "attention", "failing")}
    versions = sorted({s.get("version", "?") for s in snaps}) or ["?"]
    fails = sum(s["dispatcher"]["consecutive_failures"] for s in snaps)
    human = sum((m.get("open_by_label") or {}).get("ready-for-human", 0) for m in ms)
    clones = [m["traffic"]["clones"] for m in ms if isinstance(m["traffic"]["clones"], dict)]
    views = [m["traffic"]["views"] for m in ms if isinstance(m["traffic"]["views"], dict)]
    na = len(ms) - min(len(clones), len(views))
    total = lambda key: sum(m[key] for m in ms)  # noqa: E731
    return [
        ["factories", str(len(es)), " · ".join(s.rsplit("/", 1)[-1] for s in fleet) or "none registered"],
        ["health", f"{dot('healthy')}{tally['healthy']} {dot('attention')}{tally['attention']} {dot('failing')}{tally['failing']}",
         "healthy · attention · failing"],
        ["code under management", n(total("loc")), f"tracked lines · {n(total('files'))} files · {n(total('contributors'))} contributors"
         + (f" · {len(es) - len(ms)} not collected" if len(ms) < len(es) else "")],
        ["engine", " / ".join(versions), f"on {len(snaps)} of {len(es)} factories · {fails} failed passes"],
        ["velocity 30d", n(total("commits_30d")), f"commits · {n(total('merged_prs_30d'))} PRs merged, {n(total('agent_prs_30d'))} by agents"],
        ["open work", f"{n(total('open_issues'))} / {n(total('open_prs'))}", f"issues / PRs · {human} waiting on a human"],
        ["defects", n(total("open_bugs")), f"open bug-labelled · {n(total('closed_bugs_30d'))} closed 30d · {n(total('closed_issues_30d'))} issues closed 30d"],
        ["traffic 14d", n(sum(c["count"] for c in clones)),
         f"clones ({n(sum(c['uniques'] for c in clones))} unique) · {n(sum(v['count'] for v in views))} views · "
         f"{n(total('stars'))} stars · {n(total('forks'))} forks" + (f" · {na} unavailable" if na else "")],
    ]


# ---------------------------------------------------------------- DATA + page


def js(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


def data(fleet: dict) -> str:
    """The DATA JS block: constants, B (blocks), PLATES, P (paths), KPIS — from a `status.fleet()` dict."""
    max_loc = max((e["metrics"] or {}).get("loc", 0) for e in fleet.values()) if fleet else 0
    blocks = [factory_block(i, slug, e, max_loc) for i, (slug, e) in enumerate(fleet.items())]
    paths = [p for b in blocks for p in factory_paths(b)]
    x1 = max(24.5, round(max((b["gx"] + b["w"] for b in blocks), default=0) + 1.4, 2))
    plates = [
        {"name": "GITHUB", "cat": "ext", "x0": 6, "y0": -0.8, "x1": 25, "y1": 3.4},
        {"name": "HOST RUNTIME", "cat": "runtime", "x0": 26.5, "y0": 12, "x1": 35.5, "y1": 21.5},
        {"name": "AGENT-FACTORY ENGINE", "cat": "engine", "x0": 6, "y0": 5.2, "x1": 25, "y1": 11.4},
        {"name": "STATE", "cat": "state", "x0": 8.2, "y0": 12, "x1": 14, "y1": 14.6},
        {"name": "DISTRICT", "cat": "district", "x0": -0.8, "y0": 11.6, "x1": 4.6, "y1": 23},
        {"name": "FACTORIES", "cat": "factory", "x0": 7, "y0": 15, "x1": x1, "y1": 22.4},
    ]
    ground = {"x0": -1.8, "y0": -1.8, "x1": max(36.5, x1 + 1.2), "y1": 24.0}
    return (
        HEAD
        + f"\nconst G = {js(ground)};\n"
        + "\n/* ---- Buildings ---- */\nconst B = [\n" + STATIC_BLOCKS
        + "\n  // Factories (front, LOC-scaled)\n" + "".join(f"  {js(b)},\n" for b in blocks) + "];\n"
        + "\n/* ---- Districts (ground plates) ---- */\nconst PLATES = [\n" + "".join(f"  {js(p)},\n" for p in plates) + "];\n"
        + f"\n/* ---- Paths ---- */\nconst AF = {js(AF)};\nconst DC = {js(DC)};\nconst P = [\n" + STATIC_PATHS
        + "".join(f"  {js(p)},\n" for p in paths) + "];\n"
        + "\n/* ---- Exec KPI strip ---- */\nconst KPIS = [\n" + "".join(f"  {js(k)},\n" for k in kpis(fleet)) + "];\n"
    )


def page(fleet: dict, at: datetime | None = None) -> str:
    at = at or datetime.now(timezone.utc)
    stamp = at.strftime("%Y-%m-%d %H:%MZ")
    owners = {s.split("/", 1)[0] for s in fleet}
    owner = next(iter(owners)) if len(owners) == 1 else "District"
    versions = sorted({e["snap"].get("version", "?") for e in fleet.values() if e["snap"]}) or ["?"]
    max_loc = max((e["metrics"] or {}).get("loc", 0) for e in fleet.values()) if fleet else 0
    shas = ", ".join(f"{s.rsplit('/', 1)[-1]} @ {(e['metrics'] or {}).get('head', '?')}" for s, e in fleet.items())
    eyebrow = f"<b>{esc(owner)} District</b> · {len(fleet)} factories · engine {' / '.join(versions)} · snapshot {stamp}"
    footer = (
        f"Derived from the local checkouts, the metrics cache, and live dashboards at {stamp}: {shas}, "
        f"agent-factory @ {AF_SHA}, district @ {DC_SHA}. Factory heights use tracked line counts (h = 12 + 108·√(LOC/{n(max_loc)})); "
        "engine blocks are sized at the cited commit; GitHub pads, host runtime, systemd, and the host file are not LOC-scaled. "
        "Traffic is GitHub’s 14-day window; velocity is a 30-day window. Health is computed from each dashboard snapshot, not from factory notifications."
    )
    return TEMPLATE.read_text().replace("@@DATA@@", data(fleet)).replace("@@EYEBROW@@", eyebrow).replace("@@FOOTER@@", footer)
