#!/usr/bin/env bash
# TEMPORARY until factory #467/#469 ship; remove then.
# (remove: district-tmp-reaper-temp.{service,timer} in ~/.config/systemd/user, this script, its log)
# Deletes /tmp/factory-mutation-* and orphaned /tmp/tmp* mkdtemp dirs owned by the
# current user when nothing inside changed in AGE_MIN minutes and no process has them open.
# Env: DRY_RUN=1 lists only. AGE_MIN (default 1440 = 24h). DETAIL_LOG=file appends
# "path<TAB>inodes<TAB>bytes<TAB>action" per candidate. REAPER_TMP (default /tmp) and REAPER_LOG
# (default below) exist for tests only.
# Deployed by hand on rocm-tank as ~/.local/share/district/roots/tmp-reaper-temp.sh with
# district-tmp-reaper-temp.{service,timer}; District does not install it.
set -euo pipefail
AGE_MIN="${AGE_MIN:-1440}"
DRY_RUN="${DRY_RUN:-0}"
DETAIL_LOG="${DETAIL_LOG:-}"
TMP_ROOT="${REAPER_TMP:-/tmp}"
LOG="${REAPER_LOG:-${HOME}/.local/share/district/roots/tmp-reaper-temp.log}"
me="$(id -un)"

# Top-level /tmp entries some process holds as cwd, root or open fd.
in_use="$(find /proc/[0-9]*/cwd /proc/[0-9]*/root /proc/[0-9]*/fd -maxdepth 1 -lname "$TMP_ROOT/*" -printf '%l\n' 2>/dev/null \
  | sed -E "s#^(${TMP_ROOT}/[^/]+).*#\\1#" | sort -u || true)"

removed=0; skipped_recent=0; skipped_open=0; failed=0
while IFS= read -r -d '' d; do
  if grep -qxF -- "$d" <<<"$in_use"; then
    skipped_open=$((skipped_open+1)); [ -n "$DETAIL_LOG" ] && printf '%s\t-\t-\tskip-open\n' "$d" >>"$DETAIL_LOG"; continue
  fi
  if [ -n "$(find "$d" -mmin "-$AGE_MIN" -print -quit 2>/dev/null)" ]; then
    skipped_recent=$((skipped_recent+1)); [ -n "$DETAIL_LOG" ] && printf '%s\t-\t-\tskip-recent\n' "$d" >>"$DETAIL_LOG"; continue
  fi
  if [ -n "$DETAIL_LOG" ]; then
    printf '%s\t%s\t%s\t%s\n' "$d" "$(find "$d" 2>/dev/null | wc -l)" "$(du -sb "$d" 2>/dev/null | cut -f1)" \
      "$([ "$DRY_RUN" = 1 ] && echo would-remove || echo removed)" >>"$DETAIL_LOG"
  fi
  if [ "$DRY_RUN" != 1 ]; then
    # Factory review snapshots are left read-only (dr-x------ / d-ws------): give the owner
    # rwx back first, else rm fails and set -e aborted every run from 2026-11-07 04:00.
    chmod -R u+rwX -- "$d" 2>/dev/null || true
    if ! rm -rf --one-file-system -- "$d"; then
      failed=$((failed+1)); [ -n "$DETAIL_LOG" ] && printf '%s\t-\t-\tfailed\n' "$d" >>"$DETAIL_LOG"; continue
    fi
  fi
  removed=$((removed+1))
done < <(find "$TMP_ROOT" -mindepth 1 -maxdepth 1 -type d -user "$me" \( -name 'factory-mutation-*' -o -name 'tmp*' \) -print0)

printf '%s dry_run=%s age_min=%s removed=%s skipped_recent=%s skipped_open=%s failed=%s inodes_tmp=%s\n' \
  "$(date '+%F %T %Z')" "$DRY_RUN" "$AGE_MIN" "$removed" "$skipped_recent" "$skipped_open" "$failed" \
  "$(df -i "$TMP_ROOT" | awk 'NR==2{print $3"/"$2" ("$5")"}')" >>"$LOG"
tail -1 "$LOG"
# Still fail the unit when a dir could not be removed, but only after logging the run.
[ "$failed" -eq 0 ]
