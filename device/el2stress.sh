#!/system/bin/sh
# el2stress.sh <step> <seconds> [gateway]: drive one class of exits to the hypervisor (EL2) for <seconds>; run as root.
# A guest at EL1/EL0 only enters EL2 on: physical interrupts (device SPIs, the timer and PMU PPIs), IPIs it sends
# (ICC_SGI1R writes trap), GIC distributor/redistributor register accesses, power calls (deep idle CPU_SUSPEND,
# CPU_OFF/ON, system suspend), some system registers and hypercalls. CPU-bound or memory-bound work does not exit
# (Host RAM is mapped in large stage-2 blocks up front), so each step makes many short, frequent events instead:
#   ipi       cross-CPU wake-ups: membarrier (IPIs to all CPUs), TLB shootdowns, affinity hops, context switches,
#             futex wakes, plus hackbench thread groups ping-ponging over pipes
#   timer     timer expiries: 8 POSIX timers at 50 kHz each, interval timers, clock reads
#   idle      idle entry/exit churn: many threads sleeping short random intervals on every CPU
#   traffic   device interrupts: Wi-Fi ping flood to <gateway>, storage writes with fsync (USB traffic is added by the host)
#   affinity  GIC routing writes: the busiest SPIs (picked while traffic runs) re-routed to the next CPU every 10 ms,
#             including the tick-broadcast timer; original routes restored at the end
#   hotplug   CPU_OFF/CPU_ON: CPUs 1-7 taken offline and back online in turn; all CPUs online again at the end
#   pmu       PMU overflow interrupts: system-wide cpu-cycles sampling at 10 kHz with every CPU 80% busy and the
#             kernel's sampling throttle off for the step (the EL2 monitor must be stopped first)
#   radio     Wi-Fi and Bluetooth off/on in turn (driver unload/reload: interrupts freed and requested again, GIC
#             enable/route writes, PCIe MSI set-up); a round passes when Wi-Fi has an IPv4 address and pings the
#             gateway and Bluetooth reports on again; original states restored (borrowed from kylin100-autotest)
#   copy      random data written with fsync, page cache dropped, read back and md5-compared (storage interrupts and
#             DMA; catches silent corruption a crash check misses; borrowed from kylin100-autotest)
#   gpu       GPU load: glmark2 rendering forever (the harness installs it from STAB_APK_DIR); GPU job interrupts, GPU
#             memory mapping, display vsync; reports the GPU interrupt rate and the GPU frequency reached
#   tee       TEE calls: parallel workers generate an RSA key in the TEE (KeyMint in Trusty), sign and verify with it
#             five times, delete it, again; each call is an SMC the hypervisor forwards and a Trusty notification on
#             SGI 13 (which XHyper hooks); every verify must say OK; keys are named stab_tee_* and all removed at the end
#             (TEE encrypt/decrypt with keystore_cli_v2 failed on decrypt on 2026-10-13: not used until compared on stock)
#   all       ipi + timer + idle + affinity (with traffic) + hotplug + copy + gpu at once (not tee: TEE key generation
#             holds a CPU in the secure world for about 0.9 s per call, which together with everything else starved
#             system_server's watchdog for 16 s on 2026-10-13)
# Output: one line per finished part, "EL2STRESS <step> <part> rc=<n>"; the last line "EL2STRESS_DONE <step>".
S=${1:?step}; T=${2:?seconds}; GW=${3:-}
D=/data/local/tmp/stab; N=$D/stress-ng; TMP=$D/tmp
mkdir -p $TMP
ng() { $N "$@" --timeout ${T}s --temp-path $TMP --metrics-brief > $TMP/ng-$S-$$.log 2>&1; echo "EL2STRESS $S stress-ng rc=$?"; tail -4 $TMP/ng-$S-$$.log; }
until_end() { end=$(( $(date +%s) + T )); while [ $(date +%s) -lt $end ]; do "$@" >/dev/null 2>&1; done; }

