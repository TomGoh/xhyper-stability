#!/system/bin/sh
# el2log.sh <dir> [interval_s]: the EL2 parallel log. Run as root, detached (nohup ... &); one instance per boot.
# Every interval appends one block to <dir>/<boot_id>.log:
#   T <boottime_s> <epoch_s> <policy cur freq kHz, space separated> on=<online CPU list>
#   <cpu_id> <el2_cycles> <timestamp_ns>        (one line per CPU, straight from /proc/xcore_stats)
# A CPU missing from a block is one the driver could not sample (latched lifecycle error or offline). The loop keeps
# running across suspend (it is frozen with everything else) and stops at reboot; host/stab.py starts it again on
# every boot. <dir>/el2log.pid holds the loop's pid.
D=${1:?dir}; I=${2:-1}
mkdir -p "$D"
B=$(cat /proc/sys/kernel/random/boot_id)
echo $$ > "$D/el2log.pid"
F="$D/$B.log"
while :; do
  f=""
  for p in /sys/devices/system/cpu/cpufreq/policy*; do f="$f $(cat $p/scaling_cur_freq)"; done
  echo "T $(cut -d' ' -f1 /proc/uptime) $(date +%s)$f on=$(cat /sys/devices/system/cpu/online)" >> "$F"
  tail -n +2 /proc/xcore_stats >> "$F" 2>&1
  sleep "$I"
done
