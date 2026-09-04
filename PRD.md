# District — Product Requirements

Status: draft, 2026-09-03. Decisions recorded here are settled unless a later
section says otherwise.

## 1. Problem

[agent-factory](https://github.com/mikeroySoft/factory) runs an
autonomous ticket pipeline for **one** GitHub repository: `.factory.toml` at the
repo root, a systemd user timer per repo, a dashboard per repo. It is deliberately
repo-scoped and stateless.

The moment a second repository is onboarded, a class of concerns appears that no
single factory can own because they belong to the **host**, not the repo:

| Concern | Today (two factories: `rocm-cli`, `gpuflo`) |
|---|---|
| Which agent-factory version runs | One `uv tool` install shared by both timers. Nobody records that; upgrading is "remember to reinstall". |
| Generated artifacts going stale | systemd units bake in `PATH` and `ExecStart` at install time. Issue template, `.gitignore` lines, labels are copied at `init` and never refreshed. Keys added to agent-factory later (`review_rounds`, `cost_pattern`) never appear in existing configs. |
| Host settings committed to public repos | Both `.factory.toml` files carry `[triage] url = 127.0.0.1:11435`, the local model name, and a hand-picked `[dashboard] port`. That is machine config in `mikeroySoft/rocm-cli` history, duplicated, and drifting. |
| Port allocation | `8765` / `8766` chosen by hand. Third repo: guess again. |
| Install flags | `factory install --every --dashboard --host` are not persisted. A re-render after an upgrade requires recalling them. |
| Fleet visibility | `systemctl --user list-timers` plus one browser tab per dashboard. No single answer to "is everything healthy?". |
| Onboarding | `factory init` writes a placeholder; a human or the agent skill then reads CI, decides fork-or-not, picks a port, edits TOML, runs `doctor`, runs `install`. Correct, but every step is manual and unrecorded. |

The metaphor: each factory has its own floor, rules, and owner. The district is
the shared infrastructure — power, roads, permits — that every factory needs and
none should manage itself.

### What is *not* the problem

- Pipeline behaviour (triage, gate, review, merge) is agent-factory's job and is
  out of scope here.
- "Fork sync" is not a pluggable component. It is one key (`[repo].upstream`);
  `dispatch.py` disables the whole sync stage when it is unset. District needs
  no plugin or component system — it needs to *set the key correctly at
  onboarding*.

## 2. Users

One operator today (Mike) running N factories on one Linux workstation. The
product is right if it is still the right tool at N = 10 on the same host. It
is **not** designed for teams, multiple hosts, or other people's machines; if
that need appears, it is a new PRD.

## 3. Ownership model (the core requirement)

Everything District touches is classified by *where it lives*, not by feature.

| District owns — host-side, never committed | Factory owns — committed to its repo |
|---|---|
| agent-factory install and upgrade | `[[gate.check]]`, `[leak_scan]`, `[repo].upstream` |
| triage endpoint and model | `.github/ISSUE_TEMPLATE/agent_task.md` |
| worker / reviewer argv defaults, gate lock path | `.factory-lessons.md` |
| dashboard port allocation, timer cadence, bind host | `.gitignore` lines |
| systemd units (rendered via `factory install`) | |
| registry of managed repos | |
| GitHub labels (idempotent re-apply) | |
| supply-chain policy (package minimum age) and its check | |
| consecutive-failure cap on each timer | |

Rules that follow:

1. District **writes** committed files exactly once, at onboarding, and only
   **reports drift** on them afterwards. Repo owners decide when their floor changes.
2. District **rewrites** host-side artifacts on every `apply` without asking.
3. District **never commits** to a managed repo. It prints the `git add … &&
   git commit` line, as `factory init` does today.

## 4. Prerequisite: agent-factory changes

District shells out to the `factory` CLI and never imports `agent_factory`
internals. For that boundary to hold, agent-factory must be able to check and
render one repo *against host config* on its own. Three changes, all small:

1. **Host config layer.** `config.load()` also reads
   `~/.config/agent-factory/config.toml` when present. Merge order, lowest to
   highest precedence: `[defaults.*]` → `[repo."owner/name".*]` → repo
   `.factory.toml`. Tables have the same shape as `.factory.toml`. Unknown keys
   are ignored, so District may keep its own registry data (e.g. `path`) in the
   same file.
2. **`factory install` defaults from host config.** `--every`, `--dashboard`,
   `--host` fall back to `[defaults.install]` so the command is re-runnable with
   no arguments.
3. **`factory doctor` reports drift and speaks JSON.** New checks: issue
   template differs from the shipped template; `.factory.toml` has keys the
   loader does not know (today a typo silently does nothing); keys that exist in
   the shipped template but not in the repo file (informational). `--json`
   output for District to consume.

Example host file after migration:

```toml
[defaults.triage]
url = "http://127.0.0.1:11435/v1/chat/completions"
model = "ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M"

[defaults.install]
every = "10min"
dashboard = true
host = "127.0.0.1"

[repo."mikeroySoft/rocm-cli"]
path = "/home/mike/dev/mikeroysoft/rocm-cli"
[repo."mikeroySoft/rocm-cli".dashboard]
port = 8765

[repo."mikeroySoft/gpuflo"]
path = "/home/mike/dev/mikeroysoft/gpuflo"
[repo."mikeroySoft/gpuflo".dashboard]
port = 8766
```

After this lands, each repo's `.factory.toml` contains only repo truth: gate
checks, leak excludes, upstream.

## 5. Functional requirements

### 5.1 `district add <path | url>`

Onboard a repository, or adopt one that already has a `.factory.toml`.

Auto-detected, never asked:
- Clone if given a URL (`gh repo clone` into a configured base directory).
- Fork status and parent via `gh repo view --json isFork,parent`. If fork:
  ensure an `upstream` remote pointing at the parent exists, set
  `[repo].upstream = "upstream"`. If not: leave unset.
- Ecosystem from marker files (`Cargo.toml`, `package.json`, `pyproject.toml`,
  `Makefile`) → proposed `[[gate.check]]` list.
- Next free dashboard port above the highest allocated in the registry.
- Repo slug from `origin`.

Asked:
- Which proposed checks need single-tenant hardware (`exclusive = true`). This
  is undetectable from source.
- Confirmation of the proposed gate commands, via `$EDITOR` on the generated
  file. Proposals come from CI config or marker files; District never invents
  commands it cannot point to.

Then, in order: write the registry entry and host per-repo table; write the
minimal `.factory.toml`; `factory init`; `factory doctor`; `factory install`;
print the commit command.

**Adopt mode** (repo already has `.factory.toml`): lift host-owned keys
(`[triage]`, `[dashboard]`, `[workers]`, `[review]`, `[gate].lock`) into the
host file, strip them from the repo file, show the diff, print the commit
command. Both existing repos go through this path once.

Done when `factory doctor` prints `OK: 0 blocking problem(s)` and the timer is
active.

### 5.2 `district apply [repo] [--upgrade]`

Reconcile the host to the registry. Idempotent; safe to run any time.

- `--upgrade`: reinstall agent-factory from its source (the local checkout
  today; a git ref later) so every timer picks up the new code on its next tick.
  The install is an explicit snapshot — not editable — so an in-progress edit in
  the checkout never runs unattended.
- Per repo (all, or the one named): `factory install` (re-renders units with the
  current `PATH` and `ExecStart`), ensure labels, `factory doctor --json`.
- **Failure cap.** Count consecutive failed dispatcher passes from the journal.
  At `[defaults.max_failed_passes]` (default 10) in a row, `systemctl --user
  disable --now` the timer and report it. A pass that fails every ten minutes
  otherwise burns model spend indefinitely. Re-enable by `apply` after the cause
  is fixed; the counter resets on the first successful pass.
- **Supply-chain policy.** Workers install packages on the host with the
  operator's credentials. District owns the host-wide minimum-package-age
  setting for each package manager present (`uv`, `npm`, `cargo`) and checks it
  is set; a registry compromise is typically pulled within ~24 h, so refusing
  releases younger than that is the cheapest effective control. Missing policy
  is a WARN in the per-repo report, never a silent skip.
- Print one line per repo: version, doctor result, drift items for committed
  files, failure count, policy status. Never modify a committed file here.

Done when every registered repo reports doctor OK and the same agent-factory
version.

### 5.3 `district status`

One table, one screen, from `factory dashboard --json` per repo:

repo · agent-factory version · timer next / last · last pass exit · tickets in
flight · escalations (`ready-for-human` count) · upstream drift (forks only:
commits behind, parked sync issue if any).

Also per repo: first-gate pass % and review bounce rate (already computed by
`factory dashboard --json`). Adding a repo must not hide a bad floor; a fleet
that scales a mediocre pipeline scales mediocrity.

Non-zero exit if any repo has a failed last pass, a dead timer, or a timer
District has auto-disabled (§5.2), so it can be used from a cron or a shell
prompt.

### 5.4 `district rm <repo>`

`systemctl --user disable --now` both units, delete the unit files, drop the
registry entry. Repo files are not touched; the factory can be re-added later.

### 5.5 Registry

The host config file *is* the registry. No second store. A repo is managed iff
it has a `[repo."owner/name"]` table with a `path`.

### 5.6 `district dashboard` — the bird's-eye view

One page for the whole district, served by District on its own port and
installed as `district-dashboard.service`. The hierarchy is District → Factory;
it never descends into a factory's tickets. Each factory tile links to that
factory's own dashboard for detail.

The primary surface is the **District Atlas**: an isometric city in which every
managed repository is a building whose height is proportional to its tracked
lines of code, standing on a plate with the shared engine, the District control
tower, host runtime, and GitHub. Roads are the real control/data paths, cited
to `file:line`. Built from the codebase-atlas engine; `atlas/build_atlas.py`
is the renderer seam and `atlas/district-atlas.html` the first static cut
(snapshot 2026-09-04). The live page regenerates the DATA blocks from the
registry, a LOC scan, and each factory's `dashboard --json`.

Above the map, an exec KPI strip: factory count, health tally, code under
management, engine version spread, velocity, open work, defects, traffic.
Clicking a factory shows its health signals and the metrics in §5.7.

### 5.7 Health and metrics model

A factory's notifications stay with the factory. District derives one
**health** level per factory from `factory dashboard --json` alone:

| Level | Any of |
|---|---|
| failing | timer inactive or District-disabled · last pass failed · snapshot `errors` non-empty · `consecutive_failures ≥ 1` · dashboard unreachable |
| attention | open `ready-for-human` · upstream sync parked · bounce rate > 33% · doctor WARN |
| healthy | none of the above |

Exec metrics per factory, all sourced from `git`, `gh`, or the snapshot — never
estimated:

- **Size**: tracked LOC (by language), files, test LOC/files.
- **People**: contributor count, top-3 author share, commits (all / 30d / 7d).
- **Velocity**: merged PRs 30d (and how many from `agent/` branches), commits 30d.
- **Work**: open issues by factory label, open PRs, tickets tracked.
- **Quality**: first-gate pass %, bounce rate, escalations, open bug-labelled issues.
- **Resolution**: issues closed 30d, bug-labelled closed 30d, median days-to-close.
- **Traffic**: 14-day clones/views (count, unique), stars, forks, watchers.
- **Engine**: version, timer state, last pass, consecutive failures.

`district status --json` gains these fields so the terminal, the dashboard, and
the atlas share one source. Metrics that need `git`/`gh` are collected on a
timer (hourly is enough), not on page load.

### 5.8 Per-factory engine version

A factory may pin an engine version: `[repo."owner/name".engine] version =
"0.2.0"` (git ref or release tag). District keeps one venv per distinct pinned
version under `~/.local/share/district/engines/<version>/` and `factory
install` renders `ExecStart` from `[install].python` so each factory's units
point at the venv for its version. Unpinned factories track
`[defaults.engine].version`. `district apply --upgrade` upgrades the default;
`district apply --upgrade <slug>` moves one pin. `district status` shows the
version spread; a factory more than one release behind the default is an
`attention` signal. Requires: `[install].python` in agent-factory (S).

### 5.9 Management surfaces

The dashboard exposes the same operations as the CLI, with confirmation:
onboard a repository (the `add` questionnaire as a form: path/URL, detected
fork parent, proposed gate checks, exclusive flags), adopt, upgrade (fleet or
one factory), reset a capped timer, remove. Every action shells out to the
`district` CLI; the page never mutates state itself, so the CLI stays the
audited path and the page stays a thin client.

## 6. Non-goals

Deliberate, with the condition under which each would be reconsidered.

- ~~Combined web dashboard~~ — **reversed 2026-09-04**, see §5.6. Three
  factories was enough to make "is the fleet healthy?" a question the terminal
  table answers but an exec view does not.
- ~~Per-repo version pinning~~ — **reversed 2026-09-04**, see §5.8. A factory
  with outside contributors may need to lag an engine release.
- **Cross-repo ticket routing, shared lessons, any plugin system.** No evidence
  of need.
- **Multi-host or multi-user.** New PRD.
- **Reimplementing any `factory` command.** District orchestrates; if District
  needs a per-repo fact or action, the correct fix is a flag on `factory`.

## 6a. Known gaps (tracked, not scheduled)

Gaps that need a change in agent-factory first. District records them so the
fleet-level hook is designed in; closing them is coordinated work, not District
scope alone.

- **Workers run unsandboxed.** Every worker runs bare on the host with the
  operator's `gh` credentials, SSH keys, and shell. Sandcastle and OpenBot both
  isolate per run (container per agent, egress policy). Closing this: agent-factory
  gains a sandbox wrapper for the worker argv (e.g. `podman run … omp -p`), then
  District applies it once fleet-wide via `[defaults.workers]`. Until then, the
  supply-chain policy in §5.2 is the only mitigation. Source: mattpocock/sandcastle,
  CopilotKit/OpenBot, @altryne supply-chain PSA (2026-05-15).

## 7. What product–market fit looks like

The market is one operator with a growing fleet. Fit is observable, not
felt. District has PMF when all of the following hold:

1. **Onboarding is one command and under ten minutes**, including the gate
   checks being real (taken from CI, not placeholders). Measured on the third
   repo added.
2. **An agent-factory change reaches every factory with one command**
   (`district apply --upgrade`), and `district status` shows one version across
   the fleet within one timer interval. No hand-edited unit files remain on the
   host.
3. **No host setting exists in any managed repo's git history going forward.**
   `grep -l 127.0.0.1 */.factory.toml` returns nothing after adoption.
4. **"Is the fleet healthy?" is answered by one exit code.** `district status`
   in a prompt or cron replaces `systemctl list-timers` + N browser tabs.
5. **Adding a fork vs. a net-new repo requires zero manual config difference.**
   Fork detection sets `upstream`; the operator only confirms.
6. **District stays small.** If it grows a component system, a second config
   store, or its own copy of any `factory` logic, the boundary in §3 has
   failed and the fix is in agent-factory, not District.

Leading indicator that fit is *lost*: the operator edits a systemd unit, a
port, or a triage URL by hand instead of through District. Each such edit is a
requirement District missed.

## 8. Sequencing

1. agent-factory: host config layer, `install` defaults, `doctor` drift +
   `--json` (§4). Ship and reinstall.
2. District: `add` (adopt mode first — migrate `rocm-cli` and `gpuflo`),
   `status`, `apply`, `rm`.
3. Verify §7 items 2–5 against the two migrated repos before adding a third.

## 9. Open questions

- Where clones from URLs land (`~/dev/mikeroysoft/<name>` is the current
  convention; make it `[defaults.clone_dir]`).
- Whether gate-check proposal should call the existing agent skill
  (`skills/agent-factory/SKILL.md` already instructs an agent to derive checks
  from CI) instead of marker-file heuristics. Heuristics first; measure how
  often the operator rewrites them.
