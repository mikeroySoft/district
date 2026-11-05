# District

Host-side fleet manager for [Factory](https://github.com/mikeroySoft/factory) installs.

Factory runs an autonomous ticket pipeline for **one** repository: a `.factory.toml`
at the repo root, a host service timer, and a dashboard. District owns everything
that belongs to the **host** instead of the repo — the registry of managed repos,
the engine install and its host-wide target, triage endpoint, dashboard ports, timer
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
| `district apply [slug]` | Reconcile assigned local and remote hosts to the controller registry: the host's one Factory uv tool, units, labels, `factory doctor`, failure cap, package-age policy, and safe legacy-engine cleanup. Remote commands and config writes use bounded BatchMode SSH. A busy host normally defers a changed engine without touching timers or running passes. `--drain` asks compatible timer-started passes not to begin, waits up to `--drain-timeout` (default 6h) for active non-dashboard Factory services, then installs and post-checks only the CI-green engine path; generated-unit drift and failure-cap actions are deferred so no Factory timer is touched. `--upgrade [REF]` requests an explicit target; `--allow-downgrade` permits an ancestor. `--reset SLUG` runs one pass by hand and re-enables a capped timer if it succeeds. |
| `district drain --clear [--host NAME]` | Remove a local or remote host's drain flag while holding the operation lock. Use this for explicit operator recovery; expired flags already stop blocking Factory. |
| `district install-state` | Read fast local District/Factory versions, configured engine/ref policy, systemd timer/service state, durable holds, and the last apply record, including deferred work with its reason and original `since` timestamp. `--json` follows `district/install-state.schema.json`; release fields are `unknown` when the bounded cached lookup is unavailable. |
| `district status` | Fleet operations table across local and remote hosts. Hosts are collected in parallel with bounded SSH; every row and JSON entry names its host. An unreachable host keeps its factories visible as unreachable with the last successful observation time and produces unknown exit 2, not fleet failure exit 1. Exit 1 remains operational attention or a failed controller `gh auth status` (printed once as a fleet-level `FAIL  gh auth` row); exit 0 means every factory classifies normal. `--json` dumps the classification, evidence, and metrics keyed by slug, plus a top-level `host` row when gh auth fails. `--slug OWNER/REPO` (repeatable) collects and prints only those slugs; `--unhealthy` prints only slugs not classified normal. |
| `district doctor` | Check District's own host prerequisites, rerun each factory's scheduler toolchain preflight, and check declared toolchain profiles. `--json`. |
| `district report` | Print a read-only Markdown fleet review across every host; each repository section names its host and unreachable last-seen state. The install section comes from the persisted install-state record. |
| `district metrics [slug]` | Per-factory `git`/`gh` metrics from the hourly cache. `--refresh`, `--max-age`. |
| `district dashboard` | Serve the fleet console and host-badged Atlas on `127.0.0.1:8760`. `--install` enables the dashboard, metrics, and apply units; `--host` adds read-only LAN viewing. Remote collection is controller-initiated SSH and opens no listeners. |
| `district update` | Update District itself from eligible official CI, then run the newly installed `district apply --drain` after releasing the update lock. Dry runs and failed updates never request a drain. `--dry-run`, `--to REF`, `--rollback` (offline), `--yes`, `--json`. |
| `district rm <slug>` | Disable and delete the units, drop the registry entry. Repo files untouched. `--wait`, `--keep-units`. |
| `district version` | Print the District version (same as `-V` / `--version`). |

Before stating an install version or timer state, run `district install-state --json`
and quote its `observed_at`. Every `district apply`, successful or failed, atomically
rewrites `${XDG_STATE_HOME:-~/.local/state}/district/install-state.json`.
Outside an engine drain, text warnings identify timers changed by ordinary unit
reconciliation or a successful `--reset`; unrelated timer activations are excluded.
`last_apply.stopped` lists corrective stops when an ordinary `factory install`
starts an operator-stopped timer.

Outside an engine drain, repo unit reconciliation preserves an existing timer's
pre-install active/inactive state: active timers are restored and inactive timers
are stopped again without disabling them if `factory install` starts them. During
an engine drain District never calls `factory install` and never applies the
failure cap. Generated-unit drift is recorded as
`unit changes deferred during district drain (timers untouched)` for a later
ordinary apply; an operator-stopped timer therefore remains stopped.

An inactive but enabled factory timer is reported as `timer paused` and does not
by itself fail apply, including `--upgrade REF --allow-downgrade`. Timers recorded
in the owning engine's `paused_timers` are also treated as paused while inactive,
even when District temporarily disabled them. Missing or failed timers and
unrecorded disabled timers still fail convergence, as do failed repo checks and
registry `disabled_at` holds. Deferred rows keep their existing handling.

## Job health

`district status` raises operational attention (exit 1), and `district doctor`
reports `FAIL`, when any of these holds **on the same host and job**:

- The last 3 recorded service passes failed. `[defaults].failed_pass_streak`
  can override this threshold; remote `[hosts.NAME.defaults]` overrides apply.
- More than half the recorded runs in the last 6 hours failed.
- An enabled scheduled job has not run for 2 hours and is not currently running.
  A never-run job uses its installation/activation time when available.

These are read-only alerts, separate from `max_failed_passes` and apply's timer
failure cap. A successful latest run clears the streak, but not an unhealthy
six-hour failure rate. Nonzero service/dispatch exits count as failures even
when triage or watch succeeded. A manager's latest exit is never expanded into
multiple runs, and repeated status polls never create history.

Every configured `[hosts.NAME]` entry is inspected, including hosts without
assigned repositories. Status adds `host:NAME` rows for `host_without_repos`
and `unrecognized_factory_job` findings. Recognized hand-made launchd labels
are matched by the `factory-REPO` suffix or an unambiguous working directory.
`--slug` limits repository output, not host discovery. Unreachable hosts retain
unknown exit 2 when there is no other operational attention.

Linux reads the systemd user journal plus `ExecMainStatus`, `Result`, timer
enablement, and last-start/trigger timestamps. macOS reads `launchctl print`
(`last exit code`, `runs`, running state), disabled-job state, and the actual
plist's stdout/stderr logs. If no log paths are declared, it tries conventional
`.factory/logs/factory-REPO.{stdout,stderr}.log` and
`~/Library/Logs/factory-REPO.log` paths. macOS needs no journalctl. A dashboard
or history read that cannot launch journalctl produces `journalctl_unavailable`
rather than a traceback escaping District.

Pass logs use `=== end rc=CODE` markers. Opening/end markers may carry ISO-8601
timestamps or the shell's plain `date` format, including PST/PDT. Undated
completions still count toward streaks and the failure-rate sample; the rate
explicitly marks its window uncertain and reports the undated count rather
than silently discarding runs. Log mtime supplies last-run timing only when
no dated completion is available. Missing timing is not proof of a stall.
Reads are bounded to the latest 10,000 journal records or 4 MiB per log;
rotated-away history is not evidence.

`status --json` retains the per-repo `failure_streaks` list for job-health
alerts. Each alert includes `host`, `repo` (null for an unregistered job),
`label`, `rule` (`failure_streak`, `failure_rate`, or `stalled`), `count`
(failures), `total`, `window_hours`, `first_failed_at`, `last_failed_at`,
`last_run_at`, and nullable `first_error`/`last_error`. Rate alerts also include
`undated_count`. The table names the host, label, failure count, window, and
first error line. `pass_history_errors` includes host, repo, label, code,
error, and unreachable state; read errors are not failed job runs.

## Ownership

| District owns (host, never committed) | Factory owns (committed to its repo) |
|---|---|
| one shared uv-tool engine install and host-wide target | `[[gate.check]]`, `[leak_scan]`, `[repo].upstream` |
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

`district add OWNER/REPO --host NAME` (or a GitHub URL) clones through that
host's SSH connection. The clone parent is `[hosts.NAME].root` when set,
otherwise `[defaults].clone_dir`, resolved against the remote home directory.
`~/` uses the remote home, never the controller's. A checkout path is inspected
on the assigned host; an existing checkout and `.factory.toml` are adopted.
Check and fork detection, configuration writes, and label seeding run remotely.
Without `--no-edit`, the controller's `$EDITOR` edits a local temporary copy of
the proposal before it is copied back to the remote checkout.
`--dry-run` prints the remote destination without cloning or changing either
registry. Remote add installs no scheduler jobs; run `district apply <slug>`
after onboarding to install them.

`[hosts.NAME.defaults]` deep-merges over `[defaults]` for that remote host;
nested tables merge key by key. `defaults_unset` removes inherited dotted keys
after the merge. List each inherited signing environment key there to disable
signing on one host. Neither setting changes the controller's local config.

Remote config replacements are atomic, preserve changed existing bytes in a
timestamped `config.toml.<UTC>.bak` sibling, and report dropped table/key names
without values; `add --dry-run --host NAME` and `apply --dry-run` preview the
projected config (including adopted settings and apply's current package-age
policy) without writing.

Before `district add` finishes onboarding or `district apply` installs scheduler
jobs or resets a capped factory, District renders `factory install --print` on
the owning host. No-op and draining applies do not rerun tool probes. It takes
PATH from the rendered systemd service or launchd plist, and HOME from that
environment (or the owning user's home when omitted). Resolution runs under
`env -i` with only those PATH/HOME values; remote checks use `RemoteRunner`, not
the controller's shell environment. Remote `add --host` preflights registration;
`district apply <slug>` installs its jobs.

Required tools are `factory`, `gh`, `git`, `uv`, and `python3`, plus the effective
review/manager command binaries and the first executable of every
`[[gate.check]]` in the owning checkout's `.factory.toml`. Host defaults,
overrides, and `defaults_unset` apply to review/manager commands. A missing repo
config contributes no gates. A missing binary fails before installation, naming
the host, binary, requiring gate/config/profile, and searched PATH. Known tools
receive version probes (`xcodebuild -version`, for example); failed probes are
WARN, not activation blockers. Unknown commands and relative gate scripts are
resolved without executing them for version discovery. `district doctor` reruns
the same read-only preflight for already-registered factories.

`toolchain = "macos-xcode"` additionally requires `cargo`, `xcodegen`, `swift`,
and `xcodebuild` during onboarding and apply. Doctor's supplemental profile
diagnostics run under that scheduler environment and report Xcode, Homebrew
`xcodegen`, `swift`, `cargo`, `rustc`, authenticated `gh`, and an available iOS
Simulator runtime. Repositories on the same host share one supplemental profile
check. Optional host key `xcode_version` reports version drift as WARN. Unknown
profiles, platform mismatches, missing required tools, and failed auth/runtime
checks are FAIL; version-probe failures are WARN.

`platform` selects systemd user units for Linux and
`~/Library/LaunchAgents/<label>.plist` for macOS. Launchd jobs add
`/opt/homebrew/bin` to the service PATH and write stdout/stderr under the
factory's `.factory/logs/` directory. `local` is reserved for this machine.
District-rendered systemd executable/argument, working-directory and environment
values preserve spaces, quotes and backslashes; literal `%` and ExecStart `$`
are escaped rather than expanded by the manager. Factory render validation and
systemd file-status reads use the same systemd word decoder, not shell parsing.
Render validation checks every `ExecStart=` in a oneshot service, including
separate triage and dispatch commands. After stripping systemd exec prefixes
(`-`, `@`, `:`, `+`, `!`, `!!`), each must use the canonical tool's
`bin/python [-P] -m factory ...` or the resolved Factory CLI. A missing or
invalid command blocks installation and names the affected unit and argv.
Ambiguous loaded `ExecStart` debug text remains untrusted: self-update refuses
an unverifiable executable rather than guessing from a potentially stale unit file.
The retained self-update/recovery bundle includes that decoder and remains
stdlib-only, independent of the managed environment being replaced.
Remote commands use noninteractive SSH with a ten-second connection timeout.
The complete POSIX command script (including environment, working directory, and
deadline wrapper) is base64-encoded and executed by `/bin/sh` through Python.
The SSH login shell sees only a fixed launcher and encoded data, so fish and
POSIX login shells preserve argument quotes, backslashes, and stdin identically.
Remote hosts need Python >=3.11 as `python3`, plus Factory, `uv`, git, and authenticated `gh`
on their noninteractive PATH. Commands with execution deadlines also run a
remote Python watchdog that kills the command's process group on timeout.
District keeps the authoritative registry and operation lock on the controller,
writes a host-filtered Factory config remotely, and continues with other hosts
when one cannot be reached or a remote host operation fails.
`district update` updates this controller; registered
remote factories do not require District installations of their own.

Set optional `factory_ref` under `[defaults]` to reconcile Factory automatically.
`factory_repo` defaults to `https://github.com/mikeroySoft/factory`; no checkout
is required. The chosen SHA (`target`), `target_ref`, and `resolved_at` are
recorded beside the installed `sha` in `[defaults.engine]` locally.
A resolved target alone does not establish installed identity; a deferred first
upgrade reports missing installed SHA/ref as `unknown`.
`latest-odd` selects the highest odd-patch semver tag, including prereleases;
a tag or branch such as `"v0.4.7"` or `"main"` selects that remote ref. A
selected tag commit must itself be green. For a branch, District walks up to 50
remote commits back to the newest commit whose GitHub checks are complete and
green. Remote hosts record that target under `[hosts.NAME.engine]`; their
filtered config receives the same target under `[defaults.engine]`.
Resolution, ancestry, target persistence, receipt, and dashboard completion timeouts
fail the named engine step without further installs; repository checks on a usable
engine and the remaining hosts still run. Initial config-sync timeout fails that
host without retrying setup or installing; other selected hosts continue.
Only confirmed absence permits creating a remote config without a backup.
Any other config-read error fails the named host/path before a write; successfully
read malformed or legacy TOML is backed up byte-for-byte before projection.
Nonzero dashboard restarts also fail the engine step without aborting repository
checks; the pending restart remains saved for the next apply.

Every apply checks that the CLI exists, resolves into the canonical tool, and
has a valid uv receipt before rendering, even without `factory_ref`. Missing
or legacy-symlink CLIs fail the affected repo rows with the CLI path.
Ownership compares resolved CLI and tool paths, accepting symlinked UV/XDG
directories while still rejecting CLIs redirected to legacy checkouts.

Launchd repo drift compares parsed plist bytes on the owning host, including
binary and XML plists. Encoding or XML formatting alone does not trigger a
reinstall; malformed definitions fail the repo row by plist name without replacement.

Every host has exactly one Factory installation: the `factory` uv tool, with no
version in its path. Every managed unit and launchd job uses that tool's Python,
and District invokes its CLI to render units. A changed engine is replaced only
after the owning host is confirmed idle. Ordinary apply defers active, activating,
or deactivating non-dashboard Factory services without touching timers.
Once already idle, it retains the bounded timer-pause/recheck/restore window
that prevents a timer from starting work during replacement. Flag-based drains
never enter that timer window.

`district apply --drain` creates
`${XDG_STATE_HOME:-$HOME/.local/state}/district/drain` only on hosts with pending
engine work. Healthy, already-current hosts do not request a flag, wait for idle,
or run drained post-apply checks; they continue ordinary unit reconciliation.
The UTF-8 JSON flag is written atomically and carries schema version 1,
requester, reason, PID, creation time, and expiry. Compatible timer-started
Factory passes see the flag and skip before starting work. Already-running passes
are never stopped, killed, disabled, masked, or delayed. District waits for active
non-dashboard Factory services to finish, re-checks idle before replacement,
installs, migrates, cleans up, and runs post-apply checks. The default wait is 6 hours;
`--drain-timeout` accepts at most `465min` (7h45m), reserving 15 minutes for installation.
While that flag exists, District does not call `factory install` and does not
apply the failure cap, because Factory exposes no install-without-start option.
Generated-unit drift is reported as
`unit changes deferred during district drain (timers untouched)` and converges
on a later ordinary apply. Repository probes may still run, but cannot change
timer state; an operator-stopped timer stays stopped.
The flag expires after the selected timeout plus 15 minutes, with an absolute
8-hour TTL cap, and cleanup removes it on success, rollback, failure, timeout,
SIGTERM, or SIGINT. `district drain --clear` removes the local flag;
`--host NAME` clears one registered remote host. `district doctor` warns about
active, expired, stale, malformed, future-mtime, or unreadable flags.

Drain admission requires Factory's companion
[`drain-check` support (factory#563)](https://github.com/mikeroySoft/factory/issues/563)
and updated pass units. Check `factory drain-check --help`, then roll out the
rendered `ExecCondition=` (or launchd script gate) through an ordinary apply
before relying on drains. Older pass units ignore the flag; District still
checks running work and times out rather than swapping a busy engine.

Before clearing a successful drain, District probes Factory help, version,
tool-Python import and receipt, verifies the target revision or a descendant,
checks registered dashboards for HTTP 200, and restarts the installed District
dashboard before checking HTTP 200. It also validates the previous engine in
temporary `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` directories without touching the live
install. `factory watch --dry-run` runs from a registered repository checkout,
never the invoking directory; no available checkout is an explicit check failure.

The hourly apply automatically drains an engine change after 3 prior
deferrals or once the original deferral is more than 24 hours old. Earlier
attempts remain plain deferrals. A successful `district update` invokes
`district apply --drain` only after the self-update transaction releases its lock;
an already-current District still drains a pending engine update. Dry runs,
failed updates, and offline rollback do not drain. The apply operation lock
permits only one drain, while metrics uses its independent lock and remains
available during a long wait. Install-state schema 3 records local `last_apply.drain`
and per-host `last_apply.drains`: `requested_at`, `idle_at`, `applied_at`,
`cleared_at`, `waited_s`, and legacy folder counts before/after cleanup.

Engine busy checks use the owning host's service manager, not process cwd or argv.
On Linux, active, activating, or deactivating `factory-*.service` units block an
engine change, including discovered auxiliary and unregistered workers. On macOS,
the service backend checks the corresponding Factory launchd jobs.
Registered main units take precedence over dashboard-looking names.
Registered long-running dashboard services do not block: apply restarts loaded
dashboards after the verified engine swap. Non-Factory services and processes,
including sshd and `handland-*`, never count as Factory work. A hand-run command
outside a Factory service is not tracked by this check.

The same service-state guard covers ordinary upgrades, drains, fleet preflight,
per-host replacement, and rollback. Fleet preflight covers every changed host
before any install, and every host is re-checked before replacement.
New active Factory services or an expired deadline prevent replacement.
Every service probe uses the remaining shared idle budget, so multiple slow
unit queries cannot each consume a fresh timeout.

An authorized replacement persists `engine.install_pending` (target, previous
revision when known, and ref) before invoking uv. A failed install or
verification reinstalls and verifies the previous revision only after the same
idle guards pass. Saved install and pending dashboard intents are recovered
before resolving a fresh target. Only a verified install or rollback clears
the marker; a blocked rollback retains it for the next apply. Only loaded,
registered dashboards restart after a verified change. Roles come from the
registry, not a `-dashboard` suffix, and if both installs fail affected repos
fail without trying another `factory` on `PATH`.

`district apply --upgrade [REF]` uses the configured `factory_ref`, or `main`
when no ref is supplied. It resolves only against the remote repository, never a
local checkout. Installing an ancestor is refused with both revisions named
unless `--allow-downgrade` is present. Without `factory_ref`, ordinary apply
leaves a verified installed engine unchanged.

`install-state` obtains ahead/behind counts from GitHub compare, or reports
`unknown` when that bounded lookup is unavailable. Installed identity and counts
are also unknown when the uv receipt and recorded revision do not agree.

After successful engine reconciliation and unit rendering, apply scans all
managed systemd units and their named, prefix, and type-wide service/timer
drop-ins, Factory launchd plists, the host config, and the uv receipt before
removing unreferenced legacy `factory-*` directories. Complete systemd words are
decoded using single/double quoting, line continuations and C-style escapes,
then `%h` and `%%` are expanded before resolving symlink aliases. Malformed
quotes/escapes, unresolved specifiers or unreadable fragments report the source
file and line where available, make the scan incomplete, and prevent every deletion.
Config/receipt TOML and plist values are scanned as complete strings, preserving
spaces and literal quotes in paths. HOME/tilde expansion adds possible references
without discarding the original literal spelling. Referenced directories and
symlinks are kept and reported.
Local and remote cleanup normalize TOML date, time, and datetime settings to ISO
strings; unsupported scalar types raise before cleanup removes anything.
A legacy `factory_source` setting is removed through District's config writer
after a timestamped, byte-for-byte backup, including on remote hosts; other
files are untouched. A successfully read remote mirror with invalid TOML is also
backed up byte-for-byte before authoritative replacement. Valid mirrors without
`factory_source` and missing files do not create backups. A remote config that is
not valid UTF-8 fails that named host before any backup or replacement; apply
continues with other selected hosts.

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

All factories on one host use that host's single uv tool and target revision;
per-factory engine pins are unsupported. A multi-host apply requires every
participating host to resolve the same newest-green target before any install,
confirms every changed host idle (using host drain flags when requested), and
rolls moved hosts back if one fails.
`district doctor` and `district status` compare hosts'
installed engine, `omp` and `droid` versions, and configured worker and review
models, with one `WARN` row per difference and one `PASS` row when they match.

### Manual Ultron smoke test

From the controller, after registering `mikeroySoft-org/benito-app` on `ultron`:

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

Standalone filesystem/CLI fixtures must isolate `HOME`, XDG config/state/data/cache,
and uv tool/bin directories under temporary roots and replace external runners.
The suite does not require the invoking shell to sandbox the developer's home.

District shells out to the `factory` CLI and never imports `agent_factory`
internals. If District needs a per-repo fact or action, the fix is a flag on
`factory`, not a reimplementation here.
