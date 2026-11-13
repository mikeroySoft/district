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
| `district apply [slug]` | Reconcile assigned local and remote hosts to the controller registry: the host's one Factory uv tool, units, labels, `factory doctor`, failure cap, package-age policy, and safe legacy-engine cleanup. Remote commands and config writes use bounded BatchMode SSH. Every engine replacement pauses only the Factory timers that were active, waits up to `--drain-timeout` (default 90min) for existing services to finish, installs and verifies the target, rolls back on failure, and restores the exact prior timer state on every exit. It never stops, kills, or starts a service while timers are paused. `--drain` remains accepted but is not required for this safe protocol. `--upgrade [REF]` requests an explicit target; `--allow-downgrade` permits an ancestor. `--reset SLUG` runs one pass by hand and re-enables a capped timer if it succeeds. |
| `district drain --clear [--host NAME]` | Remove a local or remote host's drain flag while holding the operation lock. Use this for explicit operator recovery; expired flags already stop blocking Factory. |
| `district install-state` | Read fast local District/Factory versions, configured engine/ref policy, systemd timer/service state, durable holds, and the last apply record, including deferred work with its reason and original `since` timestamp. `--json` follows `district/install-state.schema.json`; release fields are `unknown` when the bounded cached lookup is unavailable. |
| `district status` | Fleet operations and per-host engine release drift, including registered hosts without repositories. Hosts are collected in parallel with bounded SSH; every row and JSON entry names its host. Exit 1 means operational attention, an engine not at the latest stable release, or failed controller `gh auth status`; exit 2 means unknown observation or release identity when no row needs attention. Exit 0 means every observation is normal. `--json` adds a top-level `engines` map beside classifications and metrics keyed by slug, plus `host` when gh auth fails. `--slug OWNER/REPO` (repeatable) limits repository collection and output; `--unhealthy` prints only non-normal repository rows. Both retain all-host engine status. |
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
Outside an engine-replacement window, text warnings identify timers changed by
ordinary unit reconciliation or a successful `--reset`; unrelated timer
activations are excluded. `last_apply.stopped` lists corrective stops when an
ordinary `factory install` starts an operator-stopped timer.

Ordinary non-engine reconciliation preserves an existing timer's pre-install
active/inactive state: active timers are restored and inactive timers are stopped
again without disabling them if `factory install` starts them. During engine
replacement, District renders with `factory install --print` and writes/reloads
Linux units without launching services. Launchd definition drift is deferred:
bootstrapping changed jobs would launch work. Failure caps retain their ordinary
non-engine behavior outside that paused window.

An inactive but enabled factory timer is reported as `timer paused` and does not
by itself fail apply, including `--upgrade REF --allow-downgrade`. Timers recorded
in the owning engine's `paused_timers` are also treated as paused while inactive,
even when District temporarily disabled them. Missing or failed timers and
unrecorded disabled timers still fail convergence, as do failed repo checks and
registry `disabled_at` holds. Deferred rows keep their existing handling.

## Repeated pass failures

`district status` raises operational attention (exit 1) when a factory's last
N recorded service passes **on the same host** all failed. The evidence column
names the host, repo, N, first failure time in that window, and the last error
line when available. `district doctor` reports the same streak as `FAIL`.
Every assigned host is checked, not just a multi-host repo's triage host.

Configure N in the host registry; the default is 6:

```toml
[defaults]
failed_pass_streak = 6 # positive integer; one hour at install.every = "10min"
```

Remote `[hosts.NAME.defaults]` overrides apply to this setting too. This is a
read-only alert, separate from `max_failed_passes` and apply's timer failure cap.
One success among the last N passes clears the streak; fewer than N completions
does not alert. Unreachable hosts do not produce streaks and retain unknown
exit 2 when there is no other operational attention.

Linux reads retained systemd user-journal unit completion results, including
failures before Factory can import. macOS reads the launchd plist's stdout/stderr
pass logs, or conventional `.factory/logs/factory-REPO.{stdout,stderr}.log` and
`~/Library/Logs/factory-REPO.log` paths when no plist log paths are declared.
Pass logs need `=== end rc=CODE` markers and an ISO-8601 timestamp with timezone
on the pass's opening marker or end line. A single manager exit status is not
treated as N passes; status polls never create pass history. Reads are bounded
to the latest 10,000 journal records or 4 MiB per log; missing, rotated-away,
or insufficient history cannot establish a streak.

`status --json` includes a `failure_streaks` list per repo, with `host`, `repo`,
`count`, `first_failed_at`, `last_failed_at`, and nullable `last_error`. It also
adds the evidence to the existing operational findings. History read errors
appear in `pass_history_errors` (host, repo, error, unreachable) and as doctor
warnings; an observation failure is not a failed factory pass.

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

