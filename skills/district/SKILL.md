---
name: district
description: Operate District, the host-side fleet manager for Factory installations. Use when the user asks to add, adopt, remove, inspect, reconcile, upgrade or recover Factory, update or roll back District itself, collect fleet metrics, or install and open the District dashboard.
---

# District

`district` manages many repositories that each run the `factory` pipeline on one Linux host. The registry and shared settings live in `$XDG_CONFIG_HOME/factory/config.toml` (default `~/.config/factory/config.toml`). `district --help` lists operations; `district <command> --help` is the source of truth for options.

First run `district --version`. If absent, install it with the first available of:

```sh
uv tool install git+https://github.com/mikeroySoft/district
pipx install git+https://github.com/mikeroySoft/district
python3 -m pip install --user git+https://github.com/mikeroySoft/district
```

Confirm `district --version`, `factory --version`, and authenticated `gh auth status`, then choose the operation:

Before stating any District/Factory install version or timer state, run `district install-state --json` and quote its `observed_at`. This local record is the source of truth; `district status --json` is a separate GitHub-bound fleet observation and does not substitute for it. Release fields may be `unknown` when the cached lookup is unavailable.


- Fleet health or a stalled factory → **Inspect**.
- Add or adopt a repository → **Onboard**.
- Converge host state or deploy a Factory update → **Apply**.
- Recover a failure-capped timer → **Reset**.
- Remove a repository from the fleet → **Remove**.
- Refresh aggregate data or serve the fleet UI → **Metrics and dashboard**.
- Update or roll back District itself → **Update District**.

## Inspect

Run `district status`. Exit 0 means all registered factories classify normal (or the fleet is empty); exit 1 means supported operational attention or a failed `gh auth status` (one fleet-level `FAIL  gh auth` row; `--json` carries it as top-level `host`, not per slug); exit 2 means unknown/partial/stale observation without attention. Exit 1 takes precedence over 2. For diagnosis, run `district status --unhealthy --json` (or `--slug owner/repo`, repeatable, to collect only those slugs; full fleet JSON is very large) and report each affected slug's `operating_state`, `execution_state`, `observation`, and `findings`, retaining `sources` timestamps and evidence. Missing telemetry remains unknown, not stopped. Project escalations and review bounces remain context, not incidents. See `OPERATIONS-CONSOLE-SPEC.md` §6 for the shared JSON contract.

Done when every operational finding and observation gap has a stated scope and evidence source.

## Onboard

1. Run `district add --dry-run <path-or-github-url>`. Check the resolved slug, clone path, fork parent, dashboard port, proposed gate commands, and checks marked for exclusive hardware.
2. For a new repository, take gate commands from its CI or existing scripts. Pass corrections with repeated `--check NAME=CMD`; pass GPU or other single-tenant checks with `--exclusive name1,name2`.
3. Run `district add <path-or-github-url>`. Existing `.factory.toml` selects adopt mode; District lifts host-owned settings into the registry. New repositories open the proposed config in `$EDITOR` unless `--no-edit` was explicitly requested.
4. Report the printed commit command. District writes onboarding files but deliberately does not commit them.
5. Run `district status` and report the new factory's operating state, findings, and observation gaps.

Done when the repository is registered, its timer is active, and `factory doctor` has no blocking problem.

## Apply

- `district apply [slug]` reconciles the selected local and remote hosts: their one shared Factory uv tool, units, labels, doctor state, failure caps, package-age policy, and unreferenced legacy engines. Remote work stays on the target through bounded, noninteractive SSH. With `factory_ref`, District resolves a branch's bounded remote history to its newest green commit, while a selected tag must itself be green, then records one host target; without it, ordinary apply retains the verified engine. A changed target defers for outside hand work, pauses only active Factory timers, drains non-dashboard services and uv-tool processes within the original idle deadline, installs the one tool with `--force`, and verifies the receipt and CLI. Dashboard-owned gate/dispatch is outside work; managed exemptions require stable PID/start-time identities and launchd ancestry. All replacement/rollback guards recheck outside work and the original deadline. On failure, reinstall and verify the previous revision only when those guards pass. Restore exactly the paused timers; unverified tools or blocked automatic rollback retain named paused/recovery intent for the next idle apply. Re-render units and restart dashboards after a verified change. Remove a legacy `factory-*` directory only after units, Factory plists, host config, and the uv receipt prove it unreferenced; an incomplete scan preserves every directory.
- Add `--upgrade [REF]` only when the user requests a Factory upgrade. REF defaults to configured `factory_ref` or `main` and resolves against `factory_repo`, never a checkout. Add `--allow-downgrade` only with explicit authorization to install an ancestor. Report target and installed SHAs, rollback outcome, timer restoration, and kept referenced legacy folders. A migrated `factory_source` key is removed by District after a timestamped backup; no per-factory engine pin exists.
- Report committed-file drift; leave those files for the repository owner. Apply owns host state only.

Done when every selected row has a version, `doctor OK` or an explained warning, no command failure, and an active timer unless District has deliberately capped it. Read `district install-state --json` afterwards; report its `observed_at`, `last_apply`, engine change, holds, and any re-enabled timers.

## Reset

Read `district status --slug owner/repo --json` and fix the recorded cause first. Then run:

```sh
district apply --reset owner/repo
```

Reset runs one dispatcher pass. It clears the cap and re-enables the timer only after that pass succeeds.

Done when the command reports `pass succeeded`, the timer is re-enabled, and `district status --slug owner/repo --json` no longer reports `capped`; report any remaining findings or observation gaps separately.

## Remove

