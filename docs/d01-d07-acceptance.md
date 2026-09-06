# D01 / D07 ordered integration acceptance

Recorded 2026-09-05. This is evidence for the existing Atlas/CLI and LAN boundary,
not acceptance of the later operations console, a production deployment, or
permission to release held tickets.

## Heads and order

1. Merge [D01 #23 / PR #33](https://github.com/mikeroySoft/district/pull/33)
   first: `535c9f78a4330c81d3303a4ce1c62daf642f1e74`.
2. Merge [D07 #24 / PR #32](https://github.com/mikeroySoft/district/pull/32)
   second. D07 owns the reconciliation, starting from
   `f670220803622b32a12f53f1561a9c7afa715202` and incorporating that exact D01
   commit in merge commit `b1ec64552511e5899a153c56fa23f53c297ef599`.
3. Initial tested reconciliation source:
   `614141684e7dadc3fcdc727999fa8f4c89e75adf`. Independent security review of
   candidate `fe8833b96ea6b64f7ea2cb86690e802d7df096f1` then identified the
   quoted/multiline diagnostic issue recorded below.
4. Final tested security correction:
   `3d199b9df73391e7aa325f640ccc35f0a90ebfbd`. The following evidence commit
   changes only this document and receipts. Review the actual final PR head,
   not an earlier worker result or the superseded initial candidate.

Both heads started from remote main
`d2606020374df127e4b672434c3b2a445d1ae000`. They had a semantic conflict even
though Git merged them without a textual conflict: D07's old allowlist removed
D01's classifier fields and supplied legacy `health: failing`.

The public projection now preserves the D01 classification without another
classifier or legacy fallback. The projection and omissions are documented in
[spec §6.2](../OPERATIONS-CONSOLE-SPEC.md#62-d01d07-public-projection-and-integration-order).
Read policy also permits an ordinary top-level link to the direct numeric-IP
root document without permitting cross-site API reads. Read-only detection
shares existing-clone and port-selection decisions with the CLI. Authorized
child diagnostics are sanitized and bounded.

## Provenance and limits

All execution was in detached temporary worktrees, principally
`/tmp/district-d07-integration`. The user's divergent main checkout and its
tracked/untracked edits were not reset, cleaned, rebased or used for integration.
No installed Factory/District package, production registry, service, setting or
deployment was changed. Ticket locks 23 and 24 were held during branch work;
coordination and the chosen ownership were recorded on both issues and PRs.

The classification scenarios below use a **controlled normalized source
adapter**, a temporary registry and a fixed evaluation clock, not invented
claims about live factories or an assumed newer installed Factory. The actual
District CLI, shared classifier, HTTP handlers, sanitizer, Atlas generator and
browser run unchanged against that adapter. The adapter supplies collection
inputs only; it does not replace the classifier or authorization policy. The
old-Factory/incomplete-source path is separately covered by regression tests.

For management, the actual District subprocess and reconciliation code ran
against `acceptance/disposable` in a second temporary registry. External
`factory`, `systemctl` and `gh` commands were recording executables in a temporary
PATH, not production services or live GitHub writes. Cap removal was checked by
loading the resulting registry, not merely by observing a successful HTTP code.
These controlled effects demonstrate the authorized path; they do not establish
D08's complete management workflow, conflict handling, or production mutation
acceptance.

## Complete gate

In the integrated worktree:

```text
uv run python -m unittest discover -s tests
Ran 93 tests in 11.198s
OK
```

New checks defend public classification, null-versus-zero semantics, preserved
identities/source timestamps, explicit truncation, credential/config/log
redaction, Inspector projection, valid/invalid detection and authorization of
actual HTTP requests. The gate uses local disposable fixtures. It neither
substitutes for the network/browser evidence below nor upgrades an installed
engine. The final run set `UV_NO_CONFIG=1` and
`UV_EXCLUDE_NEWER=2026-09-04T16:56:15Z`, preserving the existing dependency lock's
release-age setting. No dependency or version changed in this reconciliation.

## CLI → API → browser semantics

The actual executable was run twice per scenario, against that scenario's
one-entry registry:

```text
/tmp/district-d07-integration/.venv/bin/district status
/tmp/district-d07-integration/.venv/bin/district status --json
```

Both forms returned the same exit code. Each four-dimensional classification
then matched `/api/fleet` and its rendered, keyboard-opened Atlas inspector.
The final fleet page reported **3 normal, 3 attention, 4 unknown**. Missing
project metrics displayed `?`, not fabricated zero measurements.

| Scenario | Assessment | Operating | Execution | Observation | Exit | Inspector |
|---|---|---|---|---|---:|---|
| Scheduled | normal | scheduled waiting | known wait | fresh | 0 | [image](evidence/d01-d07/inspector-scheduled.png) |
| Deliberately paused | normal | deliberately paused | known wait | fresh | 0 | [image](evidence/d01-d07/inspector-paused.png) |
| Failure-capped | attention | capped | known wait | fresh | 1 | [image](evidence/d01-d07/inspector-capped.png) |
| Unexpectedly stopped | attention | unexpectedly stopped | unknown | fresh | 1 | [image](evidence/d01-d07/inspector-stopped.png) |
| Stale source | unknown | scheduled waiting | known wait | stale | 2 | [image](evidence/d01-d07/inspector-stale.png) |
| Partial collection | unknown | running | stage-active | partial | 2 | [image](evidence/d01-d07/inspector-partial.png) |
| Unavailable source | unknown | unknown | unknown | unavailable | 2 | [image](evidence/d01-d07/inspector-unavailable.png) |
| Explicit unknown state | unknown | unknown | unknown | fresh | 2 | [image](evidence/d01-d07/inspector-unknown.png) |
| Ordinary project gate failure / parked work | normal | scheduled waiting | failed | fresh | 0 | [image](evidence/d01-d07/inspector-project.png) |
| Broken execution mechanism | attention | running | failed | fresh | 1 | [image](evidence/d01-d07/inspector-mechanism.png) |

The project scenario has no operational finding. The mechanism scenario has
`runtime.mechanism_unavailable`; cap and stop scenarios respectively have
`scheduling.capped` and `scheduling.unexpected_stop`. Source rows, scoped findings,
reported ownership and unknowns remain visible. See [CLI receipt](evidence/d01-d07/cli.json),
[API/browser receipt](evidence/d01-d07/browser.json) and
[fleet screenshot](evidence/d01-d07/fleet.png); individual text outputs are alongside
them as `cli-<scenario>.txt`.

A second read advanced only the evaluation clock by 11 seconds while preserving
the source's five-second cadence and exact `observed_at`. The source age became
11, observation became stale and assessment unknown; the timestamp did not
renew on read. See [reread receipt](evidence/d01-d07/source-reread.json).

## Genuine non-loopback boundary

A throwaway Python harness was run with:

```text
unshare --user --map-root-user --net \
  /tmp/district-d07-integration/.venv/bin/python \
  /tmp/district-d01-d07-acceptance/network.py
```

It created a veth pair inside the disposable user/network namespace, put the
client in a **second** network namespace, and used:

- server: `198.18.0.1:18975`, actual dashboard bound to `0.0.0.0`;
- client: `198.18.0.2`, an actual non-loopback peer on that TCP connection;
- no forwarding proxy and no production host-interface changes.

`GET /`, `/api/fleet` and `/api/capabilities` all returned 200. Capabilities
reported `manage: false`; the payload/page contained no credential canary.
The client tried add, apply, upgrade, reset and remove, plus future/unimplemented
POST/PUT/DELETE/PATCH/PURGE mutation routes. Across six authority/header
variants per case, **all 60 requests returned 403**. Variants included correctly
formed local-style Origin/custom headers, spoofed loopback Host, `Forwarded`,
`X-Forwarded-*` and `X-Real-IP`. Local-path detect returned 403. The command log
was unchanged: **zero CLI mutations**.

```text
NETWORK ACCEPTANCE PASS: genuine remote peer; zero CLI mutations
```

The [network receipt](evidence/d01-d07/network.json) records addresses, binding,
read sizes and denial counts. This proof is not a loopback request with a
forged Host header.

Separately, Chromium viewed the wildcard listener using the host's actual LAN
URL, `http://10.0.10.99:18973/`. The visible page said read-only, explained the
loopback/proxy boundary, and disabled every management/detect control. All ten
factory inspectors were opened through their focusable SVG nodes. Factory
navigation used the LAN hostname, registered port and `/#ops`, with a new tab
and `noopener`, not the viewer's localhost. See
[read-only controls](evidence/d01-d07/lan-readonly.png) and the browser receipt.

## Trusted-local boundary and diagnostics

Real direct loopback HTTP requests exercised both bindings:

| Server binding | Access URL | Unauthorized cases | Authorized reset |
|---|---|---:|---|
| `127.0.0.1:18974` (default) | `http://127.0.0.1:18974` | 32 × 403 | 200, subprocess exit 0 |
| `0.0.0.0:18976` (wildcard) | `http://127.0.0.1:18976` | 32 × 403 | 200, subprocess exit 0 |

Missing/wrong custom header, missing/cross-site Origin, invalid Host and
forwarding headers could not invoke the command. Correct Host, exact Origin
and `X-District-Act: 1` allowed `apply --reset acceptance/disposable`. Each reset
cleared the cap in the temporary registry and invoked only recording external
commands. See [receipt](evidence/d01-d07/trusted-local.json) and the two saved
`local-reset-<port>.txt` outputs.

Chromium also clicked **Reset capped timer** through the trusted-local page.
Its confirmation named exactly `district apply --reset acceptance/disposable`;
the output showed the real `[exit 0]` after the cap-clear result, and the
registry postcondition was checked. See [browser reset](evidence/d01-d07/local-reset-browser.png)
and its [confirmation/output receipt](evidence/d01-d07/local-reset-browser.json).

Browser Detect on the existing disposable GitHub clone reported **adopt**, the
same selected port (`19020`) as the CLI, and check names/exclusivity without
commands, paths or setting values. It did not run doctor. See
[Detect screenshot](evidence/d01-d07/local-detect.png) and
[receipt](evidence/d01-d07/detect-browser.json).
Authenticated detection of a file, an option, a foreign URL and a
credential-bearing GitHub URL returned bounded 400 errors without echoing the
target. See [invalid-target receipt](evidence/d01-d07/detect-invalid.json).

An additional source deliberately supplied HTML-looking mechanism text and a
credential canary. The Inspector displayed markup literally, created zero
injected image nodes and published no canary in the DOM or API. See
[malicious-text screenshot](evidence/d01-d07/malicious-text.png) and
[receipt](evidence/d01-d07/malicious-text.json). Regression requests additionally
exercise oversized output, private-key blocks and output-record truncation.
Pattern redaction is defense in depth, not a claim that arbitrary unmarked
secret prose can be recognized perfectly.

### Independent-review diagnostic correction

Independent security review found that quoted JSON/TOML keys could bypass
assignment redaction, escaped quotes could end masking early, and the following
lines of a TOML multiline value could leak from the actual adopt diff producer
(`add.main` → `_stream`). These are marked configuration/credential fields,
not the arbitrary-unmarked-prose limitation. Three regression tests produced
four failures before the correction (including both TOML multiline delimiters)
and passed afterward:

```text
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_projection.ProjectionTest.test_private_evidence_is_redacted_without_losing_identity_or_assessment \
  test_dashboard_policy.DashboardPolicyTest.test_stream_bounds_sanitizes_and_preserves_real_exit \
  test_dashboard_policy.DashboardPolicyTest.test_stream_withholds_multiline_configuration_until_exit
Ran 3 tests in 1.050s
OK
```

The shared sanitizer now masks the full assignment tail, recognizing quoted
keys without trying to parse escaped values. Only quoted generic keys accept
colon assignment, so ordinary identities such as `stage:gate` are unchanged.
After an oversized line or triple-quoted configuration marker, streaming drains
but withholds all remaining diagnostics and still reports the actual child exit.
This deliberately sacrifices trailing diagnostic detail rather than guessing
where private data ends. Source timestamp handling is untouched.

The final actual HTTP/browser smoke supplied a quoted password containing an
escaped quote in a source error: no canary reached API or DOM
([receipt](evidence/d01-d07/quoted-error-browser.json),
[Inspector image](evidence/d01-d07/quoted-error-browser.png)).
An authorized **real `district add --no-edit` adoption** of the disposable repo
then lifted quoted and multiline configuration to the temporary host registry.
The private values survived in the registry, disappeared from the repo config,
were absent from streamed diagnostics, and the actual command exited 0
([adopt receipt](evidence/d01-d07/quoted-adopt.json)). External commands still
used recording executables, not production services or GitHub writes.
The genuine two-namespace refusal run was repeated on this source revision and
passed unchanged; the restored original ten-scenario API matrix still matched
the CLI receipt ([final matrix check](evidence/d01-d07/final-matrix-reread.json)).

## Readiness and holds

The semantic conflict and the specific D01/D07 acceptance gaps above are
resolved in the combined implementation. **Fresh independent review is still
required on the actual heads**, with merge order D01 then D07. Automated gate
success or this evidence is not self-approval.

Nothing was merged or deployed. No `factory-approved` label was applied and no
`factory-held` label removed. **D02 #25 and every later console/UI ticket remain
held**. Production upgrade/verification, live evidence unavailable from older
Factory installations, and acceptance belonging to later tickets are not
silently waived by this reconciliation.
