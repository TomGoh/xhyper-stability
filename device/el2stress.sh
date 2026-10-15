#!/system/bin/sh
# el2stress.sh <step> <seconds> [gateway]: drive one class of exits to the hypervisor (EL2) for <seconds>; run as root.
#
# A guest at EL1/EL0 only enters EL2 on: physical interrupts (device SPIs, the timer and PMU PPIs), IPIs it sends
# (ICC_SGI1R writes trap), GIC distributor/redistributor register accesses, power calls (deep idle CPU_SUSPEND,
# CPU_OFF/ON, system suspend), some system registers and hypercalls. CPU-bound or memory-bound work does not exit
# (Host RAM is mapped in large stage-2 blocks up front), so each step makes many short, frequent events instead.
#
# Steps (one function each; the comment block above each function says what it drives, how, and what it prints):
#   ipi       cross-CPU wake-ups (IPIs)
#   timer     timer expiries
#   idle      idle entry/exit churn
#   traffic   device interrupts: Wi-Fi ping flood to [gateway], storage writes
#   affinity  GIC routing writes, with traffic running
#   hotplug   CPU_OFF/CPU_ON of CPUs 1-7
#   pmu       PMU overflow interrupts
#   radio     Wi-Fi and Bluetooth off/on (driver unload/reload)
#   copy      storage write, read back and compare
#   gpu       GPU load (glmark2)
#   tee       TEE calls (KeyMint in Trusty)
#   all       ipi + timer + idle + affinity (with traffic) + hotplug + copy + gpu at once, each at a reduced size
#
# Arguments: <step> one of the names above; <seconds> how long the step runs; [gateway] the address that traffic,
# affinity and radio ping (empty: no ping).
# Environment: TEE_WORKERS and TEE_CYCLIC (tee step); LIGHT (set by `all` for its parts, see n4).
# Files: stress-ng, hackbench and cyclictest are taken from /data/local/tmp/stab; scratch files go to its tmp/.
# Output: one line per finished part, "EL2STRESS <step> <part> ...", most of them ending in "rc=<n>" (the host counts
# an rc other than 0 as a failure); a stress-ng part also prints the last 4 lines of stress-ng's log; the last line is
# "EL2STRESS_DONE <step>". <step> is the step named on the command line: the parts of `all` print "EL2STRESS all ...".
# An unknown step prints "unknown step <step>" and exits with status 2.

# ---- arguments and paths ---------------------------------------------------------------------------------------------
STEP=${1:?step}                  # the step to run: the name of one of the step functions below
DURATION=${2:?seconds}           # how long the step runs, in seconds
GATEWAY=${3:-}                   # address to ping (traffic, affinity, radio); empty: no ping
STAB_DIR=/data/local/tmp/stab    # the harness's directory on the device, with its tools
STRESS_NG=$STAB_DIR/stress-ng
TMP_DIR=$STAB_DIR/tmp            # scratch: stress-ng logs and temp path, ping log, test files
mkdir -p $TMP_DIR

# ---- helpers ---------------------------------------------------------------------------------------------------------
# ng <stress-ng arguments...>: run stress-ng with them for DURATION seconds (log: $TMP_DIR/ng-<step>-<pid>.log), then
# print "EL2STRESS <step> stress-ng rc=<its exit status>" and the last 4 lines of its log.
ng() {
  $STRESS_NG "$@" --timeout ${DURATION}s --temp-path $TMP_DIR --metrics-brief > $TMP_DIR/ng-$STEP-$$.log 2>&1
  echo "EL2STRESS $STEP stress-ng rc=$?"
  tail -4 $TMP_DIR/ng-$STEP-$$.log
}

# until_end <command...>: run the command again and again (output discarded) until DURATION seconds from now are
# over; a run in progress at that time is finished. Sets `end` (epoch seconds), like the steps do.
until_end() {
  end=$(( $(date +%s) + DURATION ))
  while [ $(date +%s) -lt $end ]; do
    "$@" >/dev/null 2>&1
  done
}