Use the exact registered slug from `district status --slug <slug> --json`. `district rm <slug>` disables and removes its timer, service, and dashboard unit on the assigned local or remote host, then drops the controller registry entry and updates the remote Factory config; repository files remain untouched. If a pass is active, report that fact and use `--wait` only when the user asked to wait for removal. Use `--keep-units` only when the user explicitly wants the registry entry removed while services remain.

Done when District reports the slug removed and a fresh status no longer contains it.

## Metrics and dashboard

- `district metrics [slug]` reads the hourly cache; `--refresh` recollects now.
- `district report` takes its install section from `${XDG_STATE_HOME:-~/.local/state}/district/install-state.json`; retain that section and its `observed_at` in weekly fleet reporting.
- `district dashboard --install` writes and enables the District dashboard service plus the hourly metrics timer.
- `district dashboard --no-open` starts one shared collector and serves it in the foreground at `127.0.0.1:8760` by default. Use explicit `/?view=overview`, `/?view=flows`, or `/?view=brief` console URLs; add the URL-encoded `factory` scope and optional `stage`, `execution`, or `panel` selection. `/` and `/legacy` retain the Atlas and management surface until cutover. Runtime observations use a five-second cadence; full GitHub/config snapshots use 60 seconds. Browser tabs and repeated `/api/fleet` requests only read the cache.
- Overview is the watch surface: one equally weighted card per factory in stable slug order, with a compact stage diagram aggregating distinct live executions per reported stage (a factory can occupy several stages at once), dispatcher/admission state, active/configured capacity, latest observed transition, a confirmed next-dispatch countdown, freshness, findings, history window/gaps, and a count strip (normal, attention, unknown; unknown is not healthy). Motion is evidence only: a gentle pulse means a fresh confirmed active stage, a brief arrive/highlight means a newly observed transition/completion since this browser's baseline; stale/unavailable observations are muted and stationary. Polls, reconnects, duplicate records, route revisits and hidden tabs never replay old events; reconnect gaps are listed in the recent-activity rail. "Watch mode" hides secondary controls only (Esc exits) and "Pause animation" pauses this browser's animation, not the factories; both persist for the tab session and are not routes. Clicking a factory name or stage opens scoped Flows (`factory`, `stage`); a finding opens its `panel`.
- Resource summaries expand to source-stamped identity/ownership evidence. Unreported stage edges are not drawn. Watch mode applies only to Overview, and its animation control remains available. Lost connections stop motion and retain aging last-known facts; requests time out after ten seconds. A disappearing finding is not a confirmed recovery: only a newer positive scheduling observation confirms recovery from a cap or unexpected stop; other disappearances remain explicitly unconfirmed.
- `--host 0.0.0.0` enables read-only LAN viewing, not remote management. A LAN URL is read-only even on the host. For manage/detect, open direct HTTP to numeric loopback or `localhost` with the actual server port (default `http://127.0.0.1:8760`); reverse proxies and forwarded headers are unsupported. The page reports capability and keeps unavailable controls disabled.
- For LAN viewing, use the listener's numeric IP and actual port. DNS aliases other than `localhost` are unsupported. `/api/fleet` is the bounded sanitized projection in `OPERATIONS-CONSOLE-SPEC.md` §§6.2 and 8, not raw `status --json`; inspect cache revision, source/collection ages, history gaps and omission metadata alongside unchanged assessments.

## Update District

Requires Linux with a working user systemd manager, `uv`, Git, authenticated `gh`, and Python 3.11+ outside the managed tool environment. Dry-run reads local install/service state and queries GitHub for the target SHA and CI; it is not an offline command, but creates no state, locks, or service changes. Rollback uses only the retained local artifact and does not require GitHub access.

1. Run `district update --dry-run --json`. Report the installed and immutable target SHAs, exact `ci.yml` / `test` status, affected District service state, and operation list. Pending, failed, or missing CI blocks installation rather than selecting an older commit; an already-installed target is a no-op.
2. Use `--to REF` only for the user's requested official tag, branch, or SHA. District accepts no alternate repository. Run with `--yes` only after the exact plan is authorized; noninteractive execution requires it.
3. After success, run `district update --dry-run --json`, inspect the active dashboard and advancing `/api/fleet` revision (a valid empty fleet needs no advance), and confirm its bind and Factory schedules are unchanged.
4. On a failed transaction, report the helper's explicit rollback result and log path. Use `district update --rollback --yes` to restore the recorded previous installation offline. If the launcher is broken, use the recovery command printed by District (normally `/usr/bin/python3 ~/.local/state/district/update/transaction.py recover ~/.local/state/district/update`).
5. Self-update supports a non-editable official `uv tool` installation, including a clean official local snapshot whose installed package matches it. Other package managers require migration first. To bootstrap an older CLI, run `uv run python -m district update --to SHA --yes` from a checkout containing the updater, after reviewing its dry run.

State, retained installation artifacts, helper, and transaction logs live under `${XDG_STATE_HOME:-~/.local/state}/district/update`. Keep this directory for offline rollback and interrupted-transaction recovery. Recovery requires confirmation through the CLI; the printed direct helper command is an explicit recovery action and refuses to race another District operation.

Done when installed provenance and CLI match the reported SHA, a formerly active dashboard has a replacement process and current collection evidence, only formerly active District timers are restored, and no Factory unit or registry setting changed.

## Ownership invariants

- Use `district apply` for generated host service definitions; host files are District-owned.
- Keep `[[gate.check]]`, `[leak_scan]`, `[repo].upstream`, the issue template, and lessons committed in each managed repository.
- Keep triage, workers, worker_wrap, reviewer, manager, dashboard port, timer cadence, bind host, gate lock, package-age policy, and the managed-repo registry in the host config.
- District shells out to `factory`; diagnose per-repository pipeline behavior with the `factory` skill rather than reproducing it here.
