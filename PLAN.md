# District — Implementation Plan

Companion to `PRD.md`. Rev 2, 2026-09-03, after adversarial review by
`gpt-5.6-sol` (24 findings; all accepted, disposition in §Review log).

Ground truth: agent-factory at `~/dev/mikeroysoft/factory`, commit
`96ce8f0` **plus an uncommitted working tree** (adds `[dashboard].theme`,
`Environment=PATH` in units). Line references below are against that working
tree and will shift; treat them as pointers, not anchors. Installed as a `uv
tool` from that directory. Managed repos: `rocm-cli` (fork of `ROCm/rocm-cli`,
port 8765), `gpuflo` (net-new, port 8766, has `dashboard.theme`). Both
dashboard units currently bind `--host 0.0.0.0`.

## Phase 0 — findings that shape the plan

- `config.load()` parses `.factory.toml` into `raw` then reads keys; host
  layering goes at that point. `[repo].slug` in the committed file takes
  precedence over the `origin` remote — District must use the *resolved* slug,
  not re-derive it.
- `dashboard --json` does not emit first-gate pass / bounce rate; they are
  computed in `dashboard.html` (`firstPass`, `bounceRate`). It also has no
  version field, and `config.timer_interval` is a hardcoded `"10min"`.
- `dashboard.py` already parses the user journal for the unit
  (`SYSLOG_IDENTIFIER == "systemd"`). Any pass-failure counting belongs there,
  not in District.
- `dispatch.main()` returns 0 for handled outcomes (worker/gate/review failure →
  escalation). Only uncaught errors produce a nonzero unit result. The failure
  cap therefore targets *operational* failure (crash, auth, endpoint down) —
  the case that burns spend without producing escalations.
- `factory install` is not convergent: it ignores `daemon-reload` errors,
  prints but does not fail on `enable --now` errors, returns 0 regardless, and
  neither restarts a running dashboard after its unit changes nor removes one
  when `--dashboard` is dropped.
- `factory init` prints label failures and returns 0.
- `Config.unit` is `factory-<basename>`; two repos with the same basename
  collide on units, journal, and dashboard.
- `tomllib` is read-only; District writes the host file with `tomli_w` (one
  dependency; hand-emitting regex strings is where bugs live).
- Package minimum age on this host: `npm 12` `min-release-age` is an integer
  **number of days**, and its precedence is CLI > env > project `.npmrc` >
  user; `uv 0.12.5` has only an absolute `exclude-newer`; `cargo 1.98` has
  nothing. Setting user-level config is both too broad (affects unrelated
  projects) and too weak (a repo `.npmrc` overrides it). Environment variables
  on the factory units are the correct scope: they beat project config and
  touch nothing else on the host.
- `.factory.toml` tables have mixed ownership: `[gate]` holds host `lock` and
  repo `timeout` + `[[gate.check]]`; `[dashboard]` holds host `port` and repo
  `theme`. Adoption must remove **keys**, not tables.

## Phase 1 — agent-factory prerequisites

One PR on agent-factory. All items testable with `tests/test_factory.py`'s
`make_repo` harness. Ship as `0.2.0` with the version single-sourced.

### 1.1 Host config layer (`config.py`)

- `HOST_CONFIG = $XDG_CONFIG_HOME/agent-factory/config.toml` (default `~/.config`).
- `HOST_TABLES = {"triage", "dashboard", "workers", "review", "install"}` plus
  the single key `gate.lock`. Host file contents outside these are **ignored
  and reported** by doctor — the host may never supply `gate.check`,
  `leak_scan`, `[repo].upstream`, etc. A clone on another machine must run the
  same gate.
- `load()`: read repo toml → resolve `slug` (committed `[repo].slug`, else
  origin) → `raw = merge(filter(host.defaults), filter(host.repo[slug]), repo_toml)`
  where `filter` keeps only `HOST_TABLES`/`gate.lock` and `merge` is recursive
  on dicts, replace on scalars/lists.
- `Config` gains `install: dict` (`every`, `dashboard`, `host`, `env`) and
  `raw_repo: dict`.
- `[install].env` — table of environment variables rendered into every unit
  (§1.2). This is where District puts the supply-chain policy.
- Tests: precedence defaults < per-repo < repo file; host-supplied
  `gate.check`/`leak_scan`/`upstream` never reach `Config`; missing host file ≡
  current behaviour; `[repo."acme/widgets"]` matched by the *resolved* slug when
  the repo file sets `[repo].slug`.

### 1.2 `factory install` convergent (`onboard.py`)

- Defaults from `cfg.install` via `parser.set_defaults`; `--dashboard` becomes
  `BooleanOptionalAction`. `dashboard.py` reads `cfg.install["every"]`.
