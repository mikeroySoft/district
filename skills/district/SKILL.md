---
name: district
description: Operate District, the host-side fleet manager for Factory installations. Use when the user asks to add, adopt, remove, inspect, reconcile, upgrade, or recover managed factory repositories; collect fleet metrics; or install and open the District dashboard.
---

# District

`district` manages many repositories that each run the `factory` pipeline on one Linux host. The registry and shared settings live in `~/.config/agent-factory/config.toml`. `district --help` lists operations; `district <command> --help` is the source of truth for options.

First run `district --version`. If absent, install it with the first available of:

```sh
uv tool install git+https://github.com/mikeroySoft/district
pipx install git+https://github.com/mikeroySoft/district
python3 -m pip install --user git+https://github.com/mikeroySoft/district
```

Confirm `district --version`, `factory --version`, and authenticated `gh auth status`, then choose the operation:

- Fleet health or a stalled factory → **Inspect**.
- Add or adopt a repository → **Onboard**.
- Converge host state or deploy a Factory update → **Apply**.
- Recover a failure-capped timer → **Reset**.
- Remove a repository from the fleet → **Remove**.
- Refresh aggregate data or serve the fleet UI → **Metrics and dashboard**.

## Inspect

Run `district status`. Exit 0 means no factory is failing; attention rows still need reporting. For diagnosis, run `district status --json` and report each affected slug's `health` and `reasons`. Use the repository's `factory dashboard --json` and `.factory/events.jsonl` only when the fleet record does not identify the cause.

Done when every non-healthy factory has a stated reason and evidence source.

## Onboard

1. Run `district add --dry-run <path-or-github-url>`. Check the resolved slug, clone path, fork parent, dashboard port, proposed gate commands, and checks marked for exclusive hardware.
2. For a new repository, take gate commands from its CI or existing scripts. Pass corrections with repeated `--check NAME=CMD`; pass GPU or other single-tenant checks with `--exclusive name1,name2`.
3. Run `district add <path-or-github-url>`. Existing `.factory.toml` selects adopt mode; District lifts host-owned settings into the registry. New repositories open the proposed config in `$EDITOR` unless `--no-edit` was explicitly requested.
4. Report the printed commit command. District writes onboarding files but deliberately does not commit them.
5. Run `district status` and confirm the new factory is not failing.

Done when the repository is registered, its timer is active, and `factory doctor` has no blocking problem.

## Apply

- `district apply [slug]` reconciles units, labels, doctor state, failure caps, and package-age policy. Omit the slug only when the whole fleet is intended.
- Add `--upgrade` only when the user requests a Factory upgrade. It stops active timers, refuses a dirty `[defaults].factory_source` checkout, reinstalls one snapshot, restarts dashboards, and restores timers.
- Report committed-file drift; leave those files for the repository owner. Apply owns host state only.

Done when every selected row has a version, `doctor OK` or an explained warning, no command failure, and an active timer unless District has deliberately capped it.

## Reset

Read `district status --json` and fix the recorded cause first. Then run:

```sh
district apply --reset owner/repo
```

Reset runs one dispatcher pass. It clears the cap and re-enables the timer only after that pass succeeds.

Done when the command reports `pass succeeded` and `district status` no longer marks the factory failing.

## Remove

Use the exact registered slug from `district status --json`. `district rm <slug>` disables and removes its timer, service, and dashboard unit, then drops the registry entry; repository files remain untouched. If a pass is active, report that fact and use `--wait` only when the user asked to wait for removal. Use `--keep-units` only when the user explicitly wants the registry entry removed while services remain.

Done when District reports the slug removed and a fresh status no longer contains it.

## Metrics and dashboard

- `district metrics [slug]` reads the hourly cache; `--refresh` recollects now.
- `district dashboard --install` writes and enables the District dashboard service plus the hourly metrics timer.
- `district dashboard --no-open` serves once in the foreground; default address is `127.0.0.1:8760`.
- `--host 0.0.0.0` enables read-only LAN viewing, not remote management. A LAN URL is read-only even on the host. For manage/detect, open direct HTTP to numeric loopback or `localhost` with the actual server port (default `http://127.0.0.1:8760`); reverse proxies and forwarded headers are unsupported. The page reports capability and keeps unavailable controls disabled.

## Ownership invariants

- Use `district apply` for generated systemd units; host files are District-owned.
- Keep `[[gate.check]]`, `[leak_scan]`, `[repo].upstream`, the issue template, and lessons committed in each managed repository.
- Keep triage, workers, reviewer, dashboard port, timer cadence, bind host, gate lock, package-age policy, and the managed-repo registry in the host config.
- District shells out to `factory`; diagnose per-repository pipeline behavior with the `factory` skill rather than reproducing it here.
