#!/usr/bin/env python3
"""Build district-atlas.html from the codebase-atlas reference engine.

The engine (projection, camera, legend, inspector) is copied verbatim from
the skill template; only the DATA blocks, header, footer, and the few
id-specific engine hooks are replaced. Numbers come from the 2026-09-04
fleet scan recorded in DATA below — never edit them by hand without a rescan.

Run: python3 atlas/build_atlas.py  → atlas/district-atlas.html
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = Path.home() / ".omp/agent/skills/codebase-atlas/references/atlas-template.html"
OUT = Path(__file__).with_name("district-atlas.html")

SCAN = "2026-09-04 01:42Z"

# ---------------------------------------------------------------- HTML chrome

TITLE = "<title>District Atlas</title>"

HEADER = """<header class="site">
  <div class="wrap">
    <div class="eyebrow"><b>mikeroySoft District</b> · 3 factories · engine 0.2.0 · snapshot """ + SCAN + """</div>
    <h1>District Atlas <span class="dim">// every factory in one district, one map</span></h1>
    <p class="thesis">
      Every tall building is a managed repository — a <em>factory</em> — with height proportional to its
      lines of code. The low-rise blocks are the shared engine (agent-factory) and the District control
      tower that keeps every factory on the same rails. Roads are real control and data paths, cited to
      the line that implements them. Click a factory for its health rollup and delivery metrics.
    </p>
    <div class="kpis" id="kpis"></div>
  </div>
