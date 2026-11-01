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
in text mode identifies timers apply disabled and then re-enabled. `last_apply.stopped`
lists corrective stops when `factory install` starts an operator-stopped timer.

When repo units change, apply preserves an existing timer's pre-install active/inactive
state: active timers are drained and restored; inactive timers are stopped again
without disabling them if install starts them. First installs and unchanged units
still converge an inactive timer through install. A retained inactive timer remains visible in apply's
output and nonzero convergence status.

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
defaults_unset = ["triage.url"] # inherited dotted keys to omit remotely

[hosts.ultron.defaults.install.dispatch_env]
GH_CONFIG_DIR = "/Users/mike/.config/factory-gh"

[repo."mikeroySoft-org/benito-app"]
path = "/Users/mike/factories/benito-app"
host = "ultron"
toolchain = "macos-xcode"
```

`[hosts.NAME.defaults]` deep-merges over `[defaults]` for that remote host;
nested tables merge key by key. `defaults_unset` removes inherited dotted keys
after the merge. List each inherited signing environment key there to disable
signing on one host. Neither setting changes the controller's local config.

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
is left alone without checking CI or reinstalling. A busy Factory service,
active/activating/deactivating `handland-*` user unit, or hand-run uv-tool
`factory gate` / `factory dispatch` process defers that host's changed engine
install. Both plain apply and `--upgrade` check before and after draining;
dashboards do not block swaps. The receipt names the busy unit or PID, other
repo unit drift still converges, and the next apply retries. Remove `factory_ref`
before rolling back, or the next apply will reinstall the configured ref's SHA.
Omit `factory_ref` to retain explicit `district apply --upgrade` behavior.

### One repo on several hosts

A repository can run on more than one host so they share its ticket queue:

```toml
[repo."mikeroySoft/factory"]
path = "/home/mike/dev/factory"
hosts = ["local", "ultron"]
triage_host = "local"
```

Set either `host` or `hosts`, not both. With more than one host,
`triage_host` is required and must be one of them; with one host it is
refused. It is the only host that triages, and `district status` reads the
repo's state from it. Each host's Factory config carries that host's
`host_name` and the `triage_host` directly in its `[repo."SLUG"]` table, as
factory#526 reads them. On the controller that config is this registry, which
already holds `triage_host`, so `district add` and `district apply` keep
`host_name = "local"` there. Other keys such as `claim_ttl` set in the
registry's `[repo."SLUG"]` reach every host.
`district add --host NAME` on a repo already registered
on another host appends `NAME` to `hosts`, keeping the first host as
`triage_host`; a bare `district add` leaves the repo's hosts alone.
`district rm` removes the units on every listed host and refuses, removing
nothing, while any of them is running a pass unless you pass `--wait`.
`district doctor` checks a repo's `toolchain` on every host that runs it.

Several hosts on one repo need Factory's host claims
([factory#526](https://github.com/mikeroySoft/factory/issues/526)) so two hosts
never work the same ticket. Until `factory claims --supported` prints exactly `host-claims 1` and exits
0 on every listed host (run with `~/.local/bin` first on `PATH` and a
10-second timeout), `district add` refuses to add a second host and
`district apply` refuses to apply a repo that shares a host with any
multi-host repo, both with a link to that issue. A host that can't be reached
is reported as unreachable, not as lacking support. Applying a repo whose hosts
run no multi-host repo never contacts the fleet hosts.

All hosts of a shared repo run one engine commit. `district apply` resolves
`factory_ref` on every one of those hosts and installs only when they agree,
with no partial upgrade. Before installing anywhere it checks the commit's CI
and running passes on every host that needs it; if a host can't resolve the
ref, its CI check isn't green, or a pass is running there, no host installs.
If an install still fails or is deferred, hosts that already moved are
reinstalled at their previous commit. Either way apply names the host that
blocked it. `district apply --upgrade` refuses when the selection touches
these hosts; use `factory_ref`. `district doctor` and `district status` compare the hosts'
installed engine, `omp` and `droid` versions, and configured worker and review
models, with one `WARN` row per difference and one `PASS` row when they match.

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
