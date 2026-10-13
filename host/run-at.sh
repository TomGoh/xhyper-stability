#!/bin/bash
# run-at.sh HH:MM <out dir> -- <stab.py run arguments ...>
# Waits until the next HH:MM (local time), then runs `python3 host/stab.py run ... --out <out dir>`. Meant to be started
# detached (`nohup host/run-at.sh 21:00 <out> -- -s <serial> --plan plans/overnight.json ... &`) so the run does not
# depend on the terminal or agent session that scheduled it. Writes <out>.pid (the waiting/running shell), <out>.console
# (stab.py output) and <out>.rc (exit status) next to the output directory; `kill $(cat <out>.pid)` cancels the wait,
# and during the run stab.py turns SIGTERM into a clean stop with summary.md.
set -u
AT=${1:?HH:MM}; OUT=${2:?out dir}; shift 2; [ "${1:-}" = -- ] && shift
cd "$(dirname "$0")/.." || exit 1
echo $$ > "$OUT.pid"
# macOS: keep the host awake (no idle sleep) for as long as this script runs, or the run stops with it.
command -v caffeinate >/dev/null && caffeinate -i -s -w $$ &
now=$(date +%s); target=$(date -j -f '%Y-%m-%d %H:%M' "$(date +%Y-%m-%d) $AT" +%s 2>/dev/null || date -d "today $AT" +%s)
[ "$target" -le "$now" ] && target=$((target + 86400))
echo "$(date '+%F %T') waiting until $(date -r "$target" '+%F %T' 2>/dev/null || date -d "@$target" '+%F %T')" > "$OUT.console"
while [ "$(date +%s)" -lt "$target" ]; do sleep 30; done
echo "$(date '+%F %T') starting" >> "$OUT.console"
python3 host/stab.py run --out "$OUT" "$@" >> "$OUT.console" 2>&1 &
child=$!
trap 'kill -TERM $child 2>/dev/null; wait $child' TERM INT
wait $child
echo $? > "$OUT.rc"