# n4 <full> [light]: the size of a part: <full>, or <light> (default 1) when LIGHT is set.
# In `all` (LIGHT=1) every part runs at about a quarter of its own step's size: all of them at full size drove the
# load average past 1000 on T11206 (2026-10-13), System UI and system_server stopped answering broadcasts, and CPU
# hot-unplug's stop_machine took 170 ms; that measures queueing, not more exits per CPU.
n4() { [ -n "${LIGHT:-}" ] && echo "${2:-1}" || echo "$1"; }

# ---- step ipi --------------------------------------------------------------------------------------------------------
# Drives: cross-CPU wake-ups, i.e. IPIs (each ICC_SGI1R write traps to EL2).
# How: stress-ng in the background with membarrier (IPIs to all CPUs), tlb-shootdown (TLB shootdowns), affinity
#   (hops between CPUs), switch (context switches) and futex (futex wakes) workers, 4 of each (in `all`: 1, 1, 2, 2
#   and 1); in the foreground hackbench thread groups ping-ponging 100-byte messages over pipes (-T, 4 groups, 2 in
#   `all`; 20000 loops per run), started again until the time is over.
# Parameters: <seconds>; LIGHT (set by `all`) for the reduced sizes.
# Prints: "EL2STRESS <step> stress-ng rc=<n>" with stress-ng's last 4 log lines; "EL2STRESS <step> hackbench rc=0".
# Fails: stress-ng exits with a status other than 0. hackbench's status is not checked (its line always says rc=0).
# Restores: nothing to restore.
ipi() {
  ng --membarrier $(n4 4) --tlb-shootdown $(n4 4) --affinity $(n4 4 2) --switch $(n4 4 2) --futex $(n4 4) &
  until_end $STAB_DIR/hackbench -T -g $(n4 4 2) -l 20000 -s 100
  echo "EL2STRESS $STEP hackbench rc=0"
  wait
}

# ---- step timer ------------------------------------------------------------------------------------------------------
# Drives: timer expiries (timer PPIs).
# How: stress-ng with 8 POSIX timers at 50 kHz each (in `all`: 4 at 20 kHz), 4 interval-timer (itimer) workers (2)
#   and 2 clock-reading workers (1).
# Parameters: <seconds>; LIGHT (set by `all`) for the reduced sizes.
# Prints: "EL2STRESS <step> stress-ng rc=<n>" with stress-ng's last 4 log lines.
# Fails: stress-ng exits with a status other than 0.
# Restores: nothing to restore.
timer() {
  ng --timer $(n4 8 4) --timer-freq $(n4 50000 20000) --itimer $(n4 4 2) --clock $(n4 2)
}

# ---- step idle -------------------------------------------------------------------------------------------------------
# Drives: idle entry/exit churn (deep idle is a CPU_SUSPEND power call).
# How: many threads sleeping short random intervals on every CPU: 16 stress-ng sleep workers with 256 threads each
#   (--sleep-max; in `all` 4 workers with 64).
# Parameters: <seconds>; LIGHT (set by `all`) for the reduced sizes.
# Prints: "EL2STRESS <step> stress-ng rc=<n>" with stress-ng's last 4 log lines.
# Fails: stress-ng exits with a status other than 0.
# Restores: nothing to restore.
idle() {
  ng --sleep $(n4 16 4) --sleep-max $(n4 256 64)
}

