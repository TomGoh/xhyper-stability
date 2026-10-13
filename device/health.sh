#!/system/bin/sh
# health.sh [marker [since boot_id]]: one health snapshot of an Android device, run as root (`su 0 sh health.sh`).
# Prints key=value lines first (parsed by host/stab.py), then raw sections between "--- <name>" markers.
# Everything here is read-only. since, boot_id: dmesg_last and boot_id of the previous snapshot; on that same boot the
# kernel section holds every matching line at or after `since` (the newest KCAP of them), otherwise the last 200.
# The kernel log is read once into a temporary copy; without a usable copy (/data full or unwritable) every use reads
# it again and the kernel section is the last 200 lines (kernel_src=pipe; kernel_src=last200 when awk failed).
# Test hooks (the host's unit tests run this script on a synthetic log): STAB_DMESG = a file read instead of dmesg,
# STAB_BOOT_ID = the boot id.
KCAP=2000
KPAT='BUG:|Oops|Unable to handle|soft lockup|hard LOCKUP|rcu.*(stall|self-detected)|blocked for more than|Kernel panic|WARNING: CPU|---\[ end trace|watchdog|preemptoff_warn|irqsoff_warn|Resume caused by|Wakeup pending|Unhandled|SError|synchronous external abort'
CONT='(disabled|enabled) (preempt|IRQ) at:'

kv() { echo "$1=$2"; }
# Optional marker: written to the kernel log, so it also appears on the serial console and aligns the two timelines.
[ -n "${1:-}" ] && echo "STAB $1" > /dev/kmsg
tmp="${TMPDIR:-/data/local/tmp}/stab-health.$$"
trap 'rm -f "$tmp".*' EXIT
trap 'exit 1' HUP INT TERM  # a probe killed on its timeout still removes its copy of the kernel log
raw() { if [ -n "${STAB_DMESG:-}" ]; then cat "$STAB_DMESG"; else dmesg; fi; }
{ raw > "$tmp.dmesg"; } 2>/dev/null
klog() { if [ -s "$tmp.dmesg" ]; then cat "$tmp.dmesg"; else raw; fi; }
boot="${STAB_BOOT_ID:-$(cat /proc/sys/kernel/random/boot_id)}"
kv boot_id "$boot"
kv uptime "$(cut -d' ' -f1 /proc/uptime)"
kv epoch "$(date +%s)"
kv boot_completed "$(getprop sys.boot_completed)"
kv boot_reason "$(getprop sys.boot.reason)"
kv boot_reason_last "$(getprop persist.sys.boot.reason.history | head -1)"
kv bootloader_reason "$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^androidboot.bootreason=//p' | head -1)"
for f in success fail failed_freeze failed_prepare failed_suspend failed_suspend_late failed_suspend_noirq \
         failed_resume_noirq failed_resume_early failed_resume last_failed_dev last_failed_step; do
  kv "suspend_$f" "$(cat /sys/power/suspend_stats/$f 2>/dev/null)"
done
# Tablet vs desktop (PC) mode on the Kylin tablets: the rotation state saved on entering PC mode is 1 while in PC mode
# (seen 1 in PC mode and 0 after switching back, 2026-10-13), and the freeform "Desk" task is visible in PC mode.
kv pc_mode "$(settings get system kylin_rotation_state_saved_before_pc_mode 2>/dev/null)"
kv desk_visible "$(dumpsys window 2>/dev/null | grep -m1 'name=Desk ' | grep -c 'visible=true')"
kv wakefulness "$(dumpsys power 2>/dev/null | sed -n 's/.*mWakefulness=\([A-Za-z]*\).*/\1/p' | head -1)"
kv loadavg "$(cut -d' ' -f1-3 /proc/loadavg)"
kv mem_avail_kb "$(sed -n 's/^MemAvailable: *\([0-9]*\).*/\1/p' /proc/meminfo)"
kv cpus_online "$(cat /sys/devices/system/cpu/online)"
for p in /sys/devices/system/cpu/cpufreq/policy*; do
  kv "$(basename $p)_min_max_cur" "$(cat $p/scaling_min_freq)/$(cat $p/scaling_max_freq)/$(cat $p/scaling_cur_freq)"
