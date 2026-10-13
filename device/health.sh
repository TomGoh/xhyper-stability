#!/system/bin/sh
# health.sh [marker]: one health snapshot of an Android device, run as root (`su 0 sh health.sh`).
# Prints key=value lines first (parsed by host/stab.py), then raw sections between "--- <name>" markers.
# Everything here is read-only.

kv() { echo "$1=$2"; }
# Optional marker: written to the kernel log, so it also appears on the serial console and aligns the two timelines.
[ -n "${1:-}" ] && echo "STAB $1" > /dev/kmsg
kv boot_id "$(cat /proc/sys/kernel/random/boot_id)"
kv uptime "$(cut -d' ' -f1 /proc/uptime)"
kv epoch "$(date +%s)"
kv boot_completed "$(getprop sys.boot_completed)"
kv boot_reason "$(getprop sys.boot.reason)"
kv boot_reason_last "$(getprop persist.sys.boot.reason.history | head -1)"
kv bootloader_reason "$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^androidboot.bootreason=//p' | head -1)"
for f in success fail failed_freeze failed_suspend failed_resume last_failed_dev last_failed_step; do
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
kv dmesg_last "$(dmesg | tail -1 | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p')"

# Kernel lines that matter for stability (lockups, oopses, watchdogs, RCU stalls, hung tasks, warnings).
# Device inventory: compared by the host after every reboot and resume (a driver that failed to probe or to resume
# under the hypervisor drops out of these lists).
echo "--- inventory"
for d in /sys/bus/pci/devices/*; do [ -e "$d" ] && echo "pci ${d##*/} $(cat $d/vendor 2>/dev/null):$(cat $d/device 2>/dev/null)"; done
for d in /sys/bus/usb/devices/*; do [ -e "$d/idVendor" ] && echo "usb $(cat $d/idVendor):$(cat $d/idProduct)"; done
sed -n 's/^ *\([0-9]*\) \[\(.*\)\].*/sound \1 \2/p' /proc/asound/cards 2>/dev/null
for d in /sys/class/net/*; do echo "net ${d##*/}"; done
sed -n 's/^N: Name="\(.*\)"/input \1/p' /proc/bus/input/devices 2>/dev/null
echo "--- kernel_bad"
dmesg | grep -E 'BUG:|Oops|Unable to handle|soft lockup|hard LOCKUP|rcu.*(stall|self-detected)|blocked for more than|Kernel panic|WARNING: CPU|---\[ end trace|watchdog|preemptoff_warn|Unhandled|SError|synchronous external abort' | tail -60
echo "--- crash_buffer"
logcat -b crash -d -t 200 2>/dev/null | grep -E 'FATAL EXCEPTION|Fatal signal|Process: |>>> ' | tail -40
echo "--- events"
logcat -b events -d -t 2000 2>/dev/null | grep -E 'am_anr|am_crash|am_wtf|watchdog' | tail -20
echo "--- end"
