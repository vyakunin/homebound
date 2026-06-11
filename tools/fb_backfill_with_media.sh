#!/usr/bin/env bash
# Re-harvest the FB Activity Log per-month WITH media, one driver invocation per
# month. Per-month (not --mode full) because:
#   * year-URL scroll caps at ~27/yr (only month granularity loads a full month),
#   * each month's media_zip then stays under MV3's ~5min sendMessage IPC cap
#     (a single full-archive media_zip exceeds it and the channel dies).
#
# Resumable: completed months are recorded in <logdir>/done.txt; re-running skips
# them. Continues past per-month failures (logged) instead of aborting.
#
# Input: a months file with "YYYY-MM <count>" per line (count ignored).
# Output: one ~/Downloads/fb-activity-export-* dir per month (merge afterwards
#   with tools/merge_activity_exports.py, then delete the per-month dirs).
#
# Usage: bash tools/fb_backfill_with_media.sh /tmp/backfill_months.txt
set -u
MONTHS_FILE="${1:?usage: fb_backfill_with_media.sh <months-file 'YYYY-MM count'>}"
PER_MONTH_TIMEOUT="${PER_MONTH_TIMEOUT:-360}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGDIR="${LOGDIR:-/tmp/fb_backfill_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$LOGDIR"
DONE="$LOGDIR/done.txt"; touch "$DONE"
echo "logdir=$LOGDIR  timeout=${PER_MONTH_TIMEOUT}s  months=$(grep -c . "$MONTHS_FILE")"
cd "$ROOT" || exit 1
total=$(grep -c . "$MONTHS_FILE"); n=0
while read -r ym _; do
  [ -z "$ym" ] && continue
  n=$((n+1))
  if grep -qx "$ym" "$DONE"; then echo "[$n/$total] skip $ym (done)"; continue; fi
  Y=${ym%-*}; M=${ym#*-}; M=${M#0}
  echo "[$n/$total $(date +%H:%M:%S)] harvest $ym"
  if timeout "$PER_MONTH_TIMEOUT" uv run --with websockets --no-project python3 \
        tools/fb_activity_log_extension/automation/drive_via_cdp.py \
        --mode iter --year "$Y" --month "$M" --max-items 0 \
        > "$LOGDIR/$ym.log" 2>&1; then
    echo "$ym" >> "$DONE"
    echo "[$n/$total $(date +%H:%M:%S)] ok $ym :: $(grep -oE 'merged=[0-9]+ items' "$LOGDIR/$ym.log" | tail -1)"
  else
    echo "[$n/$total $(date +%H:%M:%S)] FAILED $ym (see $LOGDIR/$ym.log)"
  fi
  sleep 4
done < "$MONTHS_FILE"
echo "[$(date +%H:%M:%S)] backfill complete: $(wc -l < "$DONE")/$total months ok"