# ---- step traffic ----------------------------------------------------------------------------------------------------
# Drives: device interrupts: Wi-Fi and storage (USB traffic is added by the host).
# How: with a gateway, a flood ping to it (ping -f -q) in the background, stopped by SIGINT after DURATION seconds so
#   that it prints its summary; in the foreground 128 MiB of zeros written to $TMP_DIR/dd.bin with fsync (dd
#   conv=fsync) and the file removed, again and again until the time is over.
# Parameters: <seconds>, [gateway] (without one, storage only).
# Prints: "EL2STRESS <step> ping <the last 2 lines of ping's summary, joined>" (with a gateway);
#   "EL2STRESS <step> storage rc=0".
# Fails: never through its own lines (the ping line has no rc, the storage line always says rc=0).
# Restores: nothing to restore; the test file is removed after every write.
traffic() {
  [ -n "$GATEWAY" ] && {
    timeout -s INT $DURATION ping -f -q $GATEWAY > $TMP_DIR/ping.log 2>&1
    echo "EL2STRESS $STEP ping $(tail -2 $TMP_DIR/ping.log | tr '\n' ' ')"
  } &
  until_end sh -c "dd if=/dev/zero of=$TMP_DIR/dd.bin bs=1048576 count=128 conv=fsync; rm -f $TMP_DIR/dd.bin"
  echo "EL2STRESS $STEP storage rc=0"
  wait
}