- Units gain one `Environment=K=V` line per `[install].env` entry (after the
  existing `PATH` line).
- Reconcile the full unit set: write desired units; **remove** the dashboard
  unit (stop + delete) when dashboard is false; `daemon-reload`; `enable --now`
  timer; `restart` the dashboard service when its file content changed.
  Nonzero exit on any systemd failure. `--print` unchanged.
- Test: `--print` with host `every="5min"` and `env={UV_EXCLUDE_NEWER=…}`
  emits both lines.

### 1.3 `factory init --labels-only`

Ensures the six labels, touches no files, exits nonzero on any `gh` failure.
`add` and `apply` call this instead of `gh label create`.

### 1.4 `factory doctor` drift + `--json`

- Rows collected as `{status, label, detail}`; `--json` emits rows plus
  `{ok, version, repo, root}`. District takes the resolved slug from `repo`.
- New checks: unknown keys in `raw_repo` (WARN); host-owned keys committed
  (`HOST_TABLES` or `gate.lock` present in `raw_repo`, WARN — the adopt
  signal); shipped-template keys absent from the repo file (INFO row, status
  `null`); issue template SHA-256 ≠ shipped (WARN); host file contains
  non-host tables (WARN).
- Test: repo with `[triage]` + misspelled key → both WARNs in JSON.

### 1.5 `dashboard --json` additions

- `version` (single-sourced: `pyproject.toml` via `importlib.metadata`, fallback `__version__`).
- `metrics`: port `firstPass`, `bounceRate`, `escalations`, `medAttempts` from
  `dashboard.html` to `metrics(tickets) -> dict`. HTML keeps its copy for now
  (cleanup tracked).
- `dispatcher.consecutive_failures`: from the journal parsing already in
  `dashboard.py` — count trailing unit results that are not
  `Deactivated successfully`. Passes invoked by hand from a shell are outside
  the unit and do not count either way.
- Test: synthetic tickets → 0.5/0.5; canned journal → 3.

### 1.6 Ship without breaking the live timers

1. Tests green; commit; version `0.2.0`.
2. `systemctl --user disable --now factory-rocm-cli.timer factory-gpuflo.timer`.
3. Wait until both `factory-*.service` are inactive (`systemctl --user is-active`).
4. `uv tool install --reinstall --from ~/dev/mikeroysoft/factory agent-factory`.
5. `factory --version` → `0.2.0`; `factory install --host 0.0.0.0` in each
   repo (**explicit `0.0.0.0`** — matches the running units; a silent switch to
   loopback would drop LAN access).
6. `factory doctor` in each repo shows the "host setting committed" WARNs —
   expected pre-migration.

This same stop → wait → act → restore sequence is what `district apply
--upgrade` automates (§2.3).

## Phase 2 — District

Python ≥ 3.11, `uv tool install`, entry `district`, dependency `tomli_w`.
Subprocess only: `factory`, `gh`, `git`, `systemctl`, `uv`.

```
district/
  cli.py      subcommand table (same shape as agent_factory/cli.py)
  host.py     HOST_CONFIG read/write, registry, port allocation
  add.py      onboard + adopt
  apply.py    reconcile, --upgrade, failure cap, policy
  status.py   fleet table
  rm.py
tests/test_district.py
```

### 2.1 `host.py`

- `load()`, `save()` (atomic: tmp + `os.replace`).
- `repos()` → tables under `[repo]` with a `path`.
- `next_port()` = max(all persisted `dashboard.port`, 8764) + 1. **Every
  allocation is persisted in the repo table**, never dropped as
  "equal to defaults"; `[defaults.dashboard].port` is rejected on load.
- `add` refuses a repo whose basename matches an existing registry entry
  (unit-name collision, see Phase 0).
- Test: round-trip a regex string with backslashes and quotes; port after a
  gap; basename rejection.

### 2.2 `district add <path|url>`

1. URL → `gh repo clone <url> <clone_dir>/<name>`, `clone_dir` from
   `[defaults.clone_dir]` (default `~/dev/mikeroysoft`).
2. `factory doctor --json` in the repo → resolved `repo` slug (honours a
   committed `[repo].slug`). If doctor cannot run (no git remote), stop.
3. **Write the registry entry first**: `path`, `dashboard.port = next_port()`.
   Both adopt and net-new paths depend on it.
4. Fork detection: `gh repo view <slug> --json isFork,parent`. If fork: find
   the remote whose URL matches `parent.nameWithOwner`; if found, record
   *that remote's name* as `upstream`; if none, `git remote add upstream <url>`
   — but abort before any write if a remote named `upstream` exists with a
   different URL.