# In `all` (LIGHT=1) every part runs at about a quarter of its own step's size: all of them at full size drove the
# load average past 1000 on T11206 (2026-10-13), System UI and system_server stopped answering broadcasts, and CPU
# hot-unplug's stop_machine took 170 ms; that measures queueing, not more exits per CPU.
n4() { [ -n "${LIGHT:-}" ] && echo "${2:-1}" || echo "$1"; }
ipi() {
  ng --membarrier $(n4 4) --tlb-shootdown $(n4 4) --affinity $(n4 4 2) --switch $(n4 4 2) --futex $(n4 4) &
  until_end $D/hackbench -T -g $(n4 4 2) -l 20000 -s 100; echo "EL2STRESS $S hackbench rc=0"
  wait
}
timer() { ng --timer $(n4 8 4) --timer-freq $(n4 50000 20000) --itimer $(n4 4 2) --clock $(n4 2); }
idle() { ng --sleep $(n4 16 4) --sleep-max $(n4 256 64); }
traffic() {
  [ -n "$GW" ] && { timeout -s INT $T ping -f -q $GW > $TMP/ping.log 2>&1; echo "EL2STRESS $S ping $(tail -2 $TMP/ping.log | tr '\n' ' ')"; } &
  until_end sh -c "dd if=/dev/zero of=$TMP/dd.bin bs=1048576 count=128 conv=fsync; rm -f $TMP/dd.bin"
  echo "EL2STRESS $S storage rc=0"; wait
}
affinity() {
  traffic &
  cat /proc/interrupts > $TMP/irq-a; sleep 3; cat /proc/interrupts > $TMP/irq-b
  # SPIs only (GIC hwirq >= 32, not per-CPU), busiest first, whose route can be written
  irqs=$(awk 'NR==FNR { if ($1 ~ /^[0-9]+:$/) { s = 0; for (i = 2; i <= 9; i++) s += $i; a[$1] = s } next }
              $1 ~ /^[0-9]+:$/ && $10 == "GICv3" && $11 >= 32 { s = 0; for (i = 2; i <= 9; i++) s += $i; print s - a[$1], $1 }' \
             $TMP/irq-a $TMP/irq-b | sort -rn | awk '$1 > 0 { sub(":", "", $2); print $2 }' | head -8)
  irqs="$irqs $(awk '$10 == "GICv3" && $11 == 79 { sub(":", "", $1); print $1 }' /proc/interrupts)"
  orig=""; ok=""
  for n in $irqs; do
    [ -w /proc/irq/$n/smp_affinity_list ] || continue
    orig="$orig $n=$(cat /proc/irq/$n/smp_affinity_list)"
    echo 0 > /proc/irq/$n/smp_affinity_list 2>/dev/null && ok="$ok $n"
  done
  echo "EL2STRESS $S irqs$ok"
  # As fast as the shell allows: no sleep, the clock checked every 64 rounds (a `date` per round costs a fork).
  end=$(( $(date +%s) + T - 3 )); c=0; w=0; r=0
  while :; do
    c=$(( (c + 1) % 8 )); r=$((r + 1))
    for n in $ok; do echo $c > /proc/irq/$n/smp_affinity_list 2>/dev/null && w=$((w + 1)); done
    [ $((r % 64)) -eq 0 ] && [ $(date +%s) -ge $end ] && break
  done
  for kv in $orig; do echo ${kv#*=} > /proc/irq/${kv%%=*}/smp_affinity_list 2>/dev/null; done
  echo "EL2STRESS $S affinity writes=$w restored:$orig"
  wait
}
hotplug() {
  end=$(( $(date +%s) + T )); n=0; bad=0
  while [ $(date +%s) -lt $end ]; do
    for c in 1 2 3 4 5 6 7; do
      echo 0 > /sys/devices/system/cpu/cpu$c/online 2>/dev/null || bad=$((bad + 1)); sleep 0.1
      echo 1 > /sys/devices/system/cpu/cpu$c/online 2>/dev/null || bad=$((bad + 1)); n=$((n + 1)); sleep 0.1
    done
  done
  for c in 1 2 3 4 5 6 7; do echo 1 > /sys/devices/system/cpu/cpu$c/online 2>/dev/null; done
  echo "EL2STRESS $S hotplug cycles=$n refused=$bad online=$(cat /sys/devices/system/cpu/online)"
}
pmu() {
  # Every CPU busy (cycles count at full rate) and the kernel's sampling throttle off for the step, then restored:
  # the condition that reset the tablet on 2026-10-12 (10 kHz sampling while the little cores were busy).
  pr=$(cat /proc/sys/kernel/perf_event_max_sample_rate); pc=$(cat /proc/sys/kernel/perf_cpu_time_max_percent)
  echo 0 > /proc/sys/kernel/perf_cpu_time_max_percent; echo 100000 > /proc/sys/kernel/perf_event_max_sample_rate
  $N --cpu 8 --cpu-load 80 --timeout ${T}s --temp-path $TMP > /dev/null 2>&1 &
  end=$(( $(date +%s) + T )); k=0
  while [ $(( end - $(date +%s) )) -gt 5 ]; do
    d=$(( end - $(date +%s) )); [ $d -gt 30 ] && d=30
    simpleperf record -a -e cpu-cycles -f 10000 --duration $d -o $TMP/perf.data > $TMP/perf.log 2>&1
    echo "EL2STRESS $S simpleperf rc=$? $(grep -oE 'Samples recorded: [0-9,]+|Samples lost: [0-9,]+' $TMP/perf.log | tr '\n' ' ')"
    rm -f $TMP/perf.data; k=$((k + 1))
  done
  wait
  echo $pc > /proc/sys/kernel/perf_cpu_time_max_percent; echo $pr > /proc/sys/kernel/perf_event_max_sample_rate
  echo "EL2STRESS $S restored perf_cpu_time_max_percent=$pc perf_event_max_sample_rate=$pr"
}
radio() {
  bt0=$(settings get global bluetooth_on); end=$(( $(date +%s) + T )); n=0; wf=0; bf=0
  while [ $(date +%s) -lt $end ]; do
    n=$((n + 1))
    svc wifi disable; sleep 3; svc wifi enable
    ok=0; for i in $(seq 1 30); do ip -4 addr show wlan0 2>/dev/null | grep -q inet && { ok=1; break; }; sleep 1; done
    [ $ok = 1 ] && [ -n "$GW" ] && { ping -c 3 -W 2 $GW > /dev/null 2>&1 || ok=0; }
    [ $ok = 1 ] || wf=$((wf + 1))
    cmd bluetooth_manager disable > /dev/null 2>&1; sleep 2; cmd bluetooth_manager enable > /dev/null 2>&1
    ok=0; for i in $(seq 1 30); do [ "$(settings get global bluetooth_on)" = 1 ] && { ok=1; break; }; sleep 1; done
    [ $ok = 1 ] || bf=$((bf + 1))
  done
  svc wifi enable
  [ "$bt0" = 1 ] || cmd bluetooth_manager disable > /dev/null 2>&1
  echo "EL2STRESS $S radio cycles=$n wifi_fail=$wf bt_fail=$bf rc=$([ $wf = 0 ] && [ $bf = 0 ] && echo 0 || echo 1)"
}
copy() {
  end=$(( $(date +%s) + T )); n=0; bad=0
  while [ $(date +%s) -lt $end ]; do
    n=$((n + 1))
    dd if=/dev/urandom of=$TMP/copy-src bs=1048576 count=64 2>/dev/null; a=$(md5sum $TMP/copy-src | cut -d' ' -f1)
    cp $TMP/copy-src $TMP/copy-dst; sync; echo 3 > /proc/sys/vm/drop_caches
    b=$(md5sum $TMP/copy-dst | cut -d' ' -f1); c=$(md5sum $TMP/copy-src | cut -d' ' -f1)
    [ "$a" = "$b" ] && [ "$a" = "$c" ] || { bad=$((bad + 1)); echo "EL2STRESS $S MISMATCH round $n src=$a dst=$b src-again=$c"; }
    rm -f $TMP/copy-src $TMP/copy-dst
  done
  echo "EL2STRESS $S copy rounds=$n mismatches=$bad rc=$([ $bad = 0 ] && echo 0 || echo 1)"
}
gpu() {
  pm path org.linaro.glmark2 > /dev/null 2>&1 || { echo "EL2STRESS $S glmark2 not installed rc=3"; return; }
  dev=$(ls -d /sys/class/devfreq/*.gpu 2>/dev/null | head -1)
  irq=$(awk '$NF ~ /gpu/ || $0 ~ /\.gpu/ { sub(":", "", $1); print $1; exit }' /proc/interrupts)
  cnt() { awk -v n="$irq:" '$1 == n { s = 0; for (i = 2; i <= 9; i++) s += $i; print s }' /proc/interrupts; }
  input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard
  # Full screen even in the desktop (freeform) mode: a small window renders far fewer pixels.
  am start --windowingMode 1 -n org.linaro.glmark2/.Glmark2Activity -e args "--run-forever" > /dev/null 2>&1
  sleep 5; a=$(cnt); t0=$(date +%s); fmax=0
  end=$(( t0 + T - 10 ))
  while [ $(date +%s) -lt $end ]; do
    f=$(cat $dev/cur_freq 2>/dev/null); [ "${f:-0}" -gt $fmax ] && fmax=$f
    input keyevent KEYCODE_WAKEUP; sleep 10
  done
  b=$(cnt); t1=$(date +%s)
  am force-stop org.linaro.glmark2
  echo "EL2STRESS $S glmark2 gpu_irq=$irq irq_per_s=$(( (b - a) / (t1 - t0 + 1) )) max_freq=$fmax rc=0"
}
tee() {
  command -v keystore_cli_v2 > /dev/null || { echo "EL2STRESS $S keystore_cli_v2 missing rc=3"; return; }
  trusty() { awk '$NF == "trusty" { s = 0; for (i = 2; i <= 9; i++) s += $i; print s }' /proc/interrupts; }
  # cyclictest alongside: how long CPUs stay out of Linux's reach while a TEE call runs (an interrupt that arrives
  # while the CPU is in the secure world should make Trusty yield quickly; a TEE call that cannot be interrupted shows
  # up here as a latency of up to the length of the call, about 0.9 s for an RSA key generation on T11206)
  [ -x $D/cyclictest ] && $D/cyclictest -m -p 90 -i 1000 -t -a -q -D ${T}s > $TMP/tee-cyclic.log 2>&1 &
  a=$(trusty); end=$(( $(date +%s) + T )); W=${TEE_WORKERS:-2}
  w=0; while [ $w -lt $W ]; do
    ( n=0; ops=0; bad=0
      while [ $(date +%s) -lt $end ]; do
        k=stab_tee_w${w}_$n; n=$((n + 1))
        keystore_cli_v2 generate --name=$k > /dev/null 2>&1 || bad=$((bad + 1)); ops=$((ops + 1))
        for i in 1 2 3 4 5; do
          keystore_cli_v2 sign-verify --name=$k 2>&1 | grep -q "Verify: OK" || bad=$((bad + 1)); ops=$((ops + 1))
        done
        keystore_cli_v2 delete --name=$k > /dev/null 2>&1; ops=$((ops + 1))
      done
      echo "$ops $bad" > $TMP/tee-w$w ) &
    w=$((w + 1))
  done
  wait
  ops=0; bad=0; for f in $TMP/tee-w*; do set -- $(cat $f); ops=$((ops + $1)); bad=$((bad + $2)); done; rm -f $TMP/tee-w*
  for k in $(keystore_cli_v2 list --prefix=stab_tee 2>/dev/null | grep stab_tee); do keystore_cli_v2 delete --name=$k > /dev/null 2>&1; done
  b=$(trusty); wait
  cmax=$(grep -oE 'Max: *[0-9]+' $TMP/tee-cyclic.log 2>/dev/null | awk '{printf "%s%s", sep, $2; sep=","}')
  echo "EL2STRESS $S tee workers=$W ops=$ops failures=$bad trusty_irq=$((b - a)) cyclictest_max_us=[$cmax] rc=$([ $bad = 0 ] && echo 0 || echo 1)"
}
all() { export LIGHT=1; ipi & timer & idle & affinity & hotplug & copy & gpu & wait; }

case $S in ipi|timer|idle|traffic|affinity|hotplug|pmu|radio|copy|gpu|tee|all) $S ;; *) echo "unknown step $S"; exit 2 ;; esac
echo "EL2STRESS_DONE $S"
