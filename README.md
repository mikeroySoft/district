# District

Host-side fleet manager for [Factory](https://github.com/mikeroySoft/factory) installs.

Factory runs an autonomous ticket pipeline for **one** repository: a `.factory.toml`
at the repo root, a host service timer, and a dashboard. District owns everything
that belongs to the **host** instead of the repo — the registry of managed repos,
the engine install and its version pins, triage endpoint, dashboard ports, timer
cadence, service definitions, labels, supply-chain policy, and the failure cap.

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
| `district apply [slug]` | Reconcile assigned local and remote hosts to the controller registry: units, labels, `factory doctor`, failure cap, package-age policy. Remote commands and config writes use bounded BatchMode SSH. Unit changes drain only that repository's active timer; a busy service is deferred without touching its timer. If `[defaults].factory_ref` is set, each target host installs the exact CI-green commit through its own `uv`; engine metadata is per host. An unreachable host exits 1 for its factories while other hosts continue. Then runs `gh --version` and `factory --version` under each reconciled dispatch/dashboard unit's PATH. `--upgrade` explicitly reinstalls Factory from the target host's `factory_source`; `--reset SLUG` runs one pass by hand and re-enables a capped timer if it succeeds. |
| `district install-state` | Read fast local District/Factory versions, configured engine/ref policy, systemd timer/service state, durable holds, and the last apply record, including deferred work with its reason and original `since` timestamp. `--json` follows `district/install-state.schema.json`; release fields are `unknown` when the bounded cached lookup is unavailable. |
| `district status` | Fleet operations table across local and remote hosts. Hosts are collected in parallel with bounded SSH; every row and JSON entry names its host. An unreachable host keeps its factories visible as unreachable with the last successful observation time and produces unknown exit 2, not fleet failure exit 1. Exit 1 remains operational attention or a failed controller `gh auth status` (printed once as a fleet-level `FAIL  gh auth` row); exit 0 means every factory classifies normal. `--json` dumps the classification, evidence, and metrics keyed by slug, plus a top-level `host` row when gh auth fails. `--slug OWNER/REPO` (repeatable) collects and prints only those slugs; `--unhealthy` prints only slugs not classified normal. |
| `district doctor` | Check District's own host prerequisites and every toolchain profile declared by a managed repository. `--json`. |
| `district report` | Print a read-only Markdown fleet review across every host; each repository section names its host and unreachable last-seen state. The install section comes from the persisted install-state record. |
| `district metrics [slug]` | Per-factory `git`/`gh` metrics from the hourly cache. `--refresh`, `--max-age`. |
| `district dashboard` | Serve the fleet console and host-badged Atlas on `127.0.0.1:8760`. `--install` enables the dashboard, metrics, and apply units; `--host` adds read-only LAN viewing. Remote collection is controller-initiated SSH and opens no listeners. |
| `district update` | Update District itself from eligible official CI. `--dry-run`, `--to REF`, `--rollback` (offline), `--yes`, `--json`. |
| `district rm <slug>` | Disable and delete the units, drop the registry entry. Repo files untouched. `--wait`, `--keep-units`. |
| `district version` | Print the District version (same as `-V` / `--version`). |

Before stating an install version or timer state, run `district install-state --json`
and quote its `observed_at`. Every `district apply`, successful or failed, atomically
rewrites `${XDG_STATE_HOME:-~/.local/state}/district/install-state.json`; a warning
in text mode identifies timers the last apply re-enabled.

## Ownership

| District owns (host, never committed) | Factory owns (committed to its repo) |
|---|---|
| engine install, upgrade, per-repo version pin | `[[gate.check]]`, `[leak_scan]`, `[repo].upstream` |
| triage endpoint and model | `.github/ISSUE_TEMPLATE/agent_task.md` |
| worker/reviewer argv, gate lock path | `.factory-lessons.md` |
| dashboard port, timer cadence, bind host, toolchain profile | `.gitignore` lines |
| systemd user units / launchd agents, labels, registry, policy | |

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
factory_source = "~/src/factory" # optional override for this host
xcode_version = "27.1"
root = "~/factories" # optional

[repo."mikeroySoft-org/benito-app"]
path = "/Users/mike/factories/benito-app"
host = "ultron"
toolchain = "macos-xcode"
```

`toolchain = "macos-xcode"` makes `district doctor` check that repository's
assigned host. Repositories on the same host share one profile check. The profile
runs each command with the launchd PATH District configures, including
`/opt/homebrew/bin`, and reports the Xcode version, working `xcodebuild`,
Homebrew `xcodegen`, `swift`, `cargo`, `rustc`, authenticated `gh`, and an
available iOS Simulator runtime. Optional host key `xcode_version` pins the
expected version and reports drift as `WARN`. Missing tools, failed commands,
unknown profiles, and platform mismatches are `FAIL` rows prefixed with the host
name.

`platform` selects systemd user units for Linux and
`~/Library/LaunchAgents/<label>.plist` for macOS. Launchd jobs add
`/opt/homebrew/bin` to the service PATH and write stdout/stderr under the
factory's `.factory/logs/` directory. `local` is reserved for this machine.
Remote commands use noninteractive SSH with a ten-second connection timeout.
Remote hosts need `python3` as well as Factory, `uv`, git, and authenticated `gh`
on their noninteractive PATH. Commands with execution deadlines also run a
remote Python watchdog that kills the command's process group on timeout.
District keeps the authoritative registry and operation lock on the controller,
writes a host-filtered Factory config remotely, and continues with other hosts
when one cannot be reached or a remote host operation fails.
`district update` updates this controller; registered
remote factories do not require District installations of their own.

Set `factory_ref = "latest-odd"` under `[defaults]` to let the hourly plain
`district apply` follow Factory's highest odd-patch semver tag, including
prereleases. District resolves remote tags without checking out or requiring a
clean developer clone, uses the peeled commit for annotated tags, and never
falls back to `main`. A tag, such as `factory_ref = "v0.4.7"`, or a branch,
such as `"main"`, pins that explicit remote ref instead; tags take precedence
over branches with the same name.

Every target resolves the ref from its Factory checkout, requires successful
GitHub check runs, and installs the immutable revision through `uv` with
`factory_extras`. Optional `[hosts.NAME].factory_source` overrides the default
source on that host. If a remote host has no override and the default is a
controller-absolute path, the controller resolves its canonical Git origin and
the remote installs `git+<origin>@<sha>`; the controller path is never sent
remotely. Engine state is recorded under `[defaults.engine]` for the controller
and `[hosts.NAME.engine]` for each remote host. An unchanged commit
is left alone without checking CI or reinstalling. A busy Factory service
defers only that host's changed engine install without disabling another host's
timers. Remove `factory_ref` before rolling back, or the next apply will
reinstall the configured ref's SHA. Omit `factory_ref` to retain explicit
`district apply --upgrade` behavior.

### Manual Ultron smoke test

From the controller, after registering `mikeroySoft-org/benito-app` on `ultron`
and ensuring its configured `factory_source` checkout exists and is clean on
Ultron:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 factory@ultron.example true
district apply mikeroySoft-org/benito-app --upgrade
ssh -o BatchMode=yes -o ConnectTimeout=10 factory@ultron.example \
  'test -f ~/.config/factory/config.toml &&
   test -f ~/Library/LaunchAgents/factory-benito-app.plist &&
   launchctl print gui/$(id -u)/factory-benito-app &&
   cd /Users/mike/factories/benito-app &&
   factory doctor --json &&
   factory dashboard --json'
district apply mikeroySoft-org/benito-app
```

The first apply must report the remote engine install and a converged
`mikeroySoft-org/benito-app` row. The second must remain converged. A failed SSH
probe or apply must name `ultron` without running commands for factories assigned
to other hosts.

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