# ---- step affinity ---------------------------------------------------------------------------------------------------
# Drives: GIC routing writes: every write to /proc/irq/<n>/smp_affinity_list re-routes that SPI in the GIC
#   distributor.
# How: traffic (above) runs in the background so that device interrupts fire. /proc/interrupts is read twice, 3 s
#   apart; the SPIs that fired in between, busiest first (at most 8), and the one at GIC hwirq 79 (the tick-broadcast
#   timer, however busy) are picked. Each one whose route can be written is routed to CPU 0, then all of them are
#   moved to the next CPU (1, 2, ..., 7, 0, ...) as fast as the shell allows, until <seconds> after the step started.
# Parameters: <seconds>, [gateway] (for traffic).
# Prints: "EL2STRESS <step> irqs <n> <n> ..." (the IRQs being moved), then
#   "EL2STRESS <step> affinity writes=<successful writes> restored: <irq>=<original CPU list> ...", and traffic's lines.
# Fails: never through its own lines (they have no rc).
# Restores: the original CPU list of every picked IRQ whose route was writable.
affinity() {
  traffic &
  cat /proc/interrupts > $TMP_DIR/irq-a
  sleep 3
  cat /proc/interrupts > $TMP_DIR/irq-b
  # SPIs only (GIC hwirq >= 32, not per-CPU), busiest first, whose route can be written.
  # The first awk sums the 8 per-CPU count columns (fields 2-9) of each numbered row of irq-a, then prints
  # "<increase> <irq>:" for each GICv3 row of irq-b with hwirq (field 11) >= 32.
  irqs=$(awk 'NR==FNR { if ($1 ~ /^[0-9]+:$/) { s = 0; for (i = 2; i <= 9; i++) s += $i; a[$1] = s } next }
              $1 ~ /^[0-9]+:$/ && $10 == "GICv3" && $11 >= 32 {
                s = 0; for (i = 2; i <= 9; i++) s += $i; print s - a[$1], $1 }' \
             $TMP_DIR/irq-a $TMP_DIR/irq-b |
         sort -rn |
         awk '$1 > 0 { sub(":", "", $2); print $2 }' |
         head -8)
  # Plus the tick-broadcast timer (GIC hwirq 79).
  irqs="$irqs $(awk '$10 == "GICv3" && $11 == 79 { sub(":", "", $1); print $1 }' /proc/interrupts)"
  orig=""     # "<irq>=<original CPU list>" of every IRQ whose route is writable
  routed=""   # the IRQs that took the first write (route to CPU 0): the ones moved below
  for irq in $irqs; do
    [ -w /proc/irq/$irq/smp_affinity_list ] || continue
    orig="$orig $irq=$(cat /proc/irq/$irq/smp_affinity_list)"
    echo 0 > /proc/irq/$irq/smp_affinity_list 2>/dev/null && routed="$routed $irq"
  done
  echo "EL2STRESS $STEP irqs$routed"
  # As fast as the shell allows: no sleep, the clock checked every 64 rounds (a `date` per round costs a fork).
  end=$(( $(date +%s) + DURATION - 3 ))
  cpu=0      # the CPU every IRQ is routed to in this round
  writes=0   # successful route writes
  rounds=0
  while :; do
    cpu=$(( (cpu + 1) % 8 ))
    rounds=$((rounds + 1))
    for irq in $routed; do
      echo $cpu > /proc/irq/$irq/smp_affinity_list 2>/dev/null && writes=$((writes + 1))
    done
    [ $((rounds % 64)) -eq 0 ] && [ $(date +%s) -ge $end ] && break
  done
  for kv in $orig; do
    echo ${kv#*=} > /proc/irq/${kv%%=*}/smp_affinity_list 2>/dev/null
  done
  echo "EL2STRESS $STEP affinity writes=$writes restored:$orig"
  wait
}

# ---- step hotplug ----------------------------------------------------------------------------------------------------
# Drives: CPU_OFF/CPU_ON power calls.
# How: CPUs 1-7 in turn taken offline (echo 0 > /sys/devices/system/cpu/cpu<n>/online), 0.1 s, back online, 0.1 s;
#   again and again until the time is over.
# Parameters: <seconds>.
# Prints: "EL2STRESS <step> hotplug cycles=<off/on cycles> refused=<refused writes> online=<online CPU list>".
# Fails: never through its own line (no rc); a refused off or on write is counted in refused=.
# Restores: CPUs 1-7 set online again at the end (online= shows the result).
hotplug() {
  end=$(( $(date +%s) + DURATION ))
  cycles=0    # off/on cycles of one CPU
  refused=0   # writes to an online file that failed
  while [ $(date +%s) -lt $end ]; do
    for cpu in 1 2 3 4 5 6 7; do
      echo 0 > /sys/devices/system/cpu/cpu$cpu/online 2>/dev/null || refused=$((refused + 1))
      sleep 0.1
      echo 1 > /sys/devices/system/cpu/cpu$cpu/online 2>/dev/null || refused=$((refused + 1))
      cycles=$((cycles + 1))
      sleep 0.1
    done
  done
  for cpu in 1 2 3 4 5 6 7; do
    echo 1 > /sys/devices/system/cpu/cpu$cpu/online 2>/dev/null
  done
  echo "EL2STRESS $STEP hotplug cycles=$cycles refused=$refused online=$(cat /sys/devices/system/cpu/online)"
}

# ---- step pmu --------------------------------------------------------------------------------------------------------
# Drives: PMU overflow interrupts (PMU PPIs).
# How: system-wide cpu-cycles sampling at 10 kHz (simpleperf record -a -f 10000, runs of at most 30 s until fewer
#   than 5 s are left) with every CPU 80% busy (stress-ng --cpu 8 --cpu-load 80: cycles count at full rate) and the
#   kernel's sampling throttle off for the step (perf_cpu_time_max_percent=0, perf_event_max_sample_rate=100000).
#   The EL2 monitor must be stopped first. This is the condition that reset the tablet on 2026-10-12 (10 kHz
#   sampling while the little cores were busy).
# Parameters: <seconds>.
# Prints: per simpleperf run "EL2STRESS <step> simpleperf rc=<n> Samples recorded: <n> Samples lost: <n>"; at the end
#   "EL2STRESS <step> restored perf_cpu_time_max_percent=<value> perf_event_max_sample_rate=<value>".
# Fails: simpleperf exits with a status other than 0.
# Restores: both throttle settings to their values from before the step.
pmu() {
  # Throttle off for the step (old values kept for the restore below).
  old_rate=$(cat /proc/sys/kernel/perf_event_max_sample_rate)
  old_pct=$(cat /proc/sys/kernel/perf_cpu_time_max_percent)
  echo 0 > /proc/sys/kernel/perf_cpu_time_max_percent
  echo 100000 > /proc/sys/kernel/perf_event_max_sample_rate
  $STRESS_NG --cpu 8 --cpu-load 80 --timeout ${DURATION}s --temp-path $TMP_DIR > /dev/null 2>&1 &
  end=$(( $(date +%s) + DURATION ))
  runs=0   # simpleperf runs (counted, not printed)
  while [ $(( end - $(date +%s) )) -gt 5 ]; do
    rec_s=$(( end - $(date +%s) ))   # length of this run: the time left, at most 30 s
    [ $rec_s -gt 30 ] && rec_s=30
    simpleperf record -a -e cpu-cycles -f 10000 --duration $rec_s -o $TMP_DIR/perf.data > $TMP_DIR/perf.log 2>&1
    echo "EL2STRESS $STEP simpleperf rc=$?" \
      "$(grep -oE 'Samples recorded: [0-9,]+|Samples lost: [0-9,]+' $TMP_DIR/perf.log | tr '\n' ' ')"
    rm -f $TMP_DIR/perf.data
    runs=$((runs + 1))
  done
  wait
  echo $old_pct > /proc/sys/kernel/perf_cpu_time_max_percent
  echo $old_rate > /proc/sys/kernel/perf_event_max_sample_rate
  echo "EL2STRESS $STEP restored perf_cpu_time_max_percent=$old_pct perf_event_max_sample_rate=$old_rate"
}

# ---- step radio ------------------------------------------------------------------------------------------------------
# Drives: Wi-Fi and Bluetooth driver unload/reload: interrupts freed and requested again, GIC enable/route writes,
#   PCIe MSI set-up. Borrowed from kylin100-autotest.
# How: in each cycle `svc wifi disable`, 3 s, `svc wifi enable`, then `cmd bluetooth_manager disable`, 2 s, enable;
#   again and again until the time is over (a cycle started before the end is finished).
# Parameters: <seconds>, [gateway] (pinged after Wi-Fi is back).
# Prints: "EL2STRESS <step> radio cycles=<n> wifi_fail=<n> bt_fail=<n> rc=<0|1>".
# Fails: a cycle passes when Wi-Fi has an IPv4 address on wlan0 within 30 s and, with a gateway, `ping -c 3 -W 2`
#   to it succeeds (else it counts in wifi_fail), and Bluetooth reports on again (settings global bluetooth_on = 1)
#   within 30 s (else it counts in bt_fail); rc=1 when either count is not 0.
# Restores: Wi-Fi is enabled at the end (whatever its state before the step); Bluetooth is turned off again if it
#   was off before.
radio() {
  bt_before=$(settings get global bluetooth_on)   # 1: Bluetooth was on before the step
  end=$(( $(date +%s) + DURATION ))
  cycles=0
  wifi_fail=0
  bt_fail=0
  while [ $(date +%s) -lt $end ]; do
    cycles=$((cycles + 1))
    # Wi-Fi off and on; it passes with an IPv4 address within 30 s and, with a gateway, an answered ping.
    svc wifi disable
    sleep 3
    svc wifi enable
    ok=0   # 1: the radio passed its check in this cycle
    for i in $(seq 1 30); do
      ip -4 addr show wlan0 2>/dev/null | grep -q inet && { ok=1; break; }
      sleep 1
    done
    [ $ok = 1 ] && [ -n "$GATEWAY" ] && { ping -c 3 -W 2 $GATEWAY > /dev/null 2>&1 || ok=0; }
    [ $ok = 1 ] || wifi_fail=$((wifi_fail + 1))
    # Bluetooth off and on; it passes when it reports on again within 30 s.
    cmd bluetooth_manager disable > /dev/null 2>&1
    sleep 2
    cmd bluetooth_manager enable > /dev/null 2>&1
    ok=0
    for i in $(seq 1 30); do
      [ "$(settings get global bluetooth_on)" = 1 ] && { ok=1; break; }
      sleep 1
    done
    [ $ok = 1 ] || bt_fail=$((bt_fail + 1))
  done
  svc wifi enable
  [ "$bt_before" = 1 ] || cmd bluetooth_manager disable > /dev/null 2>&1
  echo "EL2STRESS $STEP radio cycles=$cycles wifi_fail=$wifi_fail bt_fail=$bt_fail" \
    "rc=$([ $wifi_fail = 0 ] && [ $bt_fail = 0 ] && echo 0 || echo 1)"
}

# ---- step copy -------------------------------------------------------------------------------------------------------
# Drives: storage interrupts and DMA, with a data check that catches silent corruption a crash check misses.
#   Borrowed from kylin100-autotest.
# How: in each round 64 MiB from /dev/urandom written to $TMP_DIR/copy-src and its md5 taken, the file copied to
#   copy-dst, sync, the page cache dropped (drop_caches=3), then both files read back from storage and md5-compared
#   with the first sum; both removed; again and again until the time is over.
# Parameters: <seconds>.
# Prints: per bad round "EL2STRESS <step> MISMATCH round <n> src=<md5> dst=<md5> src-again=<md5>"; at the end
#   "EL2STRESS <step> copy rounds=<n> mismatches=<n> rc=<0|1>".
# Fails: any mismatch (rc=1).
# Restores: nothing to restore; the files are removed after every round.
copy() {
  end=$(( $(date +%s) + DURATION ))
  rounds=0
  mismatches=0
  while [ $(date +%s) -lt $end ]; do
    rounds=$((rounds + 1))
    dd if=/dev/urandom of=$TMP_DIR/copy-src bs=1048576 count=64 2>/dev/null
    sum_src=$(md5sum $TMP_DIR/copy-src | cut -d' ' -f1)
    cp $TMP_DIR/copy-src $TMP_DIR/copy-dst
    sync
    echo 3 > /proc/sys/vm/drop_caches
    sum_dst=$(md5sum $TMP_DIR/copy-dst | cut -d' ' -f1)
    sum_src_again=$(md5sum $TMP_DIR/copy-src | cut -d' ' -f1)
    [ "$sum_src" = "$sum_dst" ] && [ "$sum_src" = "$sum_src_again" ] || {
      mismatches=$((mismatches + 1))
      echo "EL2STRESS $STEP MISMATCH round $rounds src=$sum_src dst=$sum_dst src-again=$sum_src_again"
    }
    rm -f $TMP_DIR/copy-src $TMP_DIR/copy-dst
  done
  echo "EL2STRESS $STEP copy rounds=$rounds mismatches=$mismatches rc=$([ $mismatches = 0 ] && echo 0 || echo 1)"
}

# ---- step gpu --------------------------------------------------------------------------------------------------------
# Drives: GPU load: GPU job interrupts, GPU memory mapping, display vsync.
# How: glmark2 (org.linaro.glmark2; the harness installs it from STAB_APK_DIR) rendering forever (--run-forever),
#   started full screen after waking the screen and dismissing the keyguard; a wake-up key every 10 s keeps the
#   screen on. 5 s after the start the GPU interrupt count is taken (the first /proc/interrupts row naming gpu),
#   then the GPU frequency (cur_freq of the first /sys/class/devfreq/*.gpu) is read every 10 s for <seconds> - 10
#   to <seconds> seconds, and the interrupt count is taken again.
# Parameters: <seconds>.
# Prints: "EL2STRESS <step> glmark2 gpu_irq=<irq> irq_per_s=<GPU interrupt rate> max_freq=<highest cur_freq> rc=0";
#   "EL2STRESS <step> glmark2 not installed rc=3" without the app.
# Fails: glmark2 not installed (rc=3).
# Restores: glmark2 force-stopped at the end; the screen is not turned off.
gpu() {
  pm path org.linaro.glmark2 > /dev/null 2>&1 || { echo "EL2STRESS $STEP glmark2 not installed rc=3"; return; }
  dev=$(ls -d /sys/class/devfreq/*.gpu 2>/dev/null | head -1)   # the GPU's devfreq directory
  irq=$(awk '$NF ~ /gpu/ || $0 ~ /\.gpu/ { sub(":", "", $1); print $1; exit }' /proc/interrupts)
  # cnt: the GPU interrupt's count summed over the 8 CPUs.
  cnt() { awk -v n="$irq:" '$1 == n { s = 0; for (i = 2; i <= 9; i++) s += $i; print s }' /proc/interrupts; }
  input keyevent KEYCODE_WAKEUP
  wm dismiss-keyguard
  # Full screen even in the desktop (freeform) mode: a small window renders far fewer pixels.
  am start --windowingMode 1 -n org.linaro.glmark2/.Glmark2Activity -e args "--run-forever" > /dev/null 2>&1
  sleep 5
  irq_start=$(cnt)
  t0=$(date +%s)
  fmax=0   # highest GPU frequency seen
  end=$(( t0 + DURATION - 10 ))
  while [ $(date +%s) -lt $end ]; do
    freq=$(cat $dev/cur_freq 2>/dev/null)
    [ "${freq:-0}" -gt $fmax ] && fmax=$freq
    input keyevent KEYCODE_WAKEUP
    sleep 10
  done
  irq_end=$(cnt)
  t1=$(date +%s)
  am force-stop org.linaro.glmark2
  echo "EL2STRESS $STEP glmark2 gpu_irq=$irq irq_per_s=$(( (irq_end - irq_start) / (t1 - t0 + 1) ))" \
    "max_freq=$fmax rc=0"
}

# ---- step tee --------------------------------------------------------------------------------------------------------
# Drives: TEE calls: each call is an SMC the hypervisor forwards and a Trusty notification on SGI 13 (which XHyper
#   hooks).
# How: TEE_WORKERS parallel workers (default 2), each until the time is over: generate an RSA key in the TEE (KeyMint
#   in Trusty) named stab_tee_w<worker>_<n>, sign and verify with it five times, delete it, again. TEE
#   encrypt/decrypt with keystore_cli_v2 failed on decrypt on 2026-10-13: not used until compared on stock.
#   cyclictest runs alongside (-i 1000: 1 ms, one thread per CPU, priority 90) unless TEE_CYCLIC=0 or it is missing:
#   it shows how long CPUs stay out of Linux's reach while a TEE call runs (an interrupt that arrives while the CPU
#   is in the secure world should make Trusty yield quickly; a TEE call that cannot be interrupted shows up here as
#   a latency of up to the length of the call, about 0.9 s for an RSA key generation on T11206). Phase tee_idle sets
#   TEE_CYCLIC=0: cyclictest's 1 ms timers keep every CPU out of deep idle, where the Trusty IRQ-off windows it
#   looks for showed up.
# Parameters: <seconds>; TEE_WORKERS (default 2), TEE_CYCLIC (0: no cyclictest).
# Prints: "EL2STRESS <step> tee workers=<n> ops=<keystore calls> failures=<n> trusty_irq=<trusty interrupts during
#   the step> cyclictest_max_us=[<max latency per thread, comma separated>] rc=<0|1>", with "cyclictest=off" in place
#   of cyclictest_max_us=[...] when TEE_CYCLIC=0; "EL2STRESS <step> keystore_cli_v2 missing rc=3" without the tool.
# Fails: a key generation that fails or a sign-verify that does not print "Verify: OK" (failures=, rc=1);
#   keystore_cli_v2 missing (rc=3).
# Restores: every key named stab_tee_* is deleted at the end.
tee() {
  command -v keystore_cli_v2 > /dev/null || { echo "EL2STRESS $STEP keystore_cli_v2 missing rc=3"; return; }
  # trusty: the count of the interrupt named trusty, summed over the 8 CPUs.
  trusty() { awk '$NF == "trusty" { s = 0; for (i = 2; i <= 9; i++) s += $i; print s }' /proc/interrupts; }
  # cyclictest alongside (see above), in the background for DURATION seconds.
  [ "${TEE_CYCLIC:-1}" != 0 ] && [ -x $STAB_DIR/cyclictest ] \
    && $STAB_DIR/cyclictest -m -p 90 -i 1000 -t -a -q -D ${DURATION}s > $TMP_DIR/tee-cyclic.log 2>&1 &
  trusty_start=$(trusty)
  end=$(( $(date +%s) + DURATION ))
  workers=${TEE_WORKERS:-2}
  worker=0
  while [ $worker -lt $workers ]; do
    # One worker in a subshell: key rounds until the end; writes "<calls> <failures>" to tee-w<worker>.
    (
      n=0          # number of this worker's next key
      ops=0        # keystore calls made
      failures=0   # failed generations and sign-verifies without "Verify: OK"
      while [ $(date +%s) -lt $end ]; do
        key=stab_tee_w${worker}_$n
        n=$((n + 1))
        keystore_cli_v2 generate --name=$key > /dev/null 2>&1 || failures=$((failures + 1))
        ops=$((ops + 1))
        for i in 1 2 3 4 5; do
          keystore_cli_v2 sign-verify --name=$key 2>&1 | grep -q "Verify: OK" || failures=$((failures + 1))
          ops=$((ops + 1))
        done
        keystore_cli_v2 delete --name=$key > /dev/null 2>&1
        ops=$((ops + 1))
      done
      echo "$ops $failures" > $TMP_DIR/tee-w$worker
    ) &
    worker=$((worker + 1))
  done
  wait
  # Totals over the workers.
  ops=0
  failures=0
  for file in $TMP_DIR/tee-w*; do
    set -- $(cat $file)
    ops=$((ops + $1))
    failures=$((failures + $2))
  done
  rm -f $TMP_DIR/tee-w*
  # Delete every stab_tee_* key that is still there.
  for key in $(keystore_cli_v2 list --prefix=stab_tee 2>/dev/null | grep stab_tee); do
    keystore_cli_v2 delete --name=$key > /dev/null 2>&1
  done
  trusty_end=$(trusty)
  wait
  # cyclictest's "Max:" latency of every thread, comma separated.
  cmax=$(grep -oE 'Max: *[0-9]+' $TMP_DIR/tee-cyclic.log 2>/dev/null | awk '{printf "%s%s", sep, $2; sep=","}')
  cyc="cyclictest_max_us=[$cmax]"
  [ "${TEE_CYCLIC:-1}" = 0 ] && cyc=cyclictest=off
  echo "EL2STRESS $STEP tee workers=$workers ops=$ops failures=$failures trusty_irq=$((trusty_end - trusty_start))" \
    "$cyc rc=$([ $failures = 0 ] && echo 0 || echo 1)"
}

# ---- step all --------------------------------------------------------------------------------------------------------
# Drives: everything at once: ipi + timer + idle + affinity (with traffic) + hotplug + copy + gpu, each in the
#   background at its reduced size (LIGHT=1, see n4). Not tee: TEE key generation holds a CPU in the secure world
#   for about 0.9 s per call, which together with everything else starved system_server's watchdog for 16 s on
#   2026-10-13.
# Parameters: <seconds>, [gateway] (for traffic).
# Prints: the lines of every part, each with the step name "all".
# Fails: as each part.
# Restores: as each part.
all() {
  export LIGHT=1
  ipi &
  timer &
  idle &
  affinity &
  hotplug &
  copy &
  gpu &
  wait
}

# ---- main ------------------------------------------------------------------------------------------------------------
case $STEP in
  ipi | timer | idle | traffic | affinity | hotplug | pmu | radio | copy | gpu | tee | all) $STEP ;;
  *)
    echo "unknown step $STEP"
    exit 2
    ;;
esac
echo "EL2STRESS_DONE $STEP"
