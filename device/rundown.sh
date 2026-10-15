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

# ---- arguments and one sampler per directory -------------------------------------------------------------------------
CSV=${1:?csv}                     # the CSV file samples are appended to
SAMPLE_S=${2:-60}                 # seconds between samples
PKG=${3:-org.videolan.vlc}        # the video player whose process and media session are sampled
PID_FILE=$(dirname "$CSV")/rundown.pid
[ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null && {
  echo "already running: $(cat "$PID_FILE")"
  exit 1
}
echo $$ > "$PID_FILE"

# ---- files sampled ---------------------------------------------------------------------------------------------------
SUPPLIES=/sys/class/power_supply
BAT=$SUPPLIES/battery   # the battery's power supply directory
[ -d "$BAT" ] || for d in $SUPPLIES/*; do
  [ "$(cat $d/type 2>/dev/null)" = Battery ] && { BAT=$d; break; }
done
# first <file...>: the first readable file of the list (nothing when none is).
first() {
  for f in "$@"; do
    [ -r "$f" ] && { echo "$f"; return; }
  done
}
OCV=$(first $BAT/voltage_ocv $SUPPLIES/*/voltage_ocv)   # open circuit voltage file, or empty
AVG=$(first $BAT/current_avg $SUPPLIES/*/current_avg)   # average current file, or empty
BACKLIGHT=$(ls -d /sys/class/backlight/* 2>/dev/null | head -1)
POLICIES=$(ls -d /sys/devices/system/cpu/cpufreq/policy* 2>/dev/null)

# ---- helpers ---------------------------------------------------------------------------------------------------------
# rd <file>: the file's content; nothing when the name is empty or the file cannot be read.
rd() { [ -n "$1" ] && cat "$1" 2>/dev/null; }
# online: 1 when external power is connected (see the header), else 0.
online() {
  if [ -r $BAT/online ]; then cat $BAT/online; return; fi
  any_online=0
  for d in $SUPPLIES/*; do
    [ "$d" != "$BAT" ] && [ "$(cat $d/online 2>/dev/null)" = 1 ] && any_online=1
  done
  echo $any_online
}

# ---- header line (new file only) -------------------------------------------------------------------------------------
if [ ! -s "$CSV" ]; then
  header="epoch,uptime_s,cap,status,online,volt_uV,ocv_uV,cur_uA,cur_avg_uA,counter_uAh,temp_dC,backlight,player"
  for p in $POLICIES; do
    header="$header,f${p##*policy}_kHz"
  done
  echo "$header,vsync_hz,playback" > "$CSV"
fi

# ---- sampling loop: until the pid file is removed or names another sampler -------------------------------------------
sample=0   # number of this sample, from 0
while [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null)" = $$ ]; do
  vsync=""
  playback=""
  if [ $((sample % 10)) = 0 ]; then
    vsync=$(dumpsys SurfaceFlinger 2>/dev/null | grep -m1 -oE 'vsyncRate=[0-9.]+' | cut -d= -f2)
    # The first "state=NAME(n)" in the package's own session (from the line naming the package to the next package
    # line); NONE when the package has no session, empty when dumpsys printed nothing.
    playback=$(dumpsys media_session 2>/dev/null | awk -v p="package=$PKG" 'index($0, p) { f = 1; next }
      f && /package=/ { f = 0 }
      f && s == "" && match($0, /state=[A-Z_]+\([0-9]+\)/) { s = substr($0, RSTART + 6, RLENGTH - 6) }
      END { if (NR) print (s == "" ? "NONE" : s) }')
  fi
  player=0
  pidof $PKG > /dev/null && player=1
  freqs=""   # ",<kHz>" per cpufreq policy
  for p in $POLICIES; do
    freqs="$freqs,$(cat $p/scaling_cur_freq 2>/dev/null)"
  done
  line="$(date +%s),$(cut -d' ' -f1 /proc/uptime),$(cat $BAT/capacity),$(cat $BAT/status),$(online)"
  line="$line,$(cat $BAT/voltage_now),$(rd "$OCV"),$(cat $BAT/current_now),$(rd "$AVG")"
  line="$line,$(cat $BAT/charge_counter 2>/dev/null),$(cat $BAT/temp 2>/dev/null),$(rd "$BACKLIGHT/actual_brightness")"
  line="$line,$player$freqs,$vsync,$playback"
  echo "$line" >> "$CSV"
  sync
  echo "RUNDOWN $line" > /dev/kmsg
  sample=$((sample + 1))
  sleep "$SAMPLE_S"
done