done
kv therm_max_mc "$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)"
kv battery "$(cat /sys/class/power_supply/battery/capacity 2>/dev/null)% $(cat /sys/class/power_supply/battery/status 2>/dev/null)"
kv tombstones "$(ls /data/tombstones 2>/dev/null | grep -c '^tombstone_')"
kv anr_files "$(ls /data/anr 2>/dev/null | wc -l)"
# Crash records Android keeps in dropbox (app/native crashes, ANRs, watchdog and pre-watchdog reports, tombstones).
kv dropbox_crash "$(ls /data/system/dropbox 2>/dev/null | grep -cE 'crash|anr|watchdog|tombstone')"
kv pstore "$(ls /sys/fs/pstore 2>/dev/null | tr '\n' ' ')"
# Panic/oops records kept across reboots, with their write time: a new name or a new time is a new crash record.
kv pstore_dumps "$(for f in /sys/fs/pstore/dmesg-*; do [ -e "$f" ] && printf '%s:%s ' "${f##*/}" "$(stat -c %Y "$f")"; done)"
# The newest stamped line: a multi-line record prints its later lines without a time stamp.
kv dmesg_last "$(klog | grep '^\[ *[0-9]' | tail -n 1 | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p')"
# What woke the device or aborted its last suspend: the newest three resume/abort lines of the kernel log (with their
# kernel time, so the host can tell new ones from old), then the kernel's own record where it has one.
wr="$(klog | grep -E 'Resume caused by|Wakeup pending|Abort' | tail -n 3 | awk 'NR > 1 { printf " | " } { printf "%s", $0 }')"
[ -r /sys/kernel/wakeup_reasons/last_resume_reason ] &&
  wr="$wr | last_resume_reason $(tr '\n' ' ' < /sys/kernel/wakeup_reasons/last_resume_reason)"
kv wakeup_reason "$wr"
# Kernel lines that matter for stability (lockups, oopses, watchdogs, RCU stalls, hung tasks, warnings, preempt-off and
# IRQ-off reports without their '... at:' lines) and the resume lines. Same boot: all of them since the last snapshot
# (one tee step printed ~400 between two snapshots); kernel_new > KCAP tells the host that older ones were cut.
src=pipe
if [ -s "$tmp.dmesg" ]; then
  src=file
  grep -E "$KPAT" "$tmp.dmesg" | grep -vE "$CONT" > "$tmp.all"
  if [ -n "${2:-}" ] && [ "${3:-}" = "$boot" ]; then
    if awk -v s="$2" '{ t = $0; sub(/^\[ */, "", t); sub(/\].*/, "", t); if (t + 0 >= s + 0) print }' "$tmp.all" \
        > "$tmp.new" 2>/dev/null; then
      kv kernel_new "$(wc -l < "$tmp.new" | tr -d ' ')"
      tail -n "$KCAP" "$tmp.new" > "$tmp.kernel"
    else
      src=last200; tail -n 200 "$tmp.all" > "$tmp.kernel"
    fi
  else
    tail -n 200 "$tmp.all" > "$tmp.kernel"
  fi
fi
kv kernel_src "$src"

# Device inventory: compared by the host after every reboot and resume (a driver that failed to probe or to resume
# under the hypervisor drops out of these lists).
echo "--- inventory"
for d in /sys/bus/pci/devices/*; do [ -e "$d" ] && echo "pci ${d##*/} $(cat $d/vendor 2>/dev/null):$(cat $d/device 2>/dev/null)"; done
for d in /sys/bus/usb/devices/*; do [ -e "$d/idVendor" ] && echo "usb $(cat $d/idVendor):$(cat $d/idProduct)"; done
sed -n 's/^ *\([0-9]*\) \[\(.*\)\].*/sound \1 \2/p' /proc/asound/cards 2>/dev/null
for d in /sys/class/net/*; do echo "net ${d##*/}"; done
sed -n 's/^N: Name="\(.*\)"/input \1/p' /proc/bus/input/devices 2>/dev/null
echo "--- kernel_bad"
if [ "$src" = pipe ]; then raw | grep -E "$KPAT" | grep -vE "$CONT" | tail -n 200; else cat "$tmp.kernel"; fi
echo "--- crash_buffer"
logcat -b crash -d -t 200 2>/dev/null | grep -E 'FATAL EXCEPTION|Fatal signal|Process: |>>> ' | tail -40
echo "--- events"
logcat -b events -d -t 2000 2>/dev/null | grep -E 'am_anr|am_crash|am_wtf|watchdog' | tail -20
echo "--- end"
