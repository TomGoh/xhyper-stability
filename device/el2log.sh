#!/system/bin/sh
# el2log.sh <dir> [interval_s]: the EL2 parallel log. Run as root, detached (nohup ... &); one instance per boot.
# Every interval appends one block to <dir>/<boot_id>.log:
#   T <boottime_s> <epoch_s> <policy cur freq kHz, space separated> on=<online CPU list>
#   <cpu_id> <el2_cycles> <timestamp_ns>        (one line per CPU, straight from /proc/xcore_stats)
# A CPU missing from a block is one the driver could not sample (latched lifecycle error or offline). The loop keeps
# running across suspend (it is frozen with everything else) and stops at reboot; host/stab.py starts it again on
# every boot. <dir>/el2log.pid holds the loop's pid.

# ---- arguments and files ---------------------------------------------------------------------------------------------
DIR=${1:?dir}                                # output directory
INTERVAL=${2:-1}                             # seconds between blocks
mkdir -p "$DIR"
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)
echo $$ > "$DIR/el2log.pid"
LOG="$DIR/$BOOT_ID.log"                      # this boot's log

# ---- sampling loop: one block per interval, until killed or reboot ---------------------------------------------------
while :; do
  freqs=""   # " <kHz>" per cpufreq policy
  for p in /sys/devices/system/cpu/cpufreq/policy*; do
    freqs="$freqs $(cat $p/scaling_cur_freq)"
  done
  echo "T $(cut -d' ' -f1 /proc/uptime) $(date +%s)$freqs on=$(cat /sys/devices/system/cpu/online)" >> "$LOG"
  # Skip the header line of /proc/xcore_stats; errors (e.g. the file missing) go into the log too.
  tail -n +2 /proc/xcore_stats >> "$LOG" 2>&1
  sleep "$INTERVAL"
done
