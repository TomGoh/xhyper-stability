#!/system/bin/sh
# rundown.sh <csv> [sample_s] [package]: battery rundown sampler, run as root and detached
# (`setsid nohup sh rundown.sh <csv> > /dev/null 2>&1 < /dev/null &`), so it keeps running with nothing on USB.
# Every sample_s seconds (default 60) it appends one CSV line to <csv> and syncs, so the samples up to the automatic
# shutdown survive it, and writes the same line as "RUNDOWN <line>" to the kernel log: a serial capture carries the
# live progress to the host while USB is unplugged. The first line of a new file names the columns:
#   epoch, uptime_s, cap (%), status, online (1 = external power), volt_uV, ocv_uV, cur_uA (negative while
#   discharging), cur_avg_uA, counter_uAh, temp_dC (0.1 C), backlight (raw), player (1 = <package> has a process),
#   f<N>_kHz (current frequency of every cpufreq policy N), vsync_hz, playback (state of <package>'s media session,
#   e.g. PLAYING(3); NONE when it has none)
# sysfs only, except every 10th sample (the first included): the display refresh rate (dumpsys SurfaceFlinger) and the
# playback state (dumpsys media_session), which stay empty in the other lines; this keeps the sampler's own cost
# negligible next to video playback. Battery: the power supply named battery, else the first of type Battery. Open
# circuit voltage and average current come from the battery or any other supply that has them (a separate fuel gauge),
# empty when none has. online: the battery's own online file, else 1 when any other supply is online.
# One sampler per directory: <dir of csv>/rundown.pid holds its pid; remove the file to stop it.
L=${1:?csv}; I=${2:-60}; PKG=${3:-org.videolan.vlc}
P=$(dirname "$L")/rundown.pid
[ -f "$P" ] && kill -0 "$(cat "$P")" 2>/dev/null && { echo "already running: $(cat "$P")"; exit 1; }
echo $$ > "$P"
S=/sys/class/power_supply
B=$S/battery
[ -d "$B" ] || for d in $S/*; do [ "$(cat $d/type 2>/dev/null)" = Battery ] && { B=$d; break; }; done
first() { for f in "$@"; do [ -r "$f" ] && { echo "$f"; return; }; done; }
OCV=$(first $B/voltage_ocv $S/*/voltage_ocv); AVG=$(first $B/current_avg $S/*/current_avg)
BL=$(ls -d /sys/class/backlight/* 2>/dev/null | head -1)
POL=$(ls -d /sys/devices/system/cpu/cpufreq/policy* 2>/dev/null)
rd() { [ -n "$1" ] && cat "$1" 2>/dev/null; }
online() {
  if [ -r $B/online ]; then cat $B/online; return; fi
  o=0; for d in $S/*; do [ "$d" != "$B" ] && [ "$(cat $d/online 2>/dev/null)" = 1 ] && o=1; done; echo $o
}
if [ ! -s "$L" ]; then
  h="epoch,uptime_s,cap,status,online,volt_uV,ocv_uV,cur_uA,cur_avg_uA,counter_uAh,temp_dC,backlight,player"
  for p in $POL; do h="$h,f${p##*policy}_kHz"; done
  echo "$h,vsync_hz,playback" > "$L"
fi
n=0
while [ -f "$P" ] && [ "$(cat "$P" 2>/dev/null)" = $$ ]; do
  hz=""; st=""
  if [ $((n % 10)) = 0 ]; then
    hz=$(dumpsys SurfaceFlinger 2>/dev/null | grep -m1 -oE 'vsyncRate=[0-9.]+' | cut -d= -f2)
    st=$(dumpsys media_session 2>/dev/null | awk -v p="package=$PKG" 'index($0, p) { f = 1; next }
      f && /package=/ { f = 0 }
      f && s == "" && match($0, /state=[A-Z_]+\([0-9]+\)/) { s = substr($0, RSTART + 6, RLENGTH - 6) }
      END { if (NR) print (s == "" ? "NONE" : s) }')
  fi
  pl=0; pidof $PKG > /dev/null && pl=1
  f=""; for p in $POL; do f="$f,$(cat $p/scaling_cur_freq 2>/dev/null)"; done
  line="$(date +%s),$(cut -d' ' -f1 /proc/uptime),$(cat $B/capacity),$(cat $B/status),$(online),$(cat $B/voltage_now),$(rd "$OCV"),$(cat $B/current_now),$(rd "$AVG"),$(cat $B/charge_counter 2>/dev/null),$(cat $B/temp 2>/dev/null),$(rd "$BL/actual_brightness"),$pl$f,$hz,$st"
  echo "$line" >> "$L"
  sync
  echo "RUNDOWN $line" > /dev/kmsg
  n=$((n + 1))
  sleep "$I"
done