5. **Adopt** (repo file exists): parse; lift host-owned values into
   `[repo."slug"]` (`[triage]`, `[workers]`, `[review]` whole; `dashboard.port`
   and `gate.lock` as single keys). Rewrite the repo file by **span-aware key
   removal** on the original text: drop exactly the lines of the moved keys
   (whole-table removal only when every key in the table was moved), keep all
   other lines and comments byte-for-byte. Refuse and write nothing if the
   file uses inline tables or dotted keys for any affected table. Print
   `git diff`. Record the currently installed dashboard bind host from the
   existing unit file into `[repo."slug".install].host` so re-rendering does
   not change exposure. Go to step 8.
6. Net-new gate proposal from marker files (`Cargo.toml` → fmt/clippy/test;
   `package.json` → `npm test`, `npm run lint` if scripted; `pyproject.toml` →
   `pytest`/`ruff check .` if in deps; `Makefile` → `make test` if the target
   exists), each annotated with its source file. **No marker → stop**: District
   does not write placeholder checks (PRD §7.1). `--check name=argv…` provides
   them non-interactively.
7. Ask which checks are `exclusive`; open `$EDITOR`; abort on nonzero exit or
   if the result has no `[[gate.check]]` with a non-`true` command. Write
   `.factory.toml` with repo-owned keys only.
8. `factory init` → `factory init --labels-only` → `factory doctor` →
   `factory install`. Any nonzero exit stops with that tool's output; the
   registry entry stays so `apply` can resume.
9. Print the commit line.

Non-interactive: `--exclusive a,b --no-edit --check …`.

### 2.3 `district apply [slug] [--upgrade [REF]]`

1. `--upgrade [REF]`: source `[defaults].factory_source`; resolve a local
   commit and install its export. `HEAD` (the default) requires a clean tree.
   Sequence: disable all managed timers → wait for every managed service to
   be inactive (timeout → abort with the timer state restored) →
   `uv tool install --reinstall` → continue to step 3 → re-enable timers.
   Failure anywhere after disabling re-enables the timers before exiting.
2. Supply-chain policy → **host `[install].env`**, not global package-manager
   config: `NPM_CONFIG_MIN_RELEASE_AGE=<days>` (duration → whole days, minimum
   1), `UV_EXCLUDE_NEWER=<now − age, RFC 3339>`. `[defaults.min_package_age]`
   default `"24h"`; `"0"` removes both. `cargo` on PATH → one WARN line.
   Because `UV_EXCLUDE_NEWER` is absolute, units are re-rendered on every
   `apply`; the value drifts by one apply interval (ponytail ledger).
3. Per repo: `factory install` (host defaults; nonzero exit is a FAIL row),
   `factory init --labels-only`, `factory doctor --json`, `factory dashboard
   --json` (for `consecutive_failures` and `version`).
4. Failure cap: `consecutive_failures ≥ [defaults.max_failed_passes]`
   (default 10) and timer active → disable the timer, set
   `disabled_at`/`disabled_reason` in the repo table. Re-enable only via
   `district apply --reset <slug>`, which `systemctl --user start` the
   service, waits, and re-enables the timer iff that run succeeded.
5. Output one line per repo: version · doctor OK/WARN(n)/FAIL(n) · drift ·
   failures-in-a-row · policy. Exit nonzero if any FAIL, any disabled_at set,
   any timer inactive, or versions differ across the fleet.

### 2.4 `district status`

`factory dashboard --json` is collected per repo in a thread pool and adapted
into the shared operational classification in `OPERATIONS-CONSOLE-SPEC.md`
§6.1. Failed or malformed snapshots mean unavailable/partial observation,
not stopped machinery. Independently usable runtime evidence is retained.
Text and JSON report operating state, execution state, observation quality,
assessment and findings; project metrics remain context. `--json` retains
snapshots and metrics alongside the classification, keyed by slug.
Exit 0 means normal/empty fleet, 1 means operational attention, and 2 means
unknown without attention. The obsolete mixed `health`/`reasons` fields are removed.

### 2.5 `district rm <slug>`

Order: disable timer → refuse if the service is active (`--wait` to block) →
stop and disable dashboard → delete unit files → `daemon-reload` → verify
units gone → remove registry entry. `--keep-units` skips the systemd steps.

### 2.6 Tests

`unittest`, temp dirs, a stub `factory`/`gh`/`systemctl` first on `PATH` that
records argv and returns canned output. One behavioural regression each for:
host round-trip with regex strings; port allocation and persistence; basename
collision; adopt writes `path`; adopt on gpuflo-shaped file keeps
`dashboard.theme` and `gate.timeout`; adopt refuses inline tables; explicit
`[repo].slug` keys the registry correctly; parent remote under another name;
occupied `upstream` name aborts; net-new with no marker and `--no-edit` writes
nothing; npm day conversion (`"24h"` → `1`, `"36h"` → `2`); `install`
nonzero → FAIL row; cap at 9 no action / 10 disable / second apply still exits
nonzero / `--reset` re-enables only on success; malformed dashboard JSON →
unavailable observation; `--upgrade` restores timers on failure.

