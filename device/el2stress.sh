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
#   all       ipi + timer + idle + affinity (with traffic) + hotplug at once
# Output: one line per finished part, "EL2STRESS <step> <part> rc=<n>"; the last line "EL2STRESS_DONE <step>".
S=${1:?step}; T=${2:?seconds}; GW=${3:-}
D=/data/local/tmp/stab; N=$D/stress-ng; TMP=$D/tmp
mkdir -p $TMP
ng() { $N "$@" --timeout ${T}s --temp-path $TMP --metrics-brief > $TMP/ng-$S-$$.log 2>&1; echo "EL2STRESS $S stress-ng rc=$?"; tail -4 $TMP/ng-$S-$$.log; }
until_end() { end=$(( $(date +%s) + T )); while [ $(date +%s) -lt $end ]; do "$@" >/dev/null 2>&1; done; }

ipi() {
  ng --membarrier 4 --tlb-shootdown 4 --affinity 4 --switch 4 --futex 4 &
  until_end $D/hackbench -T -g 4 -l 20000 -s 100; echo "EL2STRESS $S hackbench rc=0"
  wait
}
timer() { ng --timer 8 --timer-freq 50000 --itimer 4 --clock 2; }
idle() { ng --sleep 16 --sleep-max 256; }
traffic() {
  [ -n "$GW" ] && { timeout $T ping -f -q $GW > $TMP/ping.log 2>&1; echo "EL2STRESS $S ping $(tail -2 $TMP/ping.log | tr '\n' ' ')"; } &
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
all() { ipi & timer & idle & affinity & hotplug & wait; }

case $S in ipi|timer|idle|traffic|affinity|hotplug|pmu|all) $S ;; *) echo "unknown step $S"; exit 2 ;; esac
echo "EL2STRESS_DONE $S"