</header>"""

KPI_CSS = """  .kpis { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0 0; }
  .kpi { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px 12px; min-width: 128px; }
  .kpi .k { font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--faint); }
  .kpi .v { font-size: 20px; font-weight: 600; color: var(--ink); line-height: 1.2; margin-top: 2px; }
  .kpi .s { font-size: 11px; color: var(--muted); }
  .health { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: 1px; }
  .health.healthy { background: #4CBB6C; } .health.attention { background: #E3A83B; } .health.failing { background: #E8493E; }
  #inspector table.kv { width: 100%; border-collapse: collapse; font-size: 12px; margin: 10px 0 4px; }
  #inspector table.kv td { padding: 3px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
  #inspector table.kv td:first-child { color: var(--muted); width: 38%; font-family: var(--mono); font-size: 11px; }
"""

LEGEND_NOTE = '<p class="legend-note">Moving dots are payloads: timer ticks, claimed tickets, gate reports, review verdicts, merges, and dashboard snapshots in transit.</p>'
HINT = '<div class="scene-hint">hover a road or building = isolate its connections · click = inspect · drag = pan · scroll = zoom · factory height ∝ LOC · ● health: green healthy · amber attention · red failing</div>'

FOOTER = """  <footer class="site">
    <div>Derived from the local checkouts and live dashboards at """ + SCAN + """: rocm-cli @ edb4ebf, gpuflo @ 08128c2, rocm-app @ 8966f8f, agent-factory @ 8f9baad, district @ f413e8c. Factory and engine heights use tracked line counts (h = 12 + 108·√(LOC/213,400)); GitHub pads, host runtime, systemd, and the host file are not LOC-scaled. Traffic is GitHub’s 14-day window; velocity is a 30-day window. Health is computed from the dashboard snapshot, not from factory notifications.</div>
  </footer>"""

# ---------------------------------------------------------------- DATA blocks

DATA = r"""'use strict';
const GH = 'https://github.com/';
const SHA = { af: '8f9baad', dc: 'f413e8c', rc: 'edb4ebf', gf: '08128c2', ra: '8966f8f' };
const CAT = {
  factory:  { name: 'Factories (managed repos)',        color: '#E8493E' },
  engine:   { name: 'agent-factory engine 0.2.0',       color: '#E3A83B' },
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

/* ---- Buildings: gx,gy grid position; w,d footprint; h height px; kind = silhouette ---- */
const B = [
  // GitHub (back row)
  { id:'issues', name:'Issues & labels', cat:'ext', gx:7.5, gy:0.3, w:2.2, d:2, h:8, kind:'pad',
    blurb:'The ticket queue for every factory. Six fixed labels carry state: needs-triage → ready-for-agent (or needs-info) → ready-for-human on escalation; factory-approved marks a reviewed PR. Open across the fleet now: 3 needs-triage, 6 ready-for-human.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',21,'the six label constants'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',150,'frontier(): ready-for-agent, unassigned, unblocked'] ],
    conn:'Claimed and escalated by the dispatcher; labelled by triage.' },
  { id:'prs', name:'Pull requests + CI', cat:'ext', gx:14, gy:0.3, w:2.2, d:2, h:8, kind:'pad',
    blurb:'One PR per ticket on branch agent/<n>. A merge needs four independent pieces of evidence: gate PASS in the body, the factory-approved label, green GitHub checks, and a head that already contains main. Fleet now: 8 open PRs; 14 merged in 30 days, 4 of them by agents.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',632,'land_pass(): the four-way merge precondition'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',561,'approve_pr(): durable APPROVE as a label'] ],
    conn:'Written by the reviewer step; merged by the merge stage.' },
  { id:'upstream', name:'ROCm/rocm-cli (fork parent)', cat:'ext', gx:20.5, gy:0.3, w:2.2, d:2, h:8, kind:'pad',
    blurb:'The only fork in the district. rocm-cli tracks it: every pass fetches upstream main and, when it moved, merges it into the fork’s main under the host gate. Today: 40 ahead, 2 behind, parked on a merge-conflict issue (#31) waiting for a human.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',469,'sync_pass(): one merge per pass, conflicts open a ready-for-human issue'], ['mikeroySoft/rocm-cli@edb4ebf/.factory.toml',4,'[repo].upstream = "upstream"'] ],
    conn:'Feeds rocm-cli only; gpuflo and rocm-app are net-new.' },

  // Host runtime (right)
  { id:'llm', name:'Local triage model', cat:'runtime', gx:27.5, gy:13, w:2.2, d:2, h:8, kind:'pad',
    blurb:'An OpenAI-compatible chat endpoint on this host running a 35B open model. Only triage talks to it: a deterministic lint first (body ≥ 80 chars, acceptance criteria present), then one JSON completion decides the label. wontfix is only ever proposed.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/triage.py',108,'call_llm(): one chat completion per issue'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/triage.py',203,'decision → label'] ],
    conn:'Endpoint and model name come from the host file, not the repos.' },
  { id:'gpulock', name:'Host lock (exclusive checks)', cat:'runtime', gx:32, gy:13.2, w:2, d:1.6, h:10, kind:'gate',
    blurb:'One GPU, many worktrees. Gate checks marked exclusive serialise on a host-wide flock so two factories never run clippy/tests on the GPU at once; every check has a timeout so a wedged run fails instead of holding the lock.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/gate.py',58,'run(): flock + timeout per check'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',50,'gate.lock is host-owned'] ],
    conn:'Taken by the gate; rocm-cli and gpuflo mark clippy/tests exclusive.' },
  { id:'worker', name:'Worker agents (omp · droid)', cat:'runtime', gx:27.5, gy:17, w:2.6, d:2.6, h:26, kind:'plant',
    blurb:'The coding agents. The dispatcher spawns the configured CLI in the ticket’s worktree with the issue, its comments, standing rules, the repo’s lessons file, and — on retries — the last gate report or review findings. Ticket label picks the worker: chore → droid, else omp. They run unsandboxed on this host today (PRD §6a).',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',217,'run_worker(): argv, worktree, log'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',37,'DEFAULT_WORKER / DEFAULT_CHORE_WORKER'] ],
    conn:'Spawned per attempt; finishes by running factory gate.' },
  { id:'reviewer', name:'Reviewer (codex exec)', cat:'runtime', gx:32, gy:17.4, w:2, d:2, h:18, kind:'kiosk',
    blurb:'A second model reviews the diff against the issue and the gate report. Every finding must cite path:line; the output ends VERDICT: APPROVE or REVISE. REVISE bounces once to the worker, then escalates.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',318,'review(): two-axis prompt, verdict parse'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',39,'DEFAULT_REVIEWER'] ],
    conn:'Its APPROVE becomes the factory-approved label on the PR.' },

  // Engine (middle band) — LOC-scaled
  { id:'config', name:'config.py (host layer)', cat:'engine', gx:6.8, gy:6, w:2.2, d:2, h:16, kind:'slab',
    blurb:'267 lines: the single source of every repo-specific value. Loads the committed .factory.toml over the host file: [defaults] < [repo."owner/name"] < repo. Only host-owned tables (triage, workers, review, install, dashboard.port, gate.lock) may come from the host; gate checks and upstream never do, so a clone elsewhere runs the same gate.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',218,'load(): layered merge'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',49,'HOST_TABLES / HOST_KEYS'] ],
    conn:'Read by every command; reads the host file District writes.' },
  { id:'triage', name:'triage.py', cat:'engine', gx:10.2, gy:6, w:2, d:2, h:16, kind:'hall',
    blurb:'289 lines. Labels every needs-triage issue: lint, then the local model. Outcomes: ready-for-agent with an agent brief, needs-info with the question, ready-for-human, or a wontfix proposal comment.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/triage.py',247,'main()'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/triage.py',30,'endpoint from host config (FACTORY_LLM_URL override)'] ],
    conn:'Talks to the model and to Issues.' },
  { id:'dispatch', name:'dispatch.py', cat:'engine', gx:13.6, gy:6, w:3, d:3, h:20, kind:'hall',
    blurb:'981 lines, the pipeline. One stateless pass: upstream sync → merge stage (≤1 PR) → claim up to max_active tickets → worker → gate → PR → review → bounce or escalate. Every transition is appended to .factory/events.jsonl with its evidence pointers.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',929,'main(): one pass'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',815,'process_ticket(): attempts, gate, review'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',289,'escalate(): ready-for-human + handoff comment'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',204,'record(): the audit trail'] ],
    conn:'Runs inside each factory checkout, fired by that factory’s timer.' },
  { id:'gate', name:'gate.py', cat:'engine', gx:18, gy:6, w:2, d:2, h:15, kind:'gate',
    blurb:'163 lines. Deterministic evidence: conflict-markers, the repo’s [[gate.check]] list in order, then a leak scan of added lines. Writes a Markdown report the reviewer and the PR body both carry.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/gate.py',98,'main()'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',266,'run_gate(): the report of record'] ],
    conn:'Run by workers, re-run by the dispatcher.' },
  { id:'dashboard', name:'dashboard.py + html', cat:'engine', gx:21.5, gy:6, w:2.6, d:2.4, h:24, kind:'sensor',
    blurb:'2,716 lines (1,012 py · 1,158 html · 546 atlas). One HTTP server per factory: tickets by stage, gate reports, worker logs, journal heartbeat, upstream drift, and an action inbox. --json is the contract District reads: version, metrics (first-gate pass, bounce), consecutive failed passes.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/dashboard.py',738,'snapshot()'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dashboard.py',595,'metrics(): fleet KPIs'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/dashboard.py',554,'consecutive_failures()'] ],
    conn:'Three instances run as persistent user services; District polls them.' },
  { id:'onboard', name:'onboard.py (init · doctor · install)', cat:'engine', gx:10.2, gy:9.2, w:2, d:1.6, h:16, kind:'hall',
    blurb:'335 lines. init writes the config, labels, issue template; doctor checks tools, auth, remotes, drift and speaks --json; install renders and converges the systemd units with defaults from the host file.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/onboard.py',245,'units(): timer + service + dashboard'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/onboard.py',143,'doctor(): drift checks'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/onboard.py',276,'install(): convergent'] ],
    conn:'Called by district add and district apply.' },
  { id:'learn', name:'learn.py · stats.py', cat:'engine', gx:18, gy:9.2, w:2, d:1.6, h:16, kind:'kiosk',
    blurb:'291 lines. learn distils the last N tickets’ trails into ≤10 repo lessons the workers carry; stats prints attempts, review rounds, hours-to-merge per ticket.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/learn.py',88,'main()'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/stats.py',165,'main()'] ],
    conn:'Operator-run; not on the timer.' },

  // Factory state on disk
  { id:'state', name:'.factory/ (events · worktrees · logs)', cat:'state', gx:9, gy:12.6, w:4, d:1.4, h:8, kind:'slab',
    blurb:'Gitignored per-repo state: events.jsonl audit trail, wt-<n> worktrees kept for forensics, per-attempt logs, per-ticket locks. Now: rocm-cli 6 worktrees / 12 events; rocm-app 1 / 3; gpuflo 1 / 0.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/dispatch.py',204,'record() appends events.jsonl'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',91,'Config.factory: <root>/.factory'] ],
    conn:'Written by the dispatcher; read by the dashboard and factory learn.' },

  // District (front-left)
  { id:'systemd', name:'systemd user timers ×3', cat:'district', gx:0.4, gy:12.2, w:2.6, d:1.8, h:14, kind:'sensor',
    blurb:'Per factory: a 10-minute timer firing a oneshot dispatch service, plus a persistent dashboard service. Units carry the operator PATH and the supply-chain policy env (npm min-release-age 1 day, uv exclude-newer) so workers never install a package younger than the policy.',
    files:[ ['mikeroySoft/agent-factory@8f9baad/agent_factory/onboard.py',252,'unit templates'], ['mikeroySoft/district@f413e8c/district/apply.py',23,'POLICY_ENV'] ],
    conn:'Rendered by factory install; converged and capped by district apply.' },
  { id:'district', name:'district (add · apply · status · rm)', cat:'district', gx:0.6, gy:15.8, w:2.2, d:2.2, h:21, kind:'mast',
    blurb:'1,468 lines of Python, 24 tests. The control tower: onboards a repo in one command (fork detection, gate proposal, port allocation), converges every factory to the host file, upgrades the engine fleet-wide with stop → wait → reinstall → restore, disables a timer after 10 consecutive failed passes, and prints the fleet table with a health exit code. Never commits to a factory; never imports the engine.',
    files:[ ['mikeroySoft/district@f413e8c/district/add.py',299,'add: onboard / adopt'], ['mikeroySoft/district@f413e8c/district/apply.py',92,'upgrade(): stop → wait → reinstall'], ['mikeroySoft/district@f413e8c/district/apply.py',181,'failure cap → timer disabled'], ['mikeroySoft/district@f413e8c/district/status.py',49,'row(): health per factory'] ],
    conn:'Writes the host file and drives factory; reads dashboard --json.' },
  { id:'hostcfg', name:'Host file (registry + defaults)', cat:'district', gx:0.4, gy:19.2, w:2.6, d:1.4, h:8, kind:'slab',
    blurb:'~/.config/agent-factory/config.toml — the registry is the config. Per factory only path and dashboard port; everything the fleet agrees on (triage endpoint, install cadence, bind host, policy env) is promoted into [defaults]. Nothing here is ever committed to a repo.',
    files:[ ['mikeroySoft/district@f413e8c/district/host.py',83,'dedupe(): promote agreed values'], ['mikeroySoft/district@f413e8c/district/host.py',75,'next_port()'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/config.py',218,'read by config.load'] ],
    conn:'Written by district; read by every factory command.' },
  { id:'install', name:'Engine install (uv tool, one snapshot)', cat:'district', gx:0.4, gy:21.2, w:2.6, d:1.2, h:12, kind:'depot',
    blurb:'One frozen agent-factory install on the host, version 0.2.0, shared by all three factories today. Every unit’s ExecStart points at its python. Upgrading is explicit and fleet-wide; per-factory engine pinning is the next surface (PRD §5.7).',
    files:[ ['mikeroySoft/district@f413e8c/district/apply.py',101,'uv tool install --reinstall'], ['mikeroySoft/agent-factory@8f9baad/agent_factory/onboard.py',246,'ExecStart = sys.executable -m agent_factory'] ],
    conn:'Replaced by district apply --upgrade; executed by every timer tick.' },

  // Factories (front, LOC-scaled)
  { id:'rocmcli', name:'rocm-cli', cat:'factory', gx:8, gy:16.2, w:3.6, d:3.6, h:120, kind:'tower3', ring:true,
    health:'attention',
    blurb:'The largest factory and the only fork: 213,400 tracked lines (168k Rust, 33k in tests), 12 contributors, 135 commits in 30 days. Gate: clippy, tests, smoke — all exclusive on the GPU. Attention: 5 tickets waiting on a human, upstream sync parked on a merge conflict, review bounce rate 50%.',
    kpis:[ ['health','<span class="health attention"></span>attention — 5 open ready-for-human · upstream parked #31 · bounce 50%'],
           ['engine','0.2.0 · timer active · last pass done · 0 consecutive failures'],
           ['codebase','213,400 LOC · 416 files · Rust 168,139 · tests 32,835 in 83 files'],
           ['people','12 contributors · top 3 authors = 62% of 258 commits'],
           ['velocity','135 commits / 30d (48 / 7d) · 9 PRs merged / 30d, 4 by agents'],
           ['work','8 open issues (3 needs-triage, 5 ready-for-human) · 6 open PRs · 14 tickets tracked'],
           ['quality','first-gate pass 100% · bounce 50% · 12 escalations all-time · 0 open bug-labelled'],
           ['resolution','6 issues closed / 30d · median 0.1 days'],
           ['traffic 14d','1,267 clones (100 unique) · 2 views · 0 stars · 0 forks'],
           ['upstream','ROCm/rocm-cli: 40 ahead · 2 behind · parked on #31 (merge conflict)'] ],
    files:[ ['mikeroySoft/rocm-cli@edb4ebf/.factory.toml',11,'[[gate.check]] clippy · tests · smoke (exclusive)'], ['mikeroySoft/rocm-cli@edb4ebf/.factory.toml',4,'upstream = "upstream"'] ],
    conn:'Fed by the ROCm upstream; dispatched by its own timer.' },
  { id:'rocmapp', name:'rocm-app', cat:'factory', gx:14.5, gy:16.6, w:3, d:3, h:75, kind:'tower3',
    health:'attention',
    blurb:'A Tauri desktop app: 73,094 tracked lines (Rust 18.8k, TypeScript 12.4k, 20.8k config/lockfiles), one author, 44 commits. Onboarded into the district today in 5 seconds; its first ticket (#31) escalated on the first attempt, which is the one attention signal.',
    kpis:[ ['health','<span class="health attention"></span>attention — 1 open ready-for-human (#31, escalated attempt 1)'],
           ['engine','0.2.0 · timer active · last pass done · 0 consecutive failures'],
           ['codebase','73,094 LOC · 188 files · Rust 18,843 · TS 12,427 · tests 14,619 in 66 files'],
           ['people','1 contributor · 44 commits'],
           ['velocity','2 commits / 30d · 0 PRs merged / 30d'],
           ['work','2 open issues (1 ready-for-human) · 1 open PR · 1 ticket tracked'],
           ['quality','no gate history yet · 1 escalation · 0 open bug-labelled'],
           ['resolution','1 issue closed / 30d · median 0.1 days'],
           ['traffic 14d','24 clones (18 unique) · 11 views (9 unique) · 0 stars · 1 fork'] ],
    files:[ ['mikeroySoft/rocm-app@8966f8f/.factory.toml',4,'[[gate.check]] typecheck · lint · fmt · tests (vitest run)'] ],
    conn:'Net-new: no upstream sync loop.' },
  { id:'gpuflo', name:'gpuflo', cat:'factory', gx:20.5, gy:17, w:2.6, d:2.6, h:52, kind:'hall',
    health:'healthy',
    blurb:'29,583 tracked lines (13.6k Rust), 2 contributors, 53 commits all within 30 days. Gate: fmt, clippy, tests, third-party notices. No tickets have run through it yet, so its factory KPIs are empty — healthy by absence of signals, not by evidence.',
    kpis:[ ['health','<span class="health healthy"></span>healthy — timer active, last pass done, nothing open'],
           ['engine','0.2.0 · timer active · last pass done · 0 consecutive failures'],
           ['codebase','29,583 LOC · 285 files · Rust 13,614 · 4,044 lines under test paths'],
           ['people','2 contributors · 53 commits'],
           ['velocity','53 commits / 30d (6 / 7d) · 5 PRs merged / 30d'],
           ['work','0 open issues · 1 open PR · 0 tickets tracked'],
           ['quality','no gate history yet · 0 escalations'],
           ['resolution','0 issues closed / 30d'],
           ['traffic 14d','147 clones (54 unique) · 37 views (8 unique) · 1 star · 1 fork'] ],
    files:[ ['mikeroySoft/gpuflo@08128c2/.factory.toml',11,'[[gate.check]] fmt · clippy · tests · notices'], ['mikeroySoft/gpuflo@08128c2/.factory.toml',4,'# Not a fork: no upstream sync.'] ],
    conn:'Net-new: no upstream sync loop.' },
];

/* ---- Districts (ground plates) ---- */
const PLATES = [
  { name:'GITHUB',               cat:'ext',      x0:6,    y0:-0.8, x1:25,   y1:3.4 },
  { name:'HOST RUNTIME',         cat:'runtime',  x0:26.5, y0:12,   x1:35.5, y1:21.5 },
  { name:'AGENT-FACTORY ENGINE', cat:'engine',   x0:6,    y0:5.2,  x1:25,   y1:11.4 },
  { name:'STATE',                cat:'state',    x0:8.2,  y0:12,   x1:14,   y1:14.6 },
  { name:'DISTRICT',             cat:'district', x0:-0.8, y0:11.6, x1:4.6,  y1:23 },
  { name:'FACTORIES',            cat:'factory',  x0:7,    y0:15,   x1:24.5, y1:22.4 },
];

/* ---- Paths ---- */
const AF = 'mikeroySoft/agent-factory@8f9baad/agent_factory/';
const DC = 'mikeroySoft/district@f413e8c/district/';
const P = [
  { id:'p1', kind:'control', from:'systemd', to:'dispatch',
    label:'10-min timer → dispatch',
    pts:[[1.7,13.1],[13.0,12.3],[15.1,7.5]], lt:0.5, ldy:-10,
    what:'factory-<repo>.timer activates factory-<repo>.service, which runs one stateless dispatch pass in the factory checkout with the unit’s PATH and policy env.',
    cite:[[AF+'onboard.py',252,'unit templates'],[AF+'dispatch.py',929,'main()']],
    payload:{r:3.2, dur:5, kind:'control'} },
  { id:'p2a', kind:'control', from:'dispatch', to:'rocmcli',
    label:'worktree agent/<n>',
    pts:[[15.1,7.5],[13.2,10.2],[7.6,12.4],[7.6,15.8],[9.8,18]], lt:0.62, ldy:0,
    what:'The claimed ticket gets a git worktree inside the factory checkout; the worker only ever pushes agent/<n>. Only the merge stage moves main.',
    cite:[[AF+'dispatch.py',815,'process_ticket()']],
    payload:{r:2.6, dur:4.4, kind:'control'} },
  { id:'p2b', kind:'control', from:'dispatch', to:'rocmapp',
    label:'',
    pts:[[15.1,7.5],[16,12.8],[16,18.1]], lt:0.5, ldy:-10,
    what:'Same claim → worktree → worker cycle in rocm-app.',
    cite:[[AF+'dispatch.py',815,'process_ticket()']],
    payload:{r:2.6, dur:4.8, kind:'control'} },
  { id:'p2c', kind:'control', from:'dispatch', to:'gpuflo',
    label:'',
    pts:[[15.1,7.5],[17.0,12.2],[21.8,13],[21.8,18.3]], lt:0.5, ldy:-10,
    what:'Same cycle in gpuflo; nothing has been claimed there yet.',
    cite:[[AF+'dispatch.py',815,'process_ticket()']],
    payload:{r:2.6, dur:5.2, kind:'control'} },
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
    what:'clippy/tests/smoke in rocm-cli and gpuflo are marked exclusive; they wait on a host-wide flock so the GPU is single-tenant across factories.',
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
  { id:'p10', kind:'build', from:'upstream', to:'rocmcli',
    label:'upstream sync',
    pts:[[21.6,1.3],[21.6,4.2],[17.3,4.2],[17.3,12.2],[12.8,15.3],[9.8,18]], lt:0.55, ldy:0,
    what:'When upstream moved, a detached worktree merges it into the fork’s main, runs the gate (leak scan skipped: upstream is public) and pushes. Conflict or gate failure opens one ready-for-human issue and parks the stage until it closes — the state rocm-cli is in now (#31).',
    cite:[[AF+'dispatch.py',469,'sync_pass()']],
    payload:{r:3, dur:6.5, kind:'build'} },
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
];
"""

# ---------------------------------------------------------------- engine hooks

GHLINK = """function ghLink(f) {
  // f[0] = 'owner/repo@sha/path' → github.com/owner/repo/blob/sha/path; display = repo/path
  const m = /^([^@]+)@([0-9a-f]+)\\/(.+)$/.exec(f[0]);
  const href = m ? GH + m[1] + '/blob/' + m[2] + '/' + m[3] + '#L' + f[1] : GH + f[0] + '#L' + f[1];
  const shown = m ? m[1].split('/')[1] + '/' + m[3] : f[0];
  return '<a href="' + href + '" target="_blank" rel="noopener">' + shown + ':' + f[1] + '</a>' +
         (f[2] ? ' <span style="color:var(--faint)">— ' + f[2] + '</span>' : '');
}"""

SELECT_EXTRA = """    '<p>' + b.blurb + '</p>' +
    (b.kpis ? '<table class="kv">' + b.kpis.map(k => '<tr><td>' + k[0] + '</td><td>' + k[1] + '</td></tr>').join('') + '</table>' : '') +"""

DEFAULT_INSPECTOR = """function defaultInspector() {
  document.getElementById('inspector').innerHTML =
    '<h2>Explainer</h2>' +
    '<h3>Follow one ticket through a factory</h3>' +
    '<ol>' +
    '<li>The factory’s timer fires every 10 minutes and runs one stateless <code>factory dispatch</code> pass — <code>onboard.py:252</code>.</li>' +
    '<li>The pass claims a <code>ready-for-agent</code> issue, assigns itself, and creates <code>.factory/wt-&lt;n&gt;</code> on branch <code>agent/&lt;n&gt;</code> — <code>dispatch.py:150</code>.</li>' +
    '<li>A worker agent runs in that worktree and finishes with <code>factory gate</code>; exclusive checks queue on the host lock — <code>dispatch.py:217</code>, <code>:266</code>.</li>' +
    '<li>The reviewer reads the diff and the gate report; <code>APPROVE</code> becomes the <code>factory-approved</code> label — <code>dispatch.py:318</code>, <code>:561</code>.</li>' +
    '<li>Next pass, the merge stage lands one PR when gate PASS, approval, green CI, and a head containing main all agree — <code>dispatch.py:632</code>. Anything else becomes <code>ready-for-human</code> — <code>:289</code>.</li>' +
    '</ol>' +
    '<h3>What District sees</h3>' +
    '<p>None of those notifications. District reads one document per factory — <code>factory dashboard --json</code> — and rolls it up: ' +
    '<span class="health failing"></span><b>failing</b> = timer inactive or District-disabled, last pass failed, snapshot errors, or any consecutive failed passes; ' +
    '<span class="health attention"></span><b>attention</b> = open <code>ready-for-human</code> tickets, a parked upstream sync, bounce rate over 33%, or doctor warnings; ' +
    '<span class="health healthy"></span><b>healthy</b> = none of the above.</p>' +
    '<p style="color:var(--muted)">Click a factory for its health signals, codebase size, velocity, defect resolution, and traffic. The lower panels list every citation.</p>';
}"""

KPI_STRIP = """
/* ================= Exec KPI strip ================= */
function buildKpis() {
  const F = B.filter(b => b.cat === 'factory');
  const n = lvl => F.filter(b => b.health === lvl).length;
  const tiles = [
    ['factories', '3', 'rocm-cli · rocm-app · gpuflo'],
    ['health', '<span class="health healthy"></span>' + n('healthy') + ' <span class="health attention"></span>' + n('attention') + ' <span class="health failing"></span>' + n('failing'), 'healthy · attention · failing'],
    ['code under management', '316,077', 'tracked lines · 889 files · 15 contributors'],
    ['engine', '0.2.0', 'on 3 of 3 factories · 0 failed passes'],
    ['velocity 30d', '190', 'commits · 14 PRs merged, 4 by agents'],
    ['open work', '10 / 8', 'issues / PRs · 6 waiting on a human'],
    ['defects', '0', 'open bug-labelled · 0 closed 30d · median close 0.1d'],
    ['traffic 14d', '1,438', 'clones (172 unique) · 50 views · 1 star · 2 forks'],
  ];
  document.getElementById('kpis').innerHTML = tiles.map(t =>
    '<div class="kpi"><div class="k">' + t[0] + '</div><div class="v">' + t[1] + '</div><div class="s">' + t[2] + '</div></div>').join('');
}
buildKpis();
"""


def sub(text: str, old: str, new: str, count: int = 1) -> str:
    assert text.count(old) >= count, f"anchor not found: {old[:60]!r}"
    return text.replace(old, new, count)


def main() -> None:
    src = TEMPLATE.read_text()
    lines = src.split("\n")

    # 1. DATA blocks: from `'use strict';` through the closing `];` of P (line before the scene-engines banner)
    start = next(i for i, l in enumerate(lines) if l == "'use strict';")
    end = next(i for i, l in enumerate(lines) if l.startswith("/* ================= Scene engines"))
    lines[start:end] = DATA.rstrip("\n").split("\n") + [""]
    out = "\n".join(lines)

    # 2. chrome
    out = sub(out, "<title>rocm-cli Atlas</title>", TITLE)
    out = re.sub(r'<header class="site">.*?</header>', HEADER, out, count=1, flags=re.S)
    out = re.sub(r'  <footer class="site">.*?</footer>', FOOTER, out, count=1, flags=re.S)
    out = sub(out, "  .thesis { max-width: 68ch; color: var(--muted); margin: 0; }\n",
              "  .thesis { max-width: 68ch; color: var(--muted); margin: 0; }\n" + KPI_CSS)
    out = re.sub(r'<p class="legend-note">Moving dots.*?</p>', LEGEND_NOTE, out, count=1, flags=re.S)
    out = re.sub(r'<div class="scene-hint">.*?</div>', HINT, out, count=1, flags=re.S)

    # 3. engine hooks that were rocm-cli specific
    out = sub(out, "  const TAGS = { iso: '// local AI on AMD GPUs, one binary',\n"
                   "                 map: '// a chart of the rocm-cli territories',\n"
                   "                 space: '// the rocm-cli system, in orbit' };",
              "  const TAGS = { iso: '// every factory in one district, one map',\n"
              "                 map: '// a chart of the district territories',\n"
              "                 space: '// the district, in orbit' };")
    out = sub(out, "'System map of rocm-cli: apps, core crates, dashboard stack, engine adapters, external services, GPU hardware, and the control and data paths between them.'",
              "'District map: three managed factories sized by lines of code, the shared agent-factory engine, the District control tower, host runtime, GitHub, and the control and data paths between them.'")
    out = sub(out, "    if (b.cat === 'hw')   // the GPU glows", "    if (b.glow)   // highlighted block glows")
    out = sub(out, "    const ringed = b.id === 'rocm';", "    const ringed = !!b.ring;")
    out = sub(out, "  const order = ['cli', 'core', 'dash', 'engine', 'ext', 'hw', 'support'];",
              "  const order = ['factory', 'district', 'engine', 'runtime', 'ext', 'state'];")
    out = sub(out, "  note.textContent = 'Building height / marker size ∝ approximate lines of code (hardware & external pads excluded).';",
              "  note.textContent = 'Factory and engine height ∝ tracked lines of code; GitHub pads, host runtime, systemd and the host file are not LOC-scaled.';")
    out = re.sub(r"function ghLink\(f\) \{.*?\n\}", GHLINK, out, count=1, flags=re.S)
    out = sub(out, "    '<p>' + b.blurb + '</p>' +\n    (b.conn ?", SELECT_EXTRA + "\n    (b.conn ?")
    out = re.sub(r"function defaultInspector\(\) \{.*?\n\}", DEFAULT_INSPECTOR, out, count=1, flags=re.S)
    # health dot in the scene label
    out = sub(out, "  t.textContent = b.name;\n}",
              "  if (b.health) {\n"
              "    const dot = document.createElementNS(SVGNS, 'tspan');\n"
              "    dot.setAttribute('fill', HEALTH_COLOR[b.health]); dot.setAttribute('stroke', 'none');\n"
              "    dot.textContent = '\\u25CF ';\n"
              "    t.appendChild(dot);\n"
              "    const nm = document.createElementNS(SVGNS, 'tspan'); nm.textContent = b.name; t.appendChild(nm);\n"
              "  } else t.textContent = b.name;\n}")
    out = sub(out, "const GC = { x: 14.7, y: 11.1 };", "const GC = { x: 17.3, y: 11 };")
    out = sub(out, "  const G = { x0: -1.8, y0: -1.8, x1: 31.2, y1: 24.0 };", "  const G = { x0: -1.8, y0: -1.8, x1: 36.5, y1: 24.0 };", 3)
    out = sub(out, "initLegendUI();\n</script>", "initLegendUI();" + KPI_STRIP + "</script>")

    OUT.write_text(out)
    print(f"wrote {OUT} ({len(out):,} bytes)")


if __name__ == "__main__":
    main()
