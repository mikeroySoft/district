# District

Host-side fleet manager for [Factory](https://github.com/mikeroySoft/factory) installs.

Factory runs an autonomous ticket pipeline for **one** repository: a `.factory.toml`
at the repo root, a systemd user timer, a dashboard. District owns everything that
belongs to the **host** instead of the repo — the registry of managed repos, the
engine install and its version pins, triage endpoint, dashboard ports, timer
cadence, systemd units, labels, supply-chain policy, and the failure cap.

Full rationale: [PRD.md](PRD.md). Project page: <https://mikeroysoft.github.io/district/>.

## Install

Two ways in; both end with the same `district` CLI on your machine.

**Have your coding agent do it:** install the skill and ask the agent to onboard
your factories. The skill installs the CLI if it is missing, then drives `add`,
`apply`, `status`, and `update` for you.

```sh
npx skills add mikeroysoft/district
```

**Or by hand.** Linux, Python ≥ 3.11, `uv`, a user systemd manager, `gh`
(authenticated), and [`factory`](https://github.com/mikeroySoft/factory) on the
host:

```sh
uv tool install git+https://github.com/mikeroySoft/district   # or: pipx install ...
```

`district doctor` checks the prerequisites. `district update` keeps the CLI
current from official CI and rolls back offline, so the install above is needed
only once.

## Commands

| Command | What one invocation does |
|---|---|
| `district add <path\|url>` | Onboard a repo, or adopt one that already has `.factory.toml`. `--dry-run` prints the proposal (slug, clone path, fork parent, port, gate checks); `--host NAME`, `--check NAME=CMD`, `--exclusive a,b`, `--no-edit`. |
| `district apply [slug]` | Reconcile the host to the registry: units, labels, `factory doctor`, failure cap, package-age policy. If `[defaults].factory_ref` is set (for example, `"main"`), every apply resolves that ref from the `factory_source` origin and installs its exact commit only after all GitHub check runs succeed; already-current, pending, and failed commits are left alone. Then runs `gh --version` and `factory --version` under each dispatch/dashboard unit's PATH (10 s timeout); any failure exits 1 naming the unit and binary. `--upgrade` explicitly reinstalls Factory from the local `[defaults].factory_source`; `--reset SLUG` runs one pass by hand and re-enables a capped timer if it succeeds. |
| `district status` | Fleet operations table. Exit 1 on operational attention or a failed `gh auth status` (printed once as a fleet-level `FAIL  gh auth` row), 2 on unknown/stale observation, 0 when every factory classifies normal. `--json` dumps the classification, evidence, and metrics keyed by slug, plus a top-level `host` row when gh auth fails. `--slug OWNER/REPO` (repeatable) collects and prints only those slugs; `--unhealthy` prints only slugs not classified normal. |
| `district doctor` | Check District's own host prerequisites. `--json`. |
| `district report` | Print a read-only Markdown fleet review. |
| `district metrics [slug]` | Per-factory `git`/`gh` metrics from the hourly cache. `--refresh`, `--max-age`. |
| `district dashboard` | Serve the fleet console on `127.0.0.1:8760`. `--install` enables the dashboard, metrics, and apply units; `--host` adds read-only LAN viewing. |
| `district update` | Update District itself from eligible official CI. `--dry-run`, `--to REF`, `--rollback` (offline), `--yes`, `--json`. |
| `district rm <slug>` | Disable and delete the units, drop the registry entry. Repo files untouched. `--wait`, `--keep-units`. |
| `district version` | Print the District version (same as `-V` / `--version`). |

## Ownership

| District owns (host, never committed) | Factory owns (committed to its repo) |
|---|---|
| engine install, upgrade, per-repo version pin | `[[gate.check]]`, `[leak_scan]`, `[repo].upstream` |
| triage endpoint and model | `.github/ISSUE_TEMPLATE/agent_task.md` |
| worker/reviewer argv, gate lock path | `.factory-lessons.md` |
| dashboard port, timer cadence, bind host | `.gitignore` lines |
| systemd units, labels, registry, policy | |

Three rules follow: District writes committed files exactly once, at onboarding,
and only reports drift on them afterwards; it rewrites host artifacts on every
`apply` without asking; it **never commits** to a managed repo — it prints the
`git add … && git commit` line for you.

The registry *is* the config: `$XDG_CONFIG_HOME/factory/config.toml`. A repo is
managed iff it has a `[repo."owner/name"]` table with a `path`. No second store.

## Hosts

Factories are local when their registry entry has no `host`. Define a remote host,
then assign it with `district add --host ultron …` or a repo table:

```toml
[hosts.ultron]
ssh = "factory@ultron.example"
platform = "macos"
root = "~/factories" # optional

[repo."mikeroySoft-org/benito-app"]
path = "/Users/mike/factories/benito-app"
host = "ultron"
```

`local` is reserved for this machine. Remote factories are recorded but skipped
until remote execution support lands in #96.

Set `factory_ref = "main"` under `[defaults]` to let the hourly plain
`district apply` follow Factory's remote `main` branch. District never checks
out or requires a clean developer clone: it resolves that branch on the clone's
GitHub `origin`, requires successful check runs for the resolved SHA, installs
that immutable Git revision with `factory_extras`, and records it in
`[defaults.engine]`. Remove `factory_ref` before rolling back, or the next apply
will reinstall the branch's SHA. Omit `factory_ref` to retain explicit
`district apply --upgrade` behavior.

## Agent use

[`skills/district/SKILL.md`](skills/district/SKILL.md) is the agent-facing
operating procedure (onboard, apply, reset, remove, inspect, update). `npx skills
add mikeroysoft/district` installs it for Claude Code, Codex, Amp, Cline, and the
other agents the `skills` CLI supports; point your agent at it instead of this
README.

## Development

```sh
uv run python -m unittest discover -s tests
```

District shells out to the `factory` CLI and never imports `agent_factory`
internals. If District needs a per-repo fact or action, the fix is a flag on
`factory`, not a reimplementation here.