## Phase 3 — Migration and PMF check

1. `district add ~/dev/mikeroysoft/rocm-cli` (adopt). Diff shows only
   `[triage]` and `dashboard.port` removed; `[dashboard].theme` absent here,
   `[gate]` untouched. Commit in rocm-cli.
2. Same for gpuflo; diff must keep `theme = ".factory-dashboard.css"`.
3. `district apply` → units re-rendered with `--host 0.0.0.0` (recorded at
   adopt) and the policy env lines; `factory doctor` in each repo shows zero
   host-setting WARNs.
4. `district status` → both `0.2.0`, KPIs populated, exit 0.
5. For each registry path: `git -C <path> show HEAD:.factory.toml` contains no
   `[triage]`, `[dashboard].port`, `[workers]`, `[review]`; the adoption
   commit's diff removes only those (PRD §7.3).
6. After one interval, `journalctl --user -u factory-rocm-cli.service -n 5`
   shows a pass under the new units (PRD §7.2).
7. Then a third, net-new repo, timed (< 10 min, PRD §7.1).

## Sequencing

| Step | Depends on | Size |
|---|---|---|
| 1.1 host layer | — | M |
| 1.2 install convergent + env | 1.1 | M |
| 1.3 labels-only | — | S |
| 1.4 doctor | 1.1 | M |
| 1.5 dashboard json | — | S |
| 1.6 ship | 1.1–1.5 | S |
| 2.1 host.py | — | S |
| 2.2 add | 1.3, 1.4, 2.1 | L |
| 2.3 apply | 1.2–1.5, 2.1 | L |
| 2.4 status | 1.5, 2.1 | S |
| 2.5 rm | 2.1 | S |
| 2.6 tests | 2.x | M |
| 3 migration | all | S |

## Ponytail ledger

- `UV_EXCLUDE_NEWER` is absolute; refreshed per `apply`. Upgrade: relative
  form if uv ships one.
- KPIs computed in Python and HTML until the HTML reads `snapshot.metrics`.
- Failure cap counts unit-level failures only; handled pipeline failures
  escalate (bounded by `max_attempts`) and are visible in `status` as
  escalations. If spend from handled failures becomes the problem, add a
  pass-outcome event to `events.jsonl` and count that instead.
- Adopt-mode TOML rewriting is line-span based and refuses inline/dotted
  forms rather than parsing them.

## Review log (rev 1 → rev 2)

All 24 findings from the `gpt-5.6-sol` review accepted. Grouped by change:

- npm `min-release-age` is days → §2.3.2 converts; env var, not user config.
- Adopt skipped writing `path` → §2.2.3 registry entry first.
- `install` non-convergent; dashboards bind `0.0.0.0` → §1.2 reconcile +
  nonzero; adopt records current bind host (§2.2.5); ship step uses explicit host.
- Upgrade during running passes; `--dirty` → §1.6 / §2.3.1 stop-wait-act-restore, no override.
- Cap misses handled failures; `_PID=1` wrong; District parsed journal →
  §1.5 `consecutive_failures` from agent-factory; scope stated in ledger.
- Hand-run success cannot re-enable → `--reset` starts the unit (§2.3.4).
- Host could supply repo-owned tables → `HOST_TABLES` filter (§1.1).
- Slug re-derivation ignored `[repo].slug` → doctor JSON `repo` (§2.2.2).
- Upstream remote name/occupancy → §2.2.4.
- Table-level removal loses `gate.timeout`/`dashboard.theme` → key-level (§2.2.5).
- Port dropped when equal to defaults → always persisted, default port rejected (§2.1).
- Global uv/npm config → unit env (§1.1 `[install].env`, §2.3.2).
- Missing shipped-key check → §1.4 INFO row.
- No version contract / not in snapshot → §1.5.
- Snapshot `errors` shown healthy → §2.4 unhealthy.
- Hardcoded labels via `gh` → `factory init --labels-only` (§1.3).
- Exit 0 on later applies while disabled → §2.3.5.
- `rm` ordering → §2.5.
- Basename collision → §2.1.
- Placeholder `["true"]` → §2.2.6 refuses.
- `grep` glob vacuous → §3.5 registry-path check.
- Missing tests → §2.6 enumerated.
- Stale line refs → Phase 0 header notes the dirty working tree; refs demoted
  to pointers.