Before `district add` finishes onboarding or an ordinary `district apply` installs
scheduler jobs or resets a capped factory, District renders
`factory install --print` on the owning host. Engine replacement uses the same
print-only rendering while timers are paused. No-op applies do not rerun tool
probes. Rendering takes PATH from the systemd service or launchd plist, and HOME
from that environment (or the owning user's home when omitted). Resolution runs
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
A resolved target alone does not establish installed identity; before the first
successful upgrade, a missing installed SHA/ref is reported as `unknown`.
`latest-release` selects the latest published, non-prerelease, non-draft GitHub
release on `factory_repo`. `latest-odd` selects the highest odd-patch semver tag,
including prereleases. A tag such as `"v0.4.7"` selects that remote tag.
To track any branch, explicitly set both `factory_ref = "main"` (or another
branch) and `factory_track_branch = true`; an unmarked branch policy fails
without changing the engine. A selected tag commit must itself be green.
For an opted-in branch, District walks up to 50 remote commits back to the
newest commit whose GitHub checks are complete and
green. Remote hosts record that target under `[hosts.NAME.engine]`; their
filtered config receives the same target under `[defaults.engine]`.
Resolution, ancestry, target persistence, receipt, and dashboard completion timeouts
fail the named engine step without further installs; repository checks on a usable
engine and the remaining hosts still run. Initial config-sync timeout fails that
host without retrying setup or installing; other selected hosts continue.
Only confirmed absence permits creating a remote config without a backup.
Any other config-read error fails the named host/path before a write; successfully
read malformed or legacy TOML is backed up byte-for-byte before projection.

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
and District invokes its CLI to render units.

Every engine replacement, whether selected by `factory_ref`, `--upgrade`, or the
accepted explicit `--drain` form, follows the same bounded protocol. District
records the initial timer state, stops only Factory timers that were active, and
waits up to `--drain-timeout` (default 90 minutes) for existing Factory services
to finish. It logs progress while waiting. The deadline is
configurable; expiry aborts replacement and proceeds directly to timer restoration.
`--drain` is no longer required to make an engine replacement safe.

While those timers are paused, District never stops, kills, or starts a service.
Linux unit reconciliation uses `factory install --print`; District writes the
rendered definitions, runs `daemon-reload`, and enables units with an `[Install]`
section without `--now`. New timers are enabled for future scheduling but remain
inactive; only timers recorded as active are restarted.
Launchd drift is deferred rather than bootstrapping jobs during the pause.
Applies with no engine replacement keep their existing install and failure-cap
behavior.

After installation, District verifies the resolved SHA in the uv receipt and
matches clean `factory --version` output against the installed distribution's
metadata (and the requested version for release tags), using PEP 440 normalization
(for example, `v0.5.1-rc.1` matches `0.5.1rc1`). Verification also checks
that the CLI resolves into the canonical tool. A failure reinstalls and verifies the
previous engine revision. Success, failure, rollback, timeout, SIGTERM, and SIGINT
all restore exactly the timers recorded as active; timers initially inactive are
left inactive. It logs progress throughout.
Progress is printed and sent to syslog with a `district-apply:` prefix, so CLI
and scheduled upgrades are journaled. `install-state` also records drain timestamps
and the exit status. Follow with `journalctl --user --grep='district-apply:'`.
Timeout reports blocking units and available ticket numbers/stages; unavailable
ticket observations are explicitly `unknown`. Rollback exits nonzero and prints
`rolled back`. Restoration logs every restarted timer's `ActiveState`.
`--drain` additionally retains the existing extended postchecks (Factory help,
isolated import, dashboards, watch dry-run, and a temporary previous-engine probe).

`district apply --upgrade` with no REF selects the latest published stable
Factory release, regardless of `factory_ref`. Drafts and prereleases are excluded;
unavailable releases fail the upgrade without falling back to a branch.
`--upgrade main` (or another explicit branch) opts into the newest CI-green
commit on that branch for this invocation. Tags and full SHAs remain explicit
targets. Resolution uses only the remote `factory_repo`, never a local checkout.
Installing an ancestor is refused with both revisions named unless
`--allow-downgrade` is present. Without `factory_ref`, ordinary apply leaves a
verified installed engine unchanged.

`district status` also checks each registered host's installed engine against
the latest stable release, including hosts without assigned repositories.
Release drift names the host, installed SHA or tag, and latest tag, and exits 1;
unknown release or installed identity exits 2 unless another row needs attention.
`--json` includes these host observations in a top-level `engines` map;
repo runtime failures remain visible independently. Host metadata is excluded from
the public dashboard's repository projection limit.

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
`host_name`, `triage_host`, and `release_host = <triage_host>` directly in its
`[repo."SLUG"]` table. The triage host also owns releases
([factory#723](https://github.com/mikeroySoft/factory/issues/723)).
On the controller that config is this registry, so `district add` and
`district apply` keep `host_name = "local"` and the matching `release_host`
there. Other keys such as `claim_ttl` set in the registry's `[repo."SLUG"]`
reach every host.
`district add --host NAME` on a repo already registered
on another host appends `NAME` to `hosts`, keeping the first host as
`triage_host`; a bare `district add` leaves the repo's hosts alone.
`district rm` removes the units on every listed host and refuses, removing
nothing, while any of them is running a pass unless you pass `--wait`.
`district doctor` checks a repo's `toolchain` on every host that runs it.

Several hosts on one repo need Factory's host claims
([factory#722](https://github.com/mikeroySoft/factory/issues/722)) so two hosts
never work the same ticket. Before every multi-host `district add` or
`district apply`, `factory claims --supported` must print exactly
`host-claims 1` (with an optional final newline) and exit 0 on every listed
host (run with `~/.local/bin` first on `PATH` and a 10-second timeout).
No version sniffing: a missing command, different output, or non-zero exit
refuses the operation without changing the registry, naming the failing
host and linking factory#722. Apply also enforces this when the selected
repo shares a host with any multi-host repo. A host that can't be reached
is reported as unreachable, not as lacking support. Applying a repo whose hosts
run no multi-host repo never contacts the fleet hosts.

All factories on one host use that host's single uv tool and target revision;
per-factory engine pins are unsupported. A multi-host apply requires every
participating host to resolve the same newest-green target before any install,
runs the bounded pause-and-drain protocol on every changed host, and rolls moved
hosts back if one fails.
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
