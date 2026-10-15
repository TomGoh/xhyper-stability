#!/usr/bin/env python3
"""stab.py: stability and stress runs for an Android device whose OS runs under a hypervisor.

  stab.py run    -s SERIAL --plan plans/<plan>.json --out DIR [--expect-boot-sha PREFIX]
  stab.py report DIR

A run executes the plan's phases in order (reboot_loop, soak, suspend, memtest, latency, standby, el2_stress,
monitor_soak, battery_rundown, tee_idle) and takes a health snapshot (device/health.sh) at least every
`health_interval_s`. Failures are classified from the host side, because a hypervisor fault rarely leaves a trace
inside Android: the device dropping off adb (DEVICE_LOST / DEVICE_GONE), a boot id that changed without a planned
reboot (UNEXPECTED_REBOOT, with the boot reasons and the pstore files of the previous boot pulled), a boot that never
completes (BOOT_FAIL); then Android-side findings (new kernel oops/lockup/watchdog lines, crash-buffer entries, ANRs,
tombstones), stress-tool failures and suspend failures. Kernel lines are attributed by their kernel time to the phase
they happened in (a `STAB PHASE_START` marker in the kernel log at every phase start), not to the snapshot that
collected them. Every event goes to events.jsonl and log.txt; summary.md is (re)written at the end, also after Ctrl-C
or an abort. Python 3 stdlib only.
"""
# ==== Program structure ===============================================================================================
# The docstring above is the command-line help (`stab.py --help` prints it); the explanation of the program is here.
#
# Classes:
#   Abort        raised to end a run early (device gone, boot failed, identity mismatch, a phase that cannot start);
#                Run.execute records it as an ABORT event and still writes the summary.
#   Device       one Android device over adb: adb / sh / su (root through `su 0`) commands with timeouts, waits for
#                adb to drop or come back and for the boot to complete, and health(): device/health.sh run as root,
#                its output parsed by parse_health.
#   ConsoleHist  bytes the serial console carried per 0.1 s of kernel time; tells whether a long preempt/IRQ-off
#                report was caused by the synchronous serial console.
#   RundownLive  follows the battery sampler's 'RUNDOWN <csv line>' kernel-log lines in a serial capture while USB is
#                unplugged (rundown-live.csv) and reports the progress.
#   Run          one run of a plan: the phases in order, the health snapshots, the events, the summary.
#
# Phases: Run.execute runs setup (device scripts and tools pushed, boot_a checked against --expect-boot-sha, a
# baseline snapshot, the EL2 parallel log started), then for each entry of the plan's "phases" a PHASE_START event
# and a 'STAB PHASE_START <phase>' kernel-log marker, Run.phase_<type>(entry), any not-responding dialog dismissed, a
# snapshot and PHASE_END. Phase types (one method each): reboot_loop, soak, suspend, memtest, standby, latency,
# el2_stress, monitor_soak, battery_rundown, tee_idle. Phase names are '<n>-<type>'; el2_stress and tee_idle add
# '/<step or block>'.
#
# Health snapshot loop: a phase sleeps through Run.wait(), which calls Run.checkpoint() at least every 15 s; a
# checkpoint that is due (every `health_interval_s`, default 300) takes a snapshot (Run.snapshot: device/health.sh),
# scans the serial capture's new bytes for fatal markers and, with the EL2 monitor, pulls and checks the EL2 log.
# A snapshot is compared with the previous one: boot id (a planned reboot, else UNEXPECTED_REBOOT with pstore and
# boot reasons), device inventory, new kernel lines (graded by kernel_grade and attributed by kernel time to the
# phase whose marker precedes them), crash buffer, ANRs, tombstones, pstore records, suspend counters. A snapshot
# that gets no answer goes to Run.lost() (PROBE_TIMEOUT, DEVICE_LOST, DEVICE_GONE, BOOT_FAIL).
#
# Events: Run.event() appends one JSON line to events.jsonl and one line to log.txt and stdout. The kind sets the
# severity: a kind in ERRORS (or an ABORT) makes the run FAIL, a kind in WARNINGS is a warning, any other kind
# (INFO, PHASE_START, PHASE_END, EL2_LOAD, ...) is information.
#
# Report: write_summary() (re)writes summary.md from events.jsonl at the end of every run (also after an abort or
# Ctrl-C) and on `stab.py report DIR`; el2_report.py and rundown_report.py write their own reports next to it.
#
# Map of this file, in order:
#   paths and event classes      REPO, DEV, ERRORS, WARNINGS
#   device access                Abort, Device, parse_health
#   serial and kernel patterns   SERIAL_FATAL, KERNEL_ERROR
#   EL2 monitor modules          ko_load_order
#   kernel log grading           ktime, printout_windows, parse_off, irq_intervals, waits_on, ConsoleHist,
#                                console_load, kernel_grade
#   suspend and standby          wake_info, wake_lines, wake_source, standby_summary
#   battery rundown helpers      lock files, settings to apply / restore, the start checks
#   battery rundown live view    stamp_time, RundownLive
#   TEE idle helpers             trusty_reports, tee_block, tee_stats, tee_best, stop_cmd, tee_idle_lines
#   small helpers                step_names, attribute, num, fnum
#   the run (class Run)          records, health snapshots, serial console, core loop, EL2 parallel log, setup,
#                                one section per phase, driver (execute)
#   report                       count, rundown_lines, write_summary
#   command line                 main

import argparse, collections, csv, datetime, json, os, re, shlex, signal, statistics, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import el2_report  # noqa: E402
import rundown_report  # noqa: E402

# ==== Paths and event classes =========================================================================================
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repository root (device/, third_party/ below)
DEV = '/data/local/tmp/stab'  # working directory on the device: scripts, tools, logs
# Event kinds that make a run FAIL (log.txt marks them ERROR)
ERRORS = {'DEVICE_MISSING', 'SERIAL_FATAL', 'UNEXPECTED_REBOOT', 'DEVICE_GONE', 'BOOT_FAIL', 'STRESS_FAIL',
          'MEMTEST_FAIL', 'SUSPEND_FAIL', 'KERNEL_BAD', 'IDENTITY_MISMATCH', 'RESUME_TIMEOUT', 'MONITOR_FAIL'}
# Event kinds that are warnings (log.txt marks them WARN; summary.md lists the first 20 of each kind)
WARNINGS = {'PC_MODE', 'ANR_DIALOG', 'HV_WAKEUP_AUDIT', 'SERIAL_STALE', 'EL2_LOG_DOWN', 'EL2_MISSING', 'EL2_HIGH',
            'EL2_COUNT_DOWN', 'KERNEL_WARN', 'SUSPEND_ABORTED', 'STANDBY_SHORT', 'DEVICE_LOST', 'PROBE_TIMEOUT',
            'KERNEL_LOG_CAPPED', 'HEALTH_DEGRADED', 'CRASH', 'ANR', 'TOMBSTONE', 'SUSPEND_NOT_ENTERED', 'SKIP',
            'MONKEY_CRASH', 'MONKEY_ANR', 'BOOT_REASON', 'RUNDOWN_INVALID', 'RESTORE_FAIL', 'TEE_IRQSOFF'}


# ==== Device access: adb, su and the health snapshot ==================================================================
class Abort(Exception):
    """Ends the run early; Run.execute records the message as an ABORT event (the run is FAIL) and writes the
    summary."""
    pass


class Device:
    """One Android device on adb (by serial number). Every call has a timeout; a timed-out call returns rc None."""
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args, timeout=60):
        """(exit code, stdout + stderr) of `adb -s <serial> <args>`; (None, 'TIMEOUT') when it did not end in time."""
        try:
            p = subprocess.run(['adb', '-s', self.serial, *args], capture_output=True, text=True,
                               errors='replace', timeout=timeout)
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return None, 'TIMEOUT'

    def sh(self, cmd, timeout=60):
        """`adb shell <cmd>` as the shell user; (exit code, output) as adb()."""
        return self.adb('shell', cmd, timeout=timeout)

    def su(self, cmd, timeout=60):  # tablets: `su 0`, never `adb root`
        """`cmd` run as root through `su 0 sh -c`; (exit code, output) as adb()."""
        return self.sh('su 0 sh -c ' + shlex.quote(cmd), timeout=timeout)

    def alive(self):
        """Whether the device answers a trivial adb shell command within 10 s."""
        rc, _ = self.adb('shell', 'true', timeout=10)
        return rc == 0

    def wait_alive(self, timeout):
        """Polls every 5 s until the device answers on adb; False when it did not within `timeout` seconds."""
        end = time.time() + timeout
        while time.time() < end:
            if self.alive():
                return True
            time.sleep(5)
        return False

    def wait_drop(self, timeout):
        """Polls every 1 s until the device stops answering on adb (suspend, reboot, unplug); False when it still
        answers after `timeout` seconds."""
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive():
                return True
            time.sleep(1)
        return False

    def wait_boot(self, timeout):
        """Polls every 5 s until sys.boot_completed is 1; False when it is not within `timeout` seconds."""
        end = time.time() + timeout
        while time.time() < end:
            rc, out = self.sh('getprop sys.boot_completed', timeout=10)
            if rc == 0 and out.strip() == '1':
                return True
            time.sleep(5)
        return False

    def health(self, marker='', since=None, boot=None):
        """Parsed snapshot, or None; self.fail then says why: 'timeout' (no answer in 180 s) or the failure.
        since, boot: the previous snapshot's dmesg_last and boot_id (only newer kernel lines on that boot).
        marker: health.sh writes 'STAB <marker>' to the kernel log (it also shows on the serial console)."""
        args = ' '.join(shlex.quote(str(a)) for a in (marker, since or '', boot or ''))
        # 180 s: under the all-at-once EL2 stress a snapshot (dumpsys, logcat) took over 90 s on T11206 (2026-10-13)
        rc, out = self.su(f'sh {DEV}/health.sh {args}', timeout=180)
        if rc != 0 or 'boot_id=' not in out:
            self.fail = 'timeout' if rc is None else f'rc={rc}: ' + ' '.join(out.split())[-120:]
            return None
        self.fail = None
        return parse_health(out)


def parse_health(out):
    """health.sh output -> {key: value, '_raw': out, '_sec': {section: [lines]}}. The key=value lines come first;
    from the first '--- <name>' line on, every non-empty line belongs to the section named last."""
    h, sec, name = {'_raw': out}, collections.defaultdict(list), None
    for line in out.splitlines():
        if line.startswith('--- '):
            name = line[4:].strip()
        elif name:
            if line.strip():
                sec[name].append(line.rstrip())
        elif '=' in line:
            k, v = line.split('=', 1)
            h[k.strip()] = v.strip()
    h['_sec'] = dict(sec)
    return h


# ==== Serial console and kernel log patterns ==========================================================================
# Fatal markers on the serial console (Linux console and the hypervisor console share the UART on T11206): the
# hypervisor's panic handler (core/kruntime/src/lang_items.rs prints 'XHYPER PANIC:' then 'XHYPER_PANIC_HOLD'), the
# Manager's fatal stop, hypervisor boot errors, a Linux panic, and the boot loader's record of the previous reset.
SERIAL_FATAL = re.compile(rb'XHYPER PANIC|XHYPER_PANIC_HOLD|MANAGER_FATAL|XHYPER_[A-Z_]*ERROR|Kernel panic|'
                          rb'exception_reboot_reason|exception_panic_reason|wdt fiq')
# Kernel lines that are KERNEL_BAD by themselves: crashes, lockups, RCU stalls, hung tasks, external aborts
KERNEL_ERROR = re.compile(r'BUG:|Oops|Unable to handle|soft lockup|hard LOCKUP|rcu.*(stall|self-detected)|'
                          r'blocked for more than|Kernel panic|SError|synchronous external abort|Unhandled')


# ==== EL2 monitor modules =============================================================================================
def ko_load_order(d):
    """Module names of the .ko files in d, ordered by their depends= (a topological sort)."""
    deps = {}  # module name (from the .ko's modinfo name=, else the file name) -> names it depends on
    for f in sorted(os.listdir(d)):
        if f.endswith('.ko'):
            b = open(os.path.join(d, f), 'rb').read()
            n = re.search(rb'\x00name=([^\x00]+)', b)
            dp = re.search(rb'\x00depends=([^\x00]*)', b)
            deps[n.group(1).decode() if n else f[:-3]] = [
                x for x in (dp.group(1).decode().split(',') if dp else []) if x]
    order = []

    # depth first: a module's dependencies (those in d) go before it; `path` breaks a dependency cycle
    def visit(n, path=()):
        if n in order or n in path:
            return
        for m in deps.get(n, []):
            if m in deps:
                visit(m, path + (n,))
        order.append(n)

    for n in deps:
        visit(n)
    return order


# ==== Kernel log grading: preempt/IRQ-off reports, WARN splats, serial console load ===================================
def ktime(line):
    """Kernel time in seconds of a dmesg line ('[  323.317830] ...'), else None."""
    m = re.match(r'\[\s*(\d+(?:\.\d+)?)\]', line)
    return float(m.group(1)) if m else None


def printout_windows(lines):
    """[start, end] of every WARN/BUG splat in the kernel log (from the 'WARNING: CPU' line to its '---[ end trace')."""
    out, start = [], None
    for l in lines:
        if re.search(r'WARNING: CPU|BUG:|Oops', l):
            start = ktime(l)
        elif '---[ end trace' in l and start is not None:
            out.append((start, ktime(l) or start))
            start = None
    return out


# A vendor preempt/IRQ-off report: kind, CPU, task, D (ms the CPU had preemption / IRQs off), F (start, kernel time)
OFF_REPORT = re.compile(r'\b(preemptoff|irqsoff)_warn: C(\d+) T:<\d+>(.*?) D:\s*(\d+(?:\.\d+)?)\s*ms'
                        r'(?: F:(\d+(?:\.\d+)?)s)?')
# Lines that are context, not findings (report continuations, splat ends, resume and wake-pending lines)
CONTEXT_LINE = re.compile(r'(preemptoff|irqsoff)_warn|---\[ end trace|Resume caused by|Wakeup pending')
# The wake interrupt's device in a resume line ('Resume caused by IRQ 127, <device>')
WAKE_IRQ = re.compile(r'Resume caused by (?:misconfigured )?IRQ \d+,? (\S+)')
SERIAL_BPS = 92160      # bytes per second of the serial console: 921600 baud 8N1 (T11206)
CONSOLE_SHARE = 0.5     # bytes stamped in [F - CONSOLE_LEAD, F + D] >= this share of what the line carries in D
CONSOLE_LEAD = 0.2      # s before F that count too: the console window is [F - CONSOLE_LEAD, F + D]
KERNEL_CAP = 2000       # health.sh KCAP: newest kernel lines per snapshot
GROUP_FIRST = 3         # preempt/IRQ-off reports written one by one per (phase, group); the rest are counted
REASON_CLASS = {'console-bound:': 'console', 'waits': 'wait', 'inside': 'splat'}  # first word of kernel_grade's why
# Device suspend/resume steps of the kernel's suspend statistics (health.sh suspend_failed_<step> counters)
DEVICE_STEPS = ('prepare', 'suspend', 'suspend_late', 'suspend_noirq', 'resume_noirq', 'resume_early', 'resume')


def parse_off(line):
    """(kind, cpu, comm, D ms, start s) of a preemptoff_warn / irqsoff_warn report (vendor debug prints: preemption or
    IRQs were off on CPU <cpu> for D ms from kernel time F); None for other lines, the '... at:' continuations too."""
    m = OFF_REPORT.search(line)
    if not m:
        return None
    d, t = float(m.group(4)), ktime(line)
    f = float(m.group(5)) if m.group(5) else (t - d / 1000 if t is not None else None)
    return None if f is None else (m.group(1), int(m.group(2)), m.group(3), d, f)


def irq_intervals(lines):
    """(cpu, start, end, D ms, comm) of every irqsoff report among lines."""
    return [(o[1], o[4], o[4] + o[3] / 1000, o[3], o[2]) for o in map(parse_off, lines) if o and o[0] == 'irqsoff']


def waits_on(off, irqs, before=0.005, after=0.030):
    """The longest irqsoff of another CPU whose interval holds this preempt-off (start >= its start - 5 ms, end <= its
    end + 30 ms): a CPU spinning in a cross call (smp_call_function_single) for a CPU that has IRQs off; else None.
    The IRQ-off CPU prints its own report (13 lines, ~10 ms at line rate) before it takes the IPI: overnight waiters
    ended up to 24 ms after the irqsoff they waited on."""
    _, cpu, _, d, f = off
    hold = [i for i in irqs if i[0] != cpu and f >= i[1] - before and f + d / 1000 <= i[2] + after]
    return max(hold, key=lambda i: i[3], default=None)


class ConsoleHist:
    """Bytes the serial console carried per 0.1 s of kernel time, for the current boot, from the host's line file
    ('HH:MM:SS.mmm [ktime][caller] text'; kernel time = the first [...]; a line without one counts at the last time
    seen). Fed incrementally (a partial last line waits for the next feed, up to PART_MAX bytes); keeps the last KEEP s
    of kernel time; a time of 0 or more than BACK s behind the newest one is a new boot and starts over (the 0: after a
    boot that ended within BACK s, the next one is never that far behind)."""
    BIN, KEEP, BACK, PART_MAX = 0.1, 7200, 100, 1 << 16
    HOST = re.compile(rb'\d\d:\d\d:\d\d\.\d{3} ')
    KT = re.compile(rb'\[\s*([0-9]+\.[0-9]+)\]')

    def __init__(self):
        # bins: {0.1 s bin index: bytes}; part: the unfinished last line; top: newest kernel time seen; cur: kernel
        # time of the current line; pruned: `top` at the last pruning (pruned once top moved 60 s on)
        self.bins, self.part, self.top, self.cur, self.pruned = {}, b'', None, None, None

    def feed(self, chunk):
        """Counts the bytes of the complete lines in `chunk` (plus the part kept from the previous feed)."""
        lines = (self.part + chunk).split(b'\n')
        self.part = lines.pop()
        if len(self.part) > self.PART_MAX:  # no line end in sight: count it as a line
            lines.append(self.part)
            self.part = b''
        for l in lines:
            h = self.HOST.match(l)
            body = l[h.end():] if h else l
            m = self.KT.match(body)
            if m:
                t = float(m.group(1))
                if t == 0.0 or (self.top is not None and t < self.top - self.BACK):
                    self.bins, self.top, self.pruned = {}, None, None
                self.cur, self.top = t, (t if self.top is None else max(self.top, t))
            if self.cur is None:
                continue
            k = int(self.cur / self.BIN)
            self.bins[k] = self.bins.get(k, 0) + len(body.rstrip(b'\r')) + 2  # CR LF on the wire
        if self.top is not None and (self.pruned is None or self.top - self.pruned > 60):
            lo = int((self.top - self.KEEP) / self.BIN)
            self.bins = {k: v for k, v in self.bins.items() if k >= lo}
            self.pruned = self.top

    def bytes_in(self, a, b):
        """Bytes stamped in kernel time [a, b] (an edge bin counts by its overlap)."""
        n = 0.0
        for k in range(int(a / self.BIN), int(b / self.BIN) + 1):
            v = self.bins.get(k)
            if v:
                n += v * max(0.0, min(b, (k + 1) * self.BIN) - max(a, k * self.BIN)) / self.BIN
        return n


def console_load(hist, f, d_ms):
    """(bytes stamped in [F - 0.2 s, F + D], their share of what the serial line carries in D)."""
    n = hist.bytes_in(f - CONSOLE_LEAD, f + d_ms / 1000)
    return n, n / (max(d_ms, 0.001) / 1000 * SERIAL_BPS)


def kernel_grade(line, long_ms=100.0, windows=(), irqs=(), hist=None):
    """(kind, why) of a kernel line: KERNEL_BAD for crashes, lockups, stalls; KERNEL_WARN for WARN_ON splats and short
    or explained preempt/IRQ-off reports; (None, '') for context lines (report continuations, resume reasons).
    preemptoff_warn / irqsoff_warn are vendor debug prints (D = how long preemption / IRQs were off); 717 ms of
    preempt-off preceded the watchdog reset of 2026-10-12, so one of long_ms or more stays an error unless explained:
    - console-bound: the serial console is synchronous (921600 baud), so printing a burst keeps the printing CPU, and
      CPUs spinning for the console, non-preemptible; bytes stamped in [F - 0.2 s, F + D] >= half the line's capacity
      in D (hist: ConsoleHist of the capture; None without one);
    - a preempt-off inside another CPU's irqsoff (irqs: irq_intervals of recent snapshots): it waited in a cross call;
      the irqsoff is the finding and is graded by itself;
    - inside a WARN splat printout (windows: printout_windows)."""
    if KERNEL_ERROR.search(line):
        return 'KERNEL_BAD', ''
    off = parse_off(line)
    if off:
        kind, _, _, d, f = off
        if d < long_ms:
            return 'KERNEL_WARN', ''
        if hist is not None:
            n, share = console_load(hist, f, d)
            if share >= CONSOLE_SHARE:
                a, b = f - CONSOLE_LEAD, f + d / 1000
                return 'KERNEL_WARN', (f'console-bound: {n / 1024:.1f} KiB on the serial console in {a:.2f}-{b:.2f} s, '
                                       f'{share:.0%} of the line rate over D')
        w = waits_on(off, irqs) if kind == 'preemptoff' else None
        if w:
            return 'KERNEL_WARN', f'waits on C{w[0]} irqsoff {w[3]:.0f} ms ({w[4]})'
        if any(f <= e + 0.1 and f + d / 1000 >= s0 for s0, e in windows):
            return 'KERNEL_WARN', 'inside a WARN splat printout'
        return 'KERNEL_BAD', ''
    if CONTEXT_LINE.search(line) and not ('Resume caused by' in line and 'watchdog' in line):  # that one: as before
        return None, ''
    return 'KERNEL_WARN', ''


# ==== Suspend and standby: wake sources ===============================================================================
def wake_info(lines, since=None):
    """(abort device, wake device) from the kernel's resume/abort lines in log order (wake_lines): abort device = from
    the first 'Resume caused by [misconfigured ]IRQ <n>[,] <device>' after a 'Wakeup pending' line (the wake interrupt
    that aborted a suspend); wake device = from the first resume line (what ended the first suspend since the previous
    snapshot), else from the sysfs last_resume_reason. Only lines after kernel time `since` (the previous snapshot)
    count. By device name, never the IRQ number: the same PCIe controller is IRQ 127 under the hypervisor and 134 on
    stock."""
    lines = [l.strip() for l in (lines.split(' | ') if isinstance(lines, str) else lines)]
    abort = wake = None
    pending = False
    for l in lines:
        if ktime(l) is None or (since is not None and ktime(l) <= since):
            continue
        pending = pending or 'Wakeup pending' in l
        m = WAKE_IRQ.search(l)
        if m:
            wake = wake or m.group(1)
            if pending:
                abort, pending = m.group(1), False
    if wake is None:
        m = re.search(r'last_resume_reason \d+ (\S+)', ' '.join(lines))
        wake = m.group(1) if m else None
    return abort, wake


def wake_lines(h):
    """Resume/abort lines of snapshot h in kernel order: from its kernel_bad section (all new lines), else health.sh's
    wakeup_reason (newest three, can miss a pair); plus the sysfs last_resume_reason."""
    kv = (h.get('wakeup_reason') or '').split(' | ')
    sec = [l for l in h['_sec'].get('kernel_bad', []) if 'Resume caused by' in l or 'Wakeup pending' in l]
    return sec + [p for p in kv if p.startswith('last_resume_reason')] if sec else kv


def wake_source(h, prev):
    """What ended a forced suspend: the device of the first resume line since the previous snapshot (a later one can
    be an automatic suspend's), else 'unknown'."""
    return wake_info(wake_lines(h), since=fnum(prev, 'dmesg_last'))[1] or 'unknown'


def standby_summary(cycles, suspend_s):
    """(INFO text, STANDBY_SHORT text or None) of a standby phase; cycles = [(seconds until adb came back or None,
    wake source)]. Short when the median is below half the requested suspend: with USB power connected the charger
    driver arms a wake alarm a few seconds ahead on every suspend (T11206, stock too, 2026-10-14)."""
    backs = sorted(b for b, _ in cycles if b is not None)
    why = collections.Counter(w for _, w in cycles)
    med = statistics.median(backs) if backs else None
    info = (f'standby: {len(cycles)} cycles of {suspend_s} s requested; adb back after median '
            + (f'{med:.0f} s, max {backs[-1]:.0f} s' if backs else '- (never)')
            + '; wake reasons ' + (', '.join(f'{w} x{n}' for w, n in why.most_common()) or '-'))
    if med is None or med >= suspend_s / 2:
        return info, None
    return info, (f'woke after a median {med:.0f} s of the requested {suspend_s} s ({why.most_common(1)[0][0]}): '
                  'long standby not measured; with USB power connected the charger driver arms a wake timer of a few '
                  'seconds on every suspend')


# ==== Battery rundown helpers: lock files, settings, start checks =====================================================
RUNDOWN = f'{DEV}/rundown'  # device directory of the rundown sampler's files (rundown-<time>.csv, rundown.pid)
# the battery's power supply directory into $B: the one named battery, else the first of type Battery
BATTERY_DIR = ('B=/sys/class/power_supply/battery; [ -d $B ] || '
               'B=$(dirname "$(grep -lx Battery /sys/class/power_supply/*/type 2>/dev/null | head -1)")')
# settings the rundown changes, read before and restored after; the global ones are restored through their commands
RUNDOWN_SETTINGS = ('system screen_brightness_mode', 'system screen_brightness', 'system peak_refresh_rate',
                    'system min_refresh_rate', 'system accelerometer_rotation', 'system user_rotation',
                    'global airplane_mode_on', 'global wifi_on', 'global bluetooth_on')
# what the operator must do once per player install (part of the message when playback is not PLAYING)
VLC_PREP = ('the player needs its one-time preparation on this device (README, battery_rundown): open a video in VLC '
            'once, close the video player introduction, set repeat to "repeat one" in the player menu')


def lock_entries(name, fields):
    """{key: fields} of the lines of third_party/<name> with at least `fields` columns (comments skipped)."""
    out = {}
    with open(os.path.join(REPO, 'third_party', name)) as f:
        for line in f:
            p = line.split()
            if len(p) >= fields and not line.startswith('#'):
                out[p[0]] = p
    return out


def sha256_file(path):
    """Hex sha256 of a host file, read in 1 MiB pieces."""
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def parse_volume(text):
    """Index from `cmd media_session volume --get` ('... volume is 3 in range [0..15]'), else None."""
    m = re.search(r'volume is (\d+)', text or '')
    return int(m.group(1)) if m else None


def hw_decoder(text, pid):
    """(listed, codec name): whether dumpsys media.resource_manager lists a hw-video-codec resource for process pid
    (its 'Pid: <n>' section; the whole dump when it has no such sections)."""
    parts = re.split(r'\bPid:\s*(\d+)', text or '')
    body = dict(zip(parts[1::2], parts[2::2])).get(str(pid), '') if len(parts) > 1 else (text or '')
    m = re.search(r'Name:\s*(\S+)', body)
    return 'hw-video-codec' in body, m.group(1) if m else ''


def rundown_apply_cmds(p):
    """Shell commands that put the device into the rundown's fixed state (plan parameters p)."""
    hz = float(p['refresh_hz'])
    radio = (['cmd connectivity airplane-mode enable', 'sleep 2', 'cmd wifi set-wifi-enabled disabled',
              'cmd bluetooth_manager disable'] if p['airplane'] else [])
    # Match content frame rate off (Android's default 1 lets a 60 fps video pull the panel to 60 Hz although
    # min = peak = 90; seen on T11206, 2026-10-14). Not kept across a reboot.
    return radio + ['settings put system screen_brightness_mode 0',
                    f"settings put system screen_brightness {p['brightness']}",
                    f'settings put system peak_refresh_rate {hz:.1f}',
                    f'settings put system min_refresh_rate {hz:.1f}',
                    'cmd display set-match-content-frame-rate-pref 0',
                    'settings put system accelerometer_rotation 0',
                    'settings put system user_rotation 0',
                    f"cmd media_session volume --stream 3 --set {p['volume']}"]


def radio_on(key, value):
    """Whether a radio setting's value means the radio is on now; None when the value was not read.
    global wifi_on: 1 on, 2 on in airplane mode, 3 off by airplane mode (remembered as on for its end), 0 off;
    global bluetooth_on: 1 on, 2 off by airplane mode, 0 off; global airplane_mode_on: 1 on."""
    if value in (None, '', 'null'):
        return None
    return value in ('1', '2') if key == 'global wifi_on' else value == '1'


def same_setting(key, a, b):
    """Whether two values of a setting mean the same (radios: the same on/off state)."""
    return radio_on(key, a) == radio_on(key, b) if key.startswith('global ') else a == b


def rundown_restore_cmds(orig):
    """Shell commands that bring back the settings read before the rundown (orig: rundown_read_settings); radios
    through their commands by their on/off state (radio_on; the airplane mode first, then 2 s for it to settle),
    'null' settings deleted, values not read left alone."""
    air, wifi, bt = (radio_on(k, orig.get(k))
                     for k in ('global airplane_mode_on', 'global wifi_on', 'global bluetooth_on'))
    cmds = []
    if air is not None:
        cmds += ['cmd connectivity airplane-mode ' + ('enable' if air else 'disable'), 'sleep 2']
    if wifi is not None:
        cmds.append('cmd wifi set-wifi-enabled ' + ('enabled' if wifi else 'disabled'))
    if bt is not None:
        cmds.append('cmd bluetooth_manager ' + ('enable' if bt else 'disable'))
    for k in RUNDOWN_SETTINGS:
        v = orig.get(k)
        if k.startswith('system ') and v not in (None, ''):
            cmds.append(f'settings delete {k}' if v == 'null' else f'settings put {k} {shlex.quote(v)}')
    if orig.get('volume') is not None:
        cmds.append(f"cmd media_session volume --stream 3 --set {orig['volume']}")
    cmds.append(f"cmd display set-match-content-frame-rate-pref {orig.get('match_content', 1)}")
    return cmds


def restore_shell(serial, orig):
    """The host command that restores the settings by hand (when the run could not: the device was away)."""
    return f"adb -s {serial} shell {shlex.quote('; '.join(rundown_restore_cmds(orig)))}"


def rundown_checks(st, p, pkg):
    """Reasons the started rundown is not in its fixed state (st: rundown_state), empty when it is."""
    bad = []
    if st.get('playback') != 'PLAYING':
        bad.append(f"playback state {st.get('playback')}, not PLAYING: {VLC_PREP}")
    if pkg not in st.get('focus', ''):
        bad.append(f"{pkg} not in focus ({st.get('focus', '').strip()[:120]})")
    if not st.get('hw', (False,))[0]:
        bad.append(f'no hw-video-codec resource for {pkg} (pid {st.get("pid") or "-"}) '
                   'in dumpsys media.resource_manager')
    if st.get('backlight') != str(p['brightness']):
        bad.append(f"backlight raw value {st.get('backlight')!r}, not {p['brightness']}")
    vs = st.get('vsync', [])
    # every one of the three refresh rate reads must be a number within 0.5 Hz of the plan's
    if not vs or any(not re.fullmatch(r'\d+(\.\d+)?', v) or abs(float(v) - float(p['refresh_hz'])) > 0.5
                     for v in vs):
        bad.append(f"refresh rate {vs}, not {p['refresh_hz']} Hz")
    if p['airplane']:
        if st.get('airplane') != '1':
            bad.append(f"airplane mode {st.get('airplane')!r}, not 1")
        if 'disabled' not in st.get('wifi', '').lower():
            bad.append(f"Wi-Fi not disabled ({st.get('wifi', '').strip()[:60]})")
        if st.get('bluetooth') != '0':
            bad.append(f"Bluetooth {st.get('bluetooth')!r}, not 0")
    if st.get('volume') != p['volume']:
        bad.append(f"media volume {st.get('volume')}, not {p['volume']}")
    if not st.get('sampler'):
        bad.append('the sampler (device/rundown.sh) is not running')
    return bad


# ==== Battery rundown live view: the sampler's lines on the serial console ============================================
def stamp_time(stamp, now):
    """Epoch of a capture's 'HH:MM:SS.mmm' stamp on the host's date at `now` (the day before when that would be
    later than now: a line from before midnight)."""
    h, m, s = stamp.split(':')
    t = datetime.datetime.fromtimestamp(now).replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    e = t.timestamp() + float(s)
    return e - 86400 if e > now + 60 else e


class RundownLive:
    """Follows the sampler's 'RUNDOWN <csv line>' kernel-log lines in a serial capture: appends them to the live file
    (host time from the capture's stamp, then the sample), and returns INFO messages: the first sample on battery, every
    10 % of capacity, and once no sample came for max(300 s, 5 samples) by the time this process saw them (never by
    a file's modification time: it is stale after a host restart) that the device has probably shut down."""
    LINE = re.compile(r'^(\d\d:\d\d:\d\d\.\d{3}) .*?RUNDOWN (.*?)\s*$')

    def __init__(self, path, header, sample_s=60):
        # path: the live file (rundown-live.csv); cols: the sampler's column names (its header line); quiet: seconds
        # without a sample after which the device counts as shut down
        self.path, self.cols, self.quiet = path, header, max(300, 5 * sample_s)
        # part: the unfinished last line; n: samples received; bad: RUNDOWN lines that did not parse
        self.part, self.n, self.bad = b'', 0, 0
        # seen_t: host time the last sample arrived; last: that sample as {column: value}
        self.seen_t = self.start = self.last = None  # start: device epoch of the first sample on battery
        # next_level: capacity (%) at which the next progress message is due; off: shutdown reported (until a sample)
        self.next_level, self.off = 90, False
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write(','.join(['host_time'] + header) + '\n')

    def feed(self, chunk, now):
        """Takes the next bytes of the capture (`now`: host epoch); appends the samples to the live file and returns
        the INFO messages due (an empty chunk only runs the quiet check)."""
        lines = (self.part + chunk).split(b'\n')
        self.part = lines.pop()
        out = []
        for raw in lines:
            if b'RUNDOWN ' not in raw:
                continue
            m = self.LINE.match(raw.decode('utf-8', 'replace').rstrip('\r'))
            vals = next(csv.reader([m.group(2)])) if m else []
            if len(vals) != len(self.cols) or not vals[0].isdigit():
                self.bad += 1
                continue
            r = dict(zip(self.cols, vals))
            ht = stamp_time(m.group(1), now)
            with open(self.path, 'a') as f:
                f.write(datetime.datetime.fromtimestamp(ht).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + ','
                        + m.group(2) + '\n')
            self.n, self.seen_t, self.last = self.n + 1, now, r
            if self.off:
                self.off = False
                out.append(f"rundown: samples again after a pause ({r.get('cap')} %)")
            if self.start is None and r.get('online') == '0':
                self.start = fnum(r, 'epoch')
                out.append(f"rundown: first sample on battery at {m.group(1)[:8]} (host clock): {r.get('cap')} %, "
                           f"{fnum(r, 'volt_uV') / 1e6 if fnum(r, 'volt_uV') else 0:.3f} V")
                self.next_level = min(90, ((num(r, 'cap', 100) - 1) // 10) * 10)
            elif self.start is not None and num(r, 'cap', 100) <= self.next_level:
                cap = num(r, 'cap')
                v, i = fnum(r, 'volt_uV'), fnum(r, 'cur_uA')
                w = f', {v * -i / 1e12:.2f} W' if v is not None and i is not None else ''
                t = fnum(r, 'temp_dC')
                out.append(f"rundown: {cap} % after {(fnum(r, 'epoch') - self.start) / 3600:.2f} h on battery"
                           + (f', {v / 1e6:.3f} V' if v else '') + w + (f', {t / 10:.1f} C' if t is not None else ''))
                self.next_level = ((cap - 1) // 10) * 10
        if self.seen_t is not None and not self.off and now - self.seen_t >= self.quiet and self.start is not None:
            self.off = True
            out.append(f'rundown: no sample for {(now - self.seen_t) / 60:.0f} min (last {self.last.get("cap")} %, '
                       f'{self.n} samples received): the device has probably shut down')
        return out


# ==== TEE idle helpers: Trusty IRQ-off reports between block markers ==================================================
# the blocks of phase tee_idle when the plan gives none: deep idle on, off, on again, 600 s each
TEE_IDLE_BLOCKS = ({'name': 'A1-idle-on', 'deep_idle': True, 'seconds': 600},
                   {'name': 'B-idle-off', 'deep_idle': False, 'seconds': 600},
                   {'name': 'A2-idle-on', 'deep_idle': True, 'seconds': 600})
# a block's kernel-log marker, written by the phase: 'STAB TEE_IDLE <phase>/<block> start|end'
TEE_MARK = re.compile(r'STAB TEE_IDLE (\S+) (start|end)\b')
# the host's time stamp at the start of a serial capture line
HOST_STAMP = re.compile(r'^\d\d:\d\d:\d\d\.\d{3} ')
# kernel time then caller ('[  323.317830][T563@C0]') at the start of a line
CALLER = re.compile(r'^\[\s*\d+(?:\.\d+)?\]\[\s*([^\]]*?)\s*\]')
# every CPU's idle states' disable files (1: the state is not used)
CPUIDLE = '/sys/devices/system/cpu/cpu*/cpuidle/state*/disable'


def caller(body):
    """The caller field after the kernel time ('[  323.317830][T563@C0] ...' -> 'T563@C0'), else None."""
    m = CALLER.match(body)
    return m.group(1) if m else None


def trusty_reports(lines, look=60):
    """Trusty IRQ-off reports among kernel lines: irqsoff_warn reports (vendor debug print 'irqsoff_warn: C<cpu>
    T:<pid><comm> D:<ms>ms F:<s>s', then the stacks where IRQs were disabled and enabled again) whose stack holds
    trusty_std_call_helper (a TEE standard call that kept the CPU's IRQs off). Two formats: the device's dmesg (stack
    lines carry no time stamp and belong to the stamped line above them; only that CPU's 'disabled/enabled IRQ at:'
    records count) and a serial capture (host time, then kernel time and caller on every line, other CPUs' lines in
    between: only the report's caller counts). The stack is looked for in the next `look` lines, up to that CPU's
    (caller's) next report. [{'i': index of the report line, 'cpu', 'comm', 'ms', 'f', 'site'}]; site: the
    trusty_std_call32 offset in the stack ('+0xe4'), '?' when there is none."""
    body = [HOST_STAMP.sub('', l.rstrip('\r'), count=1) for l in lines]
    out = []
    for i, b in enumerate(body):
        o = parse_off(b)
        if not o or o[0] != 'irqsoff':
            continue
        cpu, tag = o[1], caller(b)
        own = re.compile(rf'irqsoff_warn: C{cpu} (?:disabled|enabled) IRQ at:')
        rec, mine = [], False
        for b2 in body[i + 1:i + 1 + look]:
            if ktime(b2) is not None:  # a stamped line starts a record: the report's own, or another one
                o2 = parse_off(b2)
                same = caller(b2) == tag if tag is not None else bool(own.search(b2) or (o2 and o2[1] == cpu))
                if same and o2:  # this CPU's next report
                    break
                mine = same
            if mine:
                rec.append(b2)
                if len(rec) >= 16:
                    break
        text = ' '.join(rec)
        if 'trusty_std_call_helper' in text:
            m = re.search(r'trusty_std_call32\+(0x[0-9a-f]+)', text)
            out.append({'i': i, 'cpu': cpu, 'comm': o[2], 'ms': o[3], 'f': o[4],
                        'site': '+' + m.group(1) if m else '?'})
    return out


def tee_block(lines, name):
    """Trusty IRQ-off reports of block `name`: those whose report line lies after the last 'STAB TEE_IDLE <name> start'
    marker and before its end marker (or the end of the lines); the stack is read across the end marker (other CPUs'
    lines, the marker among them, can come between a report and its stack on a serial console). None without a start
    marker (no copy of the kernel log for this block)."""
    s = None
    for i, l in enumerate(lines):
        m = TEE_MARK.search(l)
        if m and m.group(1) == name and m.group(2) == 'start':
            s = i
    if s is None:
        return None
    e = len(lines)
    for j in range(s + 1, len(lines)):
        m = TEE_MARK.search(lines[j])
        if m and m.group(1) == name and m.group(2) == 'end':
            e = j
            break
    return [r for r in trusty_reports(lines[s:]) if r['i'] < e - s]


def tee_stats(reports, thr):
    """Counts of a block's Trusty IRQ-off reports: all, >= thr ms, >= 100 ms; the five longest; call sites of those
    >= thr ms."""
    ds = sorted((r['ms'] for r in reports), reverse=True)
    return {'reports': len(ds), 'ge_thr': sum(d >= thr for d in ds), 'ge100': sum(d >= 100 for d in ds),
            'longest': ds[:5], 'sites': dict(collections.Counter(r['site'] for r in reports if r['ms'] >= thr))}


def tee_best(b):
    """The copy of a tee_idle block's counts (device log or serial) with the most reports at or above the threshold
    (the device's on a tie), or None when neither has the block's markers."""
    return max((s for s in (b['device'], b['serial']) if s), key=lambda s: s['ge_thr'], default=None)


def stop_cmd(pids, pattern):
    """Shell command that stops each process of `pids` (shell words, e.g. $(cat <pid file>)) with its children, but only
    while its command line still matches the extended regex `pattern`: after a reboot a pid from a file or from an
    earlier call can belong to any process."""
    return (f'for p in {pids}; do tr "\\0" " " < /proc/$p/cmdline 2>/dev/null | grep -qE {shlex.quote(pattern)} && '
            '{ pkill -P $p; kill $p; }; done')


def tee_idle_lines(out):
    """The TEE idle table of summary.md (tee-idle.json written by phase tee_idle), or none."""
    try:
        with open(os.path.join(out, 'tee-idle.json')) as f:
            t = json.load(f)
    except (OSError, ValueError):
        return []
    thr = f"{t['threshold_ms']:g}"
    # one copy's count of a block: '<n >= threshold> (<n >= 100 ms>)', n/a when that copy has no block markers
    cnt = lambda s: f"{s['ge_thr']} ({s['ge100']})" if s else 'n/a'
    L = ['', f'## TEE idle: Trusty IRQ-off reports >= {thr} ms (>= 100 ms)', '',
         '| block | deep idle | s | TEE ops / failures | device log | serial | longest (ms) | call sites |',
         '|---|---|---|---|---|---|---|---|']
    for b in t['blocks']:
        best = tee_best(b) or {}
        L.append(f"| {b['name']} | {'on' if b['deep_idle'] else 'off'} | {b['seconds']} | "
                 f"{b['ops']} / {b['failures']} | "
                 f"{cnt(b['device'])} | {cnt(b['serial'])} | "
                 f"{', '.join(f'{d:.0f}' for d in best.get('longest', [])) or '-'} | "
                 f"{', '.join(f'{k} x{v}' for k, v in best.get('sites', {}).items()) or '-'} |")
    return L


# ==== Small helpers: step names, phase attribution, numbers from snapshots ============================================
def step_names(steps):
    """Phase name of each el2_stress step: a repeated step is <step>-r2, -r3, ... (the first keeps the plain name)."""
    n, out = collections.Counter(), []
    for s in steps:
        n[s] += 1
        out.append(s if n[s] == 1 else f'{s}-r{n[s]}')
    return out


def attribute(marks, boot, t):
    """Phase of the latest (boot, kernel time, phase) marker at or before kernel time t in boot `boot`, else None."""
    best = max(((k, i, ph) for i, (b, k, ph) in enumerate(marks) if b == boot and t is not None and k <= t),
               default=None)
    return best[2] if best else None


def num(h, k, default=0):
    """h[k] as an int (a float value truncated); `default` when missing, empty or not a number."""
    try:
        return int(float(h.get(k, default) or default))
    except ValueError:
        return default


def fnum(h, k):
    """h[k] as a float; None when missing or not a number."""
    try:
        return float(h.get(k))
    except (TypeError, ValueError):
        return None


# ==== The run: class Run ==============================================================================================
class Run:
    """One run of a plan on one device, written into the output directory `out` (events.jsonl, log.txt, health/,
    the phases' files, summary.md). execute() is the entry point: setup, the plan's phases in order, the final scans
    and the summary. Each phase type <t> is a method phase_<t>(p) that takes its plan entry p. Phases sleep through
    wait(), which takes the periodic checkpoints (health snapshot, serial scan, EL2 log); every finding becomes an
    event through event()."""
    def __init__(self, dev, out, plan):
        self.dev, self.out, self.plan = dev, out, plan
        os.makedirs(os.path.join(out, 'health'), exist_ok=True)
        self.ev = open(os.path.join(out, 'events.jsonl'), 'a')
        self.logf = open(os.path.join(out, 'log.txt'), 'a')
        self.last = None            # the previous good snapshot (parse_health), None before the baseline
        self.seen = set()           # kernel, crash buffer and event lines already graded or recorded
        self.nsnap = 0              # snapshots taken (numbers the files health/NNNN-<phase>-<tag>.txt)
        self.expect_reboot = False  # a planned reboot is under way: the next new boot id is not UNEXPECTED_REBOOT
        self.phase = 'setup'        # current phase name: '<n>-<type>', with '/<step or block>' inside some phases
        self.interval = plan.get('health_interval_s', 300)  # seconds between checkpoints
        self.lost_timeout = plan.get('lost_timeout_s', 900)  # seconds a lost device has to come back
        self.next_check = 0         # host time the next checkpoint is due
        self.tools = set()          # stress tools pushed to the device (those built in third_party/out/aarch64)
        self.known_dumps = set()    # pstore files already seen
        self.el2 = plan.get('el2_monitor')  # the plan's EL2 parallel log settings; None: no EL2 log
        self.el2_order = []         # monitor module names in load order
        self.el2_counts = collections.Counter()  # EL2 anomaly events per kind (the first 20 of each are logged)
        self.scanner = None         # el2_report.Scanner over the pulled EL2 logs
        self.serial = None          # path of a serial capture's line file (host-stamped), written by another process
        self.serial_scan_off = 0    # capture bytes scanned for fatal markers so far
        self.serial_marks = []      # (epoch, file size) at each snapshot
        self.serial_stale = False   # SERIAL_STALE reported (again only after output came back)
        self.nserial = 0            # serial windows saved (serial-NNN-<why>.txt)
        self.console, self.console_off = ConsoleHist(), 0  # serial bytes per kernel time (own offset into the capture)
        self.irq_kept = []          # irqsoff intervals of the last 3 snapshots (a waiter's report can come later)
        self.marks = []             # (boot_id, kernel time, phase) of every STAB PHASE_START marker
        self.probe_retry = False    # this snapshot is the one retry after a PROBE_TIMEOUT
        self.good_t = time.time()   # time of the last good snapshot
        self.held, self.held_once = [], set()  # reports whose console bytes had not reached the capture yet
        self.group_n = collections.Counter()  # (phase, group, kind) -> preempt/IRQ-off reports seen
        self.kept_lines = []        # kernel lines of the previous snapshot (a WARN splat can span two snapshots)
        self.degraded = None        # boot id for which HEALTH_DEGRADED was reported
        self.away = False           # battery_rundown: USB unplugged on purpose (no SERIAL_STALE: the device shuts down)

    # ---- Records: events and files -----------------------------------------------------------------------------------
    def event(self, kind, msg='', phase=None, **kw):
        """Records one event: a JSON line in events.jsonl ({t, phase, kind, msg, extra keys such as group and count})
        and '<time> [<phase>] [ERROR |WARN ]<kind> <msg>' in log.txt and on stdout (ERROR for ERRORS, WARN for
        WARNINGS). phase: the phase the finding belongs to when it is not the current one (kernel lines, by kernel
        time)."""
        rec = {'t': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'phase': phase or self.phase, 'kind': kind, 'msg': msg,
               **kw}
        self.ev.write(json.dumps(rec, ensure_ascii=False) + '\n')
        self.ev.flush()
        sev = 'ERROR' if kind in ERRORS else 'WARN' if kind in WARNINGS else ''
        line = f"{rec['t']} [{rec['phase']}] {sev + ' ' if sev else ''}{kind} {msg}"
        self.logf.write(line + '\n')
        self.logf.flush()
        print(line, flush=True)

    def save(self, name, text):
        """Writes `text` to the file `name` in the output directory (replacing it)."""
        with open(os.path.join(self.out, name), 'w') as f:
            f.write(text)

    # ---- Health snapshots and kernel line grading --------------------------------------------------------------------
    def snapshot(self, tag=''):
        """One health snapshot (device/health.sh with the marker '<phase> <n> <tag>'), saved as
        health/NNNN-<phase>[-<tag>].txt and compared with the previous one; returns the parsed snapshot (the one
        lost() takes when the device gave no answer). The first snapshot is the baseline: what is already there
        (pstore files, inventory, kernel / crash buffer / event lines) is not a finding. On the baseline and on every
        new boot: PC_MODE when the device is in desktop mode. On a new boot or after a suspend (tag 'suspend...'): the
        device inventory (check_inventory). A new boot id is a planned reboot (expect_reboot) or unexpected_reboot();
        the line comparison and the held reports start over, new pstore records are KERNEL_BAD, the EL2 log is brought
        up again, and nothing else is compared. On the same boot: HEALTH_DEGRADED (once per boot) when health.sh had
        no usable copy of the kernel log, KERNEL_LOG_CAPPED, the new kernel lines (grade_kernel), new crash buffer
        lines (CRASH) and am_anr event lines (ANR), rising tombstone / ANR file / dropbox crash counts, new pstore
        records (KERNEL_BAD), and a rising suspend failure count: SUSPEND_ABORTED when an alarm was due or a wake
        interrupt was pending, else SUSPEND_FAIL."""
        last = self.last or {}
        h = self.dev.health(f'{self.phase} {self.nsnap + 1} {tag}'.strip(), last.get('dmesg_last'),
                            last.get('boot_id'))
        if self.serial:  # where the capture was at this time: serial_extract starts its windows from these
            self.serial_marks.append((time.time(), os.path.getsize(self.serial) if os.path.exists(self.serial) else 0))
            self.serial_marks = self.serial_marks[-50:]
        if h is None:
            return self.lost(tag)
        self.nsnap, self.good_t = self.nsnap + 1, time.time()
        name = re.sub(r'[^A-Za-z0-9_.-]+', '_', f'{self.phase}{"-" + tag if tag else ""}')
        self.save(f'health/{self.nsnap:04d}-{name}.txt', h['_raw'])
        prev, self.last = self.last, h
        if ((prev is None or h.get('boot_id') != prev.get('boot_id'))
                and (h.get('pc_mode') == '1' or h.get('desk_visible') == '1')):
            self.event('PC_MODE', f"device is in desktop (PC) mode (kylin_rotation_state_saved_before_pc_mode="
                       f"{h.get('pc_mode')}, Desk visible={h.get('desk_visible')}): the plans expect tablet mode")
        if prev is None:  # baseline: what is already there is not a finding
            self.known_dumps = set(h.get('pstore_dumps', '').split())
            self.inventory = set(h['_sec'].get('inventory', []))  # the devices every later boot and resume must have
            for s in ('kernel_bad', 'crash_buffer', 'events'):
                self.seen.update(h['_sec'].get(s, []))
            self.irq_kept = [irq_intervals(h['_sec'].get('kernel_bad', []))]
            return h
        if h['boot_id'] != prev['boot_id'] or tag.startswith('suspend'):
            self.check_inventory(h, tag)
        # A new boot: classify the reboot, start the comparison over
        if h['boot_id'] != prev['boot_id']:
            if self.expect_reboot:
                self.expect_reboot = False
            else:
                self.unexpected_reboot(prev, h)
            for s in ('kernel_bad', 'crash_buffer', 'events'):  # new boot: compare from here on
                self.seen.update(h['_sec'].get(s, []))
            if self.held:  # the old boot's reports that waited for their console bytes: graded with what is there
                self.grade_lines(self.held, prev.get('boot_id'), self.kept_lines, self.console if self.serial else None)
            lines = h['_sec'].get('kernel_bad', [])
            self.irq_kept, self.held, self.held_once, self.kept_lines = [irq_intervals(lines)], [], set(), lines
            new_dumps = set(h.get('pstore_dumps', '').split()) - self.known_dumps
            if new_dumps:
                self.known_dumps |= new_dumps
                self.event('KERNEL_BAD', 'new panic/oops record in pstore after the reboot: '
                           f'{" ".join(sorted(new_dumps))}')
            if self.el2:
                self.el2_up()
            return h
        # The same boot: compare with the previous snapshot
        if (not h.get('dmesg_last') or h.get('kernel_src', 'file') != 'file') and self.degraded != h['boot_id']:
            self.degraded = h['boot_id']
            self.event('HEALTH_DEGRADED', f"health.sh had no usable copy of the kernel log (kernel_src="
                       f"{h.get('kernel_src')}, dmesg_last={h.get('dmesg_last')!r}; /data full or unwritable, or no "
                       'awk): kernel lines are the last 200 per snapshot, older ones can be missed (once per boot)')
        if num(h, 'kernel_new') > KERNEL_CAP:
            self.event('KERNEL_LOG_CAPPED', f"{num(h, 'kernel_new')} new kernel lines of interest since the last "
                       f'snapshot; only the newest {KERNEL_CAP} were read, findings among the older ones are lost')
        self.grade_kernel(h)
        for s, kind in (('crash_buffer', 'CRASH'), ('events', 'ANR')):
            for line in h['_sec'].get(s, []):
                if line not in self.seen:
                    self.seen.add(line)
                    if kind != 'ANR' or 'am_anr' in line:
                        self.event(kind, line[:300])
        for k, kind in (('tombstones', 'TOMBSTONE'), ('anr_files', 'ANR'), ('dropbox_crash', 'CRASH')):
            if num(h, k) > num(prev, k):
                self.event(kind, f'{k} {num(prev, k)} -> {num(h, k)}')
        new_dumps = set(h.get('pstore_dumps', '').split()) - self.known_dumps
        if new_dumps:
            self.known_dumps |= new_dumps
            self.event('KERNEL_BAD', f'new panic/oops record in pstore: {" ".join(sorted(new_dumps))}')
        adev, _ = wake_info(wake_lines(h), since=fnum(prev, 'dmesg_last'))
        # last_failed_dev keeps the last device that ever failed in this boot: it is this attempt's only when a device
        # step counter rose (overnight 2026-10-13, six wake aborts still showed the alarmtimer of an earlier one)
        rose = any(num(h, f'suspend_failed_{s}') > num(prev, f'suspend_failed_{s}') for s in DEVICE_STEPS)
        dev = h.get('suspend_last_failed_dev', '') if rose else ''
        if num(h, 'suspend_fail') > num(prev, 'suspend_fail') and dev.startswith('alarmtimer'):
            # alarmtimer refuses suspend when an alarm is due within 2 s: the kernel's normal behaviour, not a failure
            self.event('SUSPEND_ABORTED', f"fail {num(prev, 'suspend_fail')} -> {num(h, 'suspend_fail')}: alarm due "
                       f"(last_failed_dev={h.get('suspend_last_failed_dev')})")
        elif num(h, 'suspend_fail') > num(prev, 'suspend_fail') and not rose and adev:
            # A wake interrupt that arrives while the CPUs are frozen aborts the suspend (the kernel's normal behaviour;
            # seen with the Wi-Fi PCIe controller right after network load, on stock too)
            self.event('SUSPEND_ABORTED', f"fail {num(prev, 'suspend_fail')} -> {num(h, 'suspend_fail')}: "
                       f"wake interrupt pending from {adev} ({h.get('wakeup_reason', '')[:200]})")
        elif num(h, 'suspend_fail') > num(prev, 'suspend_fail'):
            self.event('SUSPEND_FAIL', f"fail {num(prev, 'suspend_fail')} -> {num(h, 'suspend_fail')}, "
                       f"last_failed_dev={h.get('suspend_last_failed_dev')} step={h.get('suspend_last_failed_step')}")
        return h

    def grade_kernel(self, h):
        """New kernel_bad lines of snapshot h, in two passes: first the irqsoff intervals (this snapshot and the
        previous two: a waiter's report can arrive a snapshot later), then each new line (kernel_grade), with the
        console histogram brought up to date first; a long report whose console bytes are not all in the capture yet
        waits one snapshot. A line goes to the phase whose marker precedes its start. Preempt/IRQ-off reports are
        grouped per phase by (kind, report, task with digits as N, reason): the first GROUP_FIRST of a group are
        written one by one, the rest as one event per group and snapshot with their count."""
        lines = h['_sec'].get('kernel_bad', [])
        self.irq_kept = (self.irq_kept + [irq_intervals(lines)])[-3:]
        held, self.held = self.held, []
        self.grade_lines(held + lines, h.get('boot_id'), self.kept_lines + lines, self.console_update(), hold=True)
        self.kept_lines = lines

    def grade_lines(self, lines, boot, context, hist, hold=False):
        """Grades the unseen lines of boot `boot` (grade_kernel); context: the lines for WARN splat windows; hist: the
        console histogram (None without a capture). hold: a preempt/IRQ-off report of 100 ms or more whose end the
        histogram has not reached yet goes to self.held (once per line) and is graded with the next snapshot."""
        irqs = {i for batch in self.irq_kept for i in batch}
        windows = printout_windows(context)
        more = {}  # (phase, group, kind) -> [count, max D, its F]
        for line in lines:
            if line in self.seen:
                continue
            off = parse_off(line)
            if (hold and hist is not None and hist.top is not None and off and off[3] >= 100.0
                    and line not in self.held_once and hist.top < off[4] + off[3] / 1000):
                self.held.append(line)
                self.held_once.add(line)
                continue
            self.seen.add(line)
            kind, why = kernel_grade(line, windows=windows, irqs=irqs, hist=hist)
            if not kind:
                continue
            ph = attribute(self.marks, boot, off[4] if off else ktime(line)) or self.phase
            tail = f' (collected in {self.phase})' if ph != self.phase else ''
            if not off:
                self.event(kind, line[:300] + tail, phase=ph)
                continue
            cls = 'short' if off[3] < 100.0 else REASON_CLASS.get(why.split(' ')[0], 'none')
            group = f"{off[0]} {re.sub(r'[0-9]+', 'N', off[2])} {cls}"
            self.group_n[(ph, group, kind)] += 1
            if self.group_n[(ph, group, kind)] <= GROUP_FIRST:  # the first ones of a group: one event each
                self.event(kind, line[:300] + (f' [{why}]' if why else '') + tail, phase=ph, group=group)
                continue
            m = more.setdefault((ph, group, kind), [0, 0.0, 0.0])  # the rest: counted, one event per group below
            m[0] += 1
            if off[3] > m[1]:
                m[1], m[2] = off[3], off[4]
        for (ph, group, kind), (n, d, f) in more.items():
            self.event(kind, f'{n} more like this: {group}, max {d:.3f} ms at F={f:.6f}s'
                       + (f' (collected in {self.phase})' if ph != self.phase else ''), phase=ph, group=group, count=n)

    def console_update(self):
        """The console histogram, fed up to the end of the serial capture; None without a capture (or when it cannot
        be read: the capture can be replaced while a run reads it)."""
        if not self.serial:
            return None
        try:
            end = os.path.getsize(self.serial)
            if end < self.console_off:  # a new capture file
                self.console, self.console_off = ConsoleHist(), 0
            with open(self.serial, 'rb') as f:
                f.seek(self.console_off)
                while self.console_off < end:
                    b = f.read(min(8 << 20, end - self.console_off))
                    if not b:
                        break
                    self.console.feed(b)
                    self.console_off += len(b)
        except OSError:
            return None
        return self.console

    def mark_phase(self):
        """Writes 'STAB PHASE_START <phase>' to the kernel log and reads back its kernel time in the same call: kernel
        lines are attributed to the phase whose marker is the latest one before them (snapshots collect a step's last
        lines during the next step)."""
        m = f'STAB PHASE_START {self.phase}'
        _, out = self.dev.su(f'echo {shlex.quote(m)} > /dev/kmsg; cat /proc/sys/kernel/random/boot_id; '
                             f'dmesg | grep -F {shlex.quote(m)} | tail -1', timeout=60)
        lines = [l.strip() for l in (out or '').splitlines()]
        boot = next((l for l in lines if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f-]{27}', l)), None)
        t = next((ktime(l) for l in lines if l.endswith(m) and ktime(l) is not None), None)
        if boot and t is not None:
            self.marks.append((boot, t, self.phase))
        elif self.last:  # not read back (e.g. a loaded device): from the last snapshot on, the collecting phase
            self.marks.append((self.last.get('boot_id'), fnum(self.last, 'dmesg_last') or 0.0, None))
            self.event('INFO', f'phase marker not read back ({" ".join((out or "").split())[-80:]}): kernel lines '
                       'after the last snapshot go to the phase that collects them')

    def check_inventory(self, h, tag):
        """After a reboot or a resume every device of the first snapshot must be back (PCI, USB, sound cards, network
        interfaces, input devices): a driver that failed to probe or to resume drops out."""
        cur = set(h['_sec'].get('inventory', []))
        if not cur or not getattr(self, 'inventory', None):
            return
        missing, extra = sorted(self.inventory - cur), sorted(cur - self.inventory)
        if missing:
            self.event('DEVICE_MISSING', f'{tag}: {len(missing)} missing: ' + '; '.join(missing)[:300])
        if extra:
            self.event('INFO', f'{tag}: new in the inventory: ' + '; '.join(extra)[:200])

    def lost(self, tag):
        """No health answer. A probe that timed out (under full load a snapshot can outlast its 180 s) on a device that
        answers adb within 60 s with the same boot id: PROBE_TIMEOUT and one retry; a retry that fails too is
        DEVICE_GONE (a live device whose probe keeps failing would otherwise never be checked again). Any other
        failure: DEVICE_LOST, then wait up to lost_timeout."""
        why = getattr(self.dev, 'fail', None) or 'no answer'
        self.event('INFO', f'no health answer ({tag}): {why}; waiting')
        t0 = time.time()
        if why == 'timeout' and self.last and self.dev.wait_alive(60):
            _, out = self.dev.sh('cat /proc/sys/kernel/random/boot_id', timeout=30)
            if (out or '').strip() == self.last.get('boot_id'):
                self.event('PROBE_TIMEOUT', f'health probe timed out ({tag}), back after {time.time() - t0:.0f} s, '
                           'same boot')
                if self.probe_retry:  # not the time since the last good snapshot: a 1500 s standby is longer
                    self.serial_extract('probe-fails')
                    self.event('DEVICE_GONE', 'health probe fails on a live device: the retry timed out too (no good '
                               f'snapshot for {time.time() - self.good_t:.0f} s)')
                    raise Abort('health probe fails')
                self.probe_retry = True
                try:
                    return self.snapshot(tag + '-after-timeout')
                finally:
                    self.probe_retry = False
        self.event('DEVICE_LOST', f'no adb/health answer ({tag}); waiting up to {self.lost_timeout} s')
        if not self.dev.wait_alive(max(0, self.lost_timeout - (time.time() - t0))):
            self.serial_extract('device-gone')
            self.event('DEVICE_GONE', f'not back after {self.lost_timeout} s: hung or powered off; a person must look '
                       '(power button, serial console)')
            raise Abort('device gone')
        back = time.time() - t0
        if not self.dev.wait_boot(400):
            self.event('BOOT_FAIL', 'adb back but sys.boot_completed never became 1 within 400 s')
            raise Abort('boot did not complete')
        self.event('INFO', f'device back after {back:.0f} s')
        self.serial_extract('device-lost')
        h = self.dev.health()
        if h is None:
            self.event('DEVICE_GONE', f'back on adb but the health probe fails ({getattr(self.dev, "fail", None)})')
            raise Abort('health fails')
        return self.snapshot(tag + '-after-lost')

    def unexpected_reboot(self, prev, h):
        """A new boot id that no phase planned (prev, h: the snapshots before and after): the pstore files the new
        snapshot lists (the previous boot's panic / console records) copied into pstore-NNNN/ (NNNN: events so far + 1),
        the first panic, watchdog or preempt-off line among them as a hint, a serial window (serial_extract), and
        UNEXPECTED_REBOOT with the boot reasons (sys.boot.reason, its history, the boot loader's)."""
        n = sum(1 for _ in open(os.path.join(self.out, 'events.jsonl'))) + 1
        d = os.path.join(self.out, f'pstore-{n:04d}')
        os.makedirs(d, exist_ok=True)
        files = h.get('pstore', '').split()
        for f in files:
            _, txt = self.dev.su(f'cat /sys/fs/pstore/{f}', timeout=60)
            with open(os.path.join(d, f), 'w') as fh:
                fh.write(txt)
        hint = ''
        for f in files:
            txt = open(os.path.join(d, f), errors='replace').read()
            m = re.search(r'(Kernel panic[^\n]*|wdt[^\n]*|watchdog[^\n]*|preemptoff_warn[^\n]*)', txt)
            if m:
                hint = f'{f}: {m.group(1)[:160]}'
                break
        self.serial_extract('unexpected-reboot')
        self.event('UNEXPECTED_REBOOT', f"boot_id {prev['boot_id'][:8]} -> {h['boot_id'][:8]}; sys.boot.reason="
                   f"{h.get('boot_reason')} history={h.get('boot_reason_last')} bootloader={h.get('bootloader_reason')}"
                   f"; pstore {' '.join(files)} saved to {os.path.basename(d)}; {hint}")

    # ---- Serial console: windows and scans ---------------------------------------------------------------------------
    def serial_extract(self, why, before_s=600, cap=30 << 20):
        """Serial lines from about before_s seconds before now to the end of the capture, into serial-NNN-<why>.txt.
        The start is the capture size at the last snapshot at least before_s ago (serial_marks), else at the oldest
        mark; never more than `cap` bytes (30 MiB)."""
        if not self.serial or not os.path.exists(self.serial):
            return
        end = os.path.getsize(self.serial)
        start = next((off for t, off in reversed(self.serial_marks) if t <= time.time() - before_s),
                     self.serial_marks[0][1] if self.serial_marks else max(0, end - cap))
        start = max(start, end - cap)
        self.nserial += 1
        name = f'serial-{self.nserial:03d}-{why}.txt'
        with open(self.serial, 'rb') as f, open(os.path.join(self.out, name), 'wb') as o:
            f.seek(start)
            o.write(f.read(end - start))
        self.event('INFO', f'serial window saved: {name} ({(end - start) >> 10} KiB)')

    def serial_scan(self):
        """Scans the capture's bytes added since the last scan: SERIAL_STALE once when nothing was added (not while
        the device is away on purpose), HV_WAKEUP_AUDIT for the first 5 XHYPER_VCPU_WAKEUP_LOST lines, SERIAL_FATAL
        for the first 10 fatal marker lines (SERIAL_FATAL pattern) and then a serial window."""
        if not self.serial or not os.path.exists(self.serial):
            return
        end = os.path.getsize(self.serial)
        if end == self.serial_scan_off:
            if not self.serial_stale and not self.away:
                self.serial_stale = True
                self.event('SERIAL_STALE', f'no serial output since the last check ({self.serial}): capture stopped or '
                           'the adapter/wires are off')
            return
        self.serial_stale = False
        with open(self.serial, 'rb') as f:
            f.seek(self.serial_scan_off)
            chunk = f.read(end - self.serial_scan_off)
        self.serial_scan_off = end
        for l in [l for l in chunk.splitlines() if b'XHYPER_VCPU_WAKEUP_LOST' in l][:5]:
            self.event('HV_WAKEUP_AUDIT', l.decode('utf-8', 'replace')[:240])
        hits = [l for l in chunk.splitlines() if SERIAL_FATAL.search(l)]
        for l in hits[:10]:
            self.event('SERIAL_FATAL', l.decode('utf-8', 'replace')[:300])
        if hits:
            self.serial_extract('serial-fatal')

    # ---- Core loop: checkpoint (wait() follows the EL2 section) ------------------------------------------------------
    def checkpoint(self, tag=''):
        """When due (every self.interval seconds): a health snapshot, a serial scan and, with the EL2 parallel log, a
        pull and scan of the EL2 logs; otherwise nothing."""
        if time.time() >= self.next_check:
            self.snapshot(tag)
            self.serial_scan()
            if self.el2:
                self.el2_pull_scan()
            self.next_check = time.time() + self.interval

    # ---- EL2 parallel log: monitor modules and the device-side sampling loop -----------------------------------------
    def el2_setup(self):
        """Prepares the EL2 parallel log (plan key el2_monitor): the monitor's .ko files (directory `artifacts`, with
        environment variables expanded) and device/el2log.sh pushed to the device, logs of earlier runs removed there,
        the cpufreq policies saved (el2-policies.txt), the scanner created, then el2_up(). EL2_LOG_DOWN and no EL2 log
        for this run when the directory is missing or the push fails."""
        art = os.path.expanduser(os.path.expandvars(self.el2['artifacts']))
        if '$' in art or not os.path.isdir(art):
            self.event('EL2_LOG_DOWN', f"monitor module directory not found: {self.el2['artifacts']} -> {art} "
                       '(set XHYPER_MONITOR_ARTIFACTS)')
            self.el2 = None
            return
        self.el2_order = ko_load_order(art)
        kos = [os.path.join(art, f'{m}.ko') for m in self.el2_order]
        # Logs of earlier runs were pulled by those runs; a stale log would be scanned again as this run's data.
        self.dev.su(f'kill $(cat {DEV}/el2/el2log.pid 2>/dev/null) 2>/dev/null; '
                    f'rm -f {DEV}/el2/*.log {DEV}/el2/el2log.pid; '
                    f'mkdir -p {DEV}/xhmon {DEV}/el2 && chmod 777 {DEV}/xhmon {DEV}/el2')
        rc, out = self.dev.adb('push', *kos, os.path.join(REPO, 'device', 'el2log.sh'), f'{DEV}/xhmon/', timeout=300)
        if rc != 0:
            self.event('EL2_LOG_DOWN', 'push of monitor modules failed: ' + out[-200:])
            self.el2 = None
            return
        _, pol = self.dev.sh('for p in /sys/devices/system/cpu/cpufreq/policy*; do '
                             'echo "${p##*/}:$(tr " " "," < $p/related_cpus)"; done', timeout=30)
        self.save('el2-policies.txt', pol.strip() + '\n')
        os.makedirs(os.path.join(self.out, 'el2'), exist_ok=True)
        self.scanner = el2_report.Scanner(self.out)
        self.event('INFO', f"EL2 log: modules {' '.join(self.el2_order)}; policies {' '.join(pol.split())}")
        self.el2_up()

    def el2_up(self):
        """Monitor modules loaded, monitor counting, sampling loop running (all idempotent; after every boot)."""
        mods = ' '.join(self.el2_order)
        iv = self.el2.get('interval_s', 1)
        # Modules loaded unless the monitor's /proc file is there; the monitor started unless its counts move; the
        # sampling loop started unless its pid file names a running el2log.sh. Prints EL2_UP at the end (NO_STATS and
        # exit 1 when the monitor's /proc file is missing).
        cmd = (f'if [ ! -e /proc/xcore_stats ]; then for m in {mods}; do grep -q "^$m " /proc/modules || '
               f'insmod {DEV}/xhmon/$m.ko || echo "INSMOD_FAIL $m"; done; fi; '
               '[ -e /proc/xcore_stats ] || { echo NO_STATS; exit 1; }; '
               # Counting means the counts move: a stopped monitor keeps its committed (non-zero) counts.
               'a=$(tail -n +2 /proc/xcore_stats | awk "{s += \\$2} END {print s}"); sleep 0.5; '
               'b=$(tail -n +2 /proc/xcore_stats | awk "{s += \\$2} END {print s}"); '
               '[ "$b" != "$a" ] || { echo 1 > /proc/xcore_stats; echo STARTED_MONITOR; }; '
               f'pid=$(cat {DEV}/el2/el2log.pid 2>/dev/null); '
               'if [ -z "$pid" ] || ! tr "\\0" " " < /proc/$pid/cmdline 2>/dev/null | grep -q el2log; then '
               f'nohup sh {DEV}/xhmon/el2log.sh {DEV}/el2 {iv} > /dev/null 2>&1 & echo STARTED; fi; echo EL2_UP')
        rc, out = self.dev.su(cmd, timeout=120)
        if 'EL2_UP' not in out:
            self.event('EL2_LOG_DOWN', 'monitor/log not up: ' + ' '.join(out.split())[-200:])
            return False
        self.event('INFO', 'EL2 log up' + (' (monitor started)' if 'STARTED_MONITOR' in out else '')
                   + (' (loop started)' if '\nSTARTED' in out or out.startswith('STARTED') else ''))
        return True

    def el2_pull_scan(self):
        """Pulls the device's EL2 logs into el2/ and scans the new samples (el2_report.Scanner): the first 20 anomalies
        of each kind are events, then one 'more of these' event; all are counted in el2-summary.md."""
        self.dev.adb('pull', f'{DEV}/el2/.', os.path.join(self.out, 'el2'), timeout=300)
        if not self.scanner:
            return
        for kind, ep, msg in self.scanner.scan():
            self.el2_counts[kind] += 1
            if self.el2_counts[kind] <= 20:  # the rest is in el2-summary.md
                self.event(kind, msg)
            elif self.el2_counts[kind] == 21:
                self.event(kind, 'more of these: counted in el2-summary.md, not logged one by one')

    # ---- Core loop: wait ---------------------------------------------------------------------------------------------
    def wait(self, seconds, tag=''):
        """Sleeps `seconds` in steps of at most 15 s, taking the checkpoints that fall due (tag: their snapshot tag)."""
        end = time.time() + seconds
        while time.time() < end:
            self.checkpoint(tag)
            time.sleep(min(15, max(0, end - time.time())))

    # ---- Setup: device files, identity, baseline; APKs, dialogs, tools, screen ---------------------------------------
    def setup(self, expect_sha):
        """Prepares the device and the run: the device must answer on adb (else Abort); device/health.sh,
        el2stress.sh, rundown.sh, klogwatch.sh and the stress tools built in third_party/out/aarch64 (stress-ng,
        stressapptest, cyclictest, hackbench; self.tools) pushed to DEV; boot_a's sha256, the build fingerprint, uname
        and the kernel command line saved in identity.txt; IDENTITY_MISMATCH and Abort when boot_a's sha256 does not
        start with expect_sha; with a serial capture, the scan starts at its current end (the console histogram 1 MiB
        before it); the baseline snapshot; the EL2 parallel log when the plan has one; the first checkpoint due one
        interval later."""
        if not self.dev.alive():
            raise Abort(f'device {self.dev.serial} not reachable')
        self.dev.su(f'mkdir -p {DEV}/tmp && chmod 777 {DEV} {DEV}/tmp')
        files = [os.path.join(REPO, 'device', f) for f in ('health.sh', 'el2stress.sh', 'rundown.sh', 'klogwatch.sh')]
        tdir = os.path.join(REPO, 'third_party', 'out', 'aarch64')
        for t in ('stress-ng', 'stressapptest', 'cyclictest', 'hackbench'):
            if os.path.exists(os.path.join(tdir, t)):
                files.append(os.path.join(tdir, t))
                self.tools.add(t)
        rc, out = self.dev.adb('push', *files, f'{DEV}/', timeout=300)
        if rc != 0:
            raise Abort('adb push failed: ' + out[-300:])
        self.dev.su(f'chmod 755 {DEV}/*')
        _, sha = self.dev.su('sha256sum /dev/block/by-name/boot_a', timeout=120)
        _, ident = self.dev.sh('getprop ro.build.fingerprint; uname -a; cat /proc/cmdline', timeout=30)
        self.save('identity.txt', f'boot_a {sha.strip()}\n{ident}\ntools {sorted(self.tools)}\n')
        self.event('INFO', f"boot_a {sha.split()[0][:16] if sha.split() else '?'}; tools {sorted(self.tools)}")
        if expect_sha and not sha.startswith(expect_sha):
            self.event('IDENTITY_MISMATCH', f'boot_a {sha[:16]} is not the expected {expect_sha}')
            raise Abort('identity mismatch')
        if self.serial:
            self.serial_scan_off = os.path.getsize(self.serial) if os.path.exists(self.serial) else 0
            self.console_off = max(0, self.serial_scan_off - (1 << 20))  # console bytes from a little before the start
            self.event('INFO', f'serial capture {self.serial} from byte {self.serial_scan_off}')
        self.snapshot('baseline')
        if self.el2:
            self.el2_setup()
        self.next_check = time.time() + self.interval

    def ensure_apks(self, keys):
        """Install the APKs named in third_party/apks.lock from STAB_APK_DIR, after checking their sha256.
        SKIP when a key is not in the lock, when its file is not in STAB_APK_DIR and the package is not installed (an
        installed package without a file is left as it is), when the file's sha256 differs, or when `adb install`
        does not answer Success (INFO when it does)."""
        lock = {}  # key -> [key, file name, package, version, sha256, install flags or '-']
        for line in open(os.path.join(REPO, 'third_party', 'apks.lock')):
            f = line.split()
            if len(f) >= 6 and not line.startswith('#'):
                lock[f[0]] = f
        d = os.environ.get('STAB_APK_DIR', '')
        for k in keys:
            if k not in lock:
                self.event('SKIP', f'apk {k}: not in third_party/apks.lock')
                continue
            _, fname, pkg, ver, sha, flags = lock[k][:6]
            _, have = self.dev.sh(f'pm path {pkg}', timeout=30)
            path = os.path.join(d, fname)
            if not d or not os.path.exists(path):
                if 'package:' not in have:
                    self.event('SKIP', f'apk {k}: {fname} not found in STAB_APK_DIR ({d or "unset"})')
                continue
            import hashlib
            h = hashlib.sha256(open(path, 'rb').read()).hexdigest()
            if h != sha:
                self.event('SKIP', f'apk {k}: {fname} sha256 {h[:16]} is not the locked {sha[:16]}')
                continue
            args = ['install', '-r'] + ([flags] if flags != '-' else []) + [path]
            rc, out = self.dev.adb(*args, timeout=600)
            self.event('INFO' if 'Success' in out else 'SKIP',
                       f'apk {k} ({pkg} {ver}): ' + out.strip().splitlines()[-1][:120])

    def dismiss_anr_dialogs(self, tag, tries=4):
        """A not-responding dialog left on screen blocks every later UI step: answer 'wait' (等待) and record it
        (ANR_DIALOG); the button is found in a uiautomator dump, else the dialog gets the back key. Up to `tries`
        dialogs, 2 s apart."""
        for _ in range(tries):
            _, f = self.dev.sh('dumpsys window | grep -m1 mCurrentFocus', timeout=60)
            if 'Not Responding' not in f:
                return
            _, x = self.dev.sh('uiautomator dump /sdcard/stab-ui.xml >/dev/null 2>&1; cat /sdcard/stab-ui.xml',
                               timeout=90)
            m = re.search(r'text="(?:等待|Wait)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', x or '')
            who = re.search(r'Not Responding: ([^}\s]+)', f)
            self.event('ANR_DIALOG', f'{tag}: dialog for {who.group(1) if who else "?"} answered with wait')
            if m:
                a, b, c, d = map(int, m.groups())
                self.dev.sh(f'input tap {(a + c) // 2} {(b + d) // 2}', timeout=30)
            else:
                self.dev.sh('input keyevent KEYCODE_BACK', timeout=30)
            time.sleep(2)

    def tool(self, name):
        """Whether stress tool `name` was pushed (built on the host); SKIP when not, the caller skips its step."""
        if name in self.tools:
            return True
        self.event('SKIP', f'{name} not built (third_party/out/aarch64/{name}); step skipped')
        return False

    def screen(self, on):
        """Screen on (wake-up key, keyguard dismissed) or off (sleep key)."""
        if on:
            self.dev.sh('input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard', timeout=30)
        else:
            self.dev.sh('input keyevent KEYCODE_SLEEP', timeout=30)

    # ---- Phase reboot_loop: repeated reboots -------------------------------------------------------------------------
    def phase_reboot_loop(self, p):
        """Reboot loop: the boot path under the hypervisor and the bring-up of every CPU, by repeated reboots.

        Each of `count` (default 10) rounds: a snapshot (pre-reboot<i>), a planned `adb reboot` (expect_reboot), 15 s,
        up to 300 s for adb and 400 s more for sys.boot_completed, `settle_s` (default 60) s, and a snapshot
        (reboot<i>) on the new boot, which also checks the device inventory and new pstore records.
        Records: INFO 'reboot <i>: booted in <s> s, reason <sys.boot.reason>, pstore <files>'; BOOT_REASON (warning)
        when sys.boot.reason does not start with 'reboot'; BOOT_FAIL (error) and Abort when the device is not booted
        within 700 s or its boot id did not change (the reboot did not happen). Nothing to restore."""
        for i in range(1, p.get('count', 10) + 1):
            pre = self.snapshot(f'pre-reboot{i}')
            self.expect_reboot = True
            t0 = time.time()
            self.dev.adb('reboot', timeout=60)
            time.sleep(15)
            if not self.dev.wait_alive(300) or not self.dev.wait_boot(400):
                self.event('BOOT_FAIL', f'reboot {i}: not booted within 700 s')
                raise Abort('boot failed')
            up = time.time() - t0
            time.sleep(p.get('settle_s', 60))
            h = self.snapshot(f'reboot{i}')
            if h.get('boot_id') == pre.get('boot_id'):
                self.expect_reboot = False
                self.event('BOOT_FAIL', f'reboot {i}: boot id unchanged, the reboot did not happen')
                raise Abort('reboot did not happen')
            reason = h.get('boot_reason', '')
            if not reason.startswith('reboot'):
                self.event('BOOT_REASON', f'reboot {i}: sys.boot.reason={reason}')
            self.event('INFO', f'reboot {i}: booted in {up:.0f} s, reason {reason}, pstore {h.get("pstore")}')

    # ---- Phase soak: monkey, stress-ng bursts, screen cycles, music --------------------------------------------------
    def stress_burst(self, seconds, args, tag):
        """One stress-ng run of `seconds` with `args` as root in DEV/tmp (also used by monitor_soak); its output saved
        as stress-<tag>.txt. Exit 0: INFO; exit 3 (a stressor that cannot run here): SKIP with the skip lines; any
        other exit: STRESS_FAIL with the fail / error lines; no exit code: DEVICE_LOST when adb timed out, else
        STRESS_FAIL, and a snapshot. SKIP (tool()) when stress-ng is not built."""
        if not self.tool('stress-ng'):
            return
        cmd = (f'cd {DEV}/tmp && {DEV}/stress-ng {args} --timeout {seconds}s --metrics-brief --temp-path {DEV}/tmp; '
               'echo STRESS_RC=$?')
        rc, out = self.dev.su(cmd, timeout=seconds + 180)
        self.save(f'stress-{tag}.txt', out)
        m = re.search(r'STRESS_RC=(\d+)', out)
        src = int(m.group(1)) if m else None
        if src is None:
            self.event('DEVICE_LOST' if rc is None else 'STRESS_FAIL', f'stress-ng {tag}: no exit code (adb rc {rc})')
            self.snapshot('after-stress')
        elif src == 0:
            self.event('INFO', f'stress-ng {tag}: exit 0')
        elif src == 3:  # a stressor could not run here (not implemented, no resource): not a failure of the device
            self.event('SKIP', f'stress-ng {tag}: exit 3 (stressor not runnable here); ' +
                       ' | '.join(l for l in out.splitlines() if re.search(r'skip|not implemented', l, re.I))[:300])
        else:
            self.event('STRESS_FAIL', f'stress-ng {tag}: exit {src}; ' +
                       ' | '.join(l for l in out.splitlines() if re.search(r'fail|error', l, re.I))[:300])

    def monkey(self, start, p=None):
        """start: monkey started detached on the plan's `packages` only (no system keys, no 'any' events; crashes,
        timeouts and security exceptions ignored, native crashes watched), `throttle_ms` (default 300) ms between
        events, enough events for `minutes` plus 1000. Not start: monkey stopped, its log saved as monkey.log,
        MONKEY_CRASH / MONKEY_ANR (warnings) with a count per app."""
        if not start:
            self.dev.sh('pkill -f com.android.commands.monkey', timeout=30)
            _, log = self.dev.sh(f'cat {DEV}/monkey.log', timeout=60)
            self.save('monkey.log', log)
            for kind, pat in (('MONKEY_CRASH', r'// CRASH: (\S+)'), ('MONKEY_ANR', r'NOT RESPONDING: (\S+)')):
                for name, n in collections.Counter(re.findall(pat, log)).items():
                    self.event(kind, f'{name} x{n}')
            return
        pk = ' '.join(f'-p {x}' for x in p['packages'])
        count = int(p['minutes'] * 60 * 1000 / p.get('throttle_ms', 300)) + 1000
        self.dev.sh(f'nohup monkey {pk} --throttle {p.get("throttle_ms", 300)} --pct-syskeys 0 --pct-anyevent 0 '
                    '--ignore-crashes --ignore-timeouts --ignore-security-exceptions --monitor-native-crashes '
                    f'-v {count} > {DEV}/monkey.log 2>&1 &', timeout=30)

    def music_state(self, pkg):
        """Playback state of package pkg's session in dumpsys media_session (e.g. PLAYING, PAUSED); 'NONE' without."""
        _, out = self.dev.sh('dumpsys media_session', timeout=30)
        m = re.search(r'package=' + re.escape(pkg) + r'.*?state=PlaybackState \{state=([A-Z_]+)|package='
                      + re.escape(pkg) + r'[\s\S]{0,800}?state=([A-Z_]+)\(', out)
        return (m.group(1) or m.group(2)) if m else 'NONE'

    def music_play(self, mu):
        """Plays mu['path'] in the music app's track page through a VIEW intent on its MediaStore entry (the way
        workspace tools/perf/t11206-music.sh drives the Kylin music app). Volume set with key events (the media_session
        volume command does not move the speaker index on T11206). mu: the soak plan's `music`: path (default
        /sdcard/Music/test.mp3), package (com.kylinos.music), local (a host copy pushed again when the track is
        missing), volume (3), mime (audio/mpeg), activity (.activities.TrackActivity). Returns whether playback was
        started: False with SKIP (once per run) when the track is not in the device's media library."""
        pkg = mu.get('package', 'com.kylinos.music')
        _, q = self.dev.sh('content query --uri content://media/external/audio/media --projection _id:_data',
                           timeout=30)
        m = re.search(r'_id=(\d+), _data=\S*/' + re.escape(os.path.basename(mu.get('path', '/sdcard/Music/test.mp3')))
                      + r'\b', q)
        if not m and mu.get('local') and os.path.exists(os.path.expandvars(os.path.expanduser(mu['local']))):
            # Random taps in the file manager may have deleted it: push the host copy again and look once more.
            self.dev.adb('push', os.path.expandvars(os.path.expanduser(mu['local'])), mu['path'], timeout=300)
            time.sleep(3)
            _, q = self.dev.sh('content query --uri content://media/external/audio/media --projection _id:_data',
                               timeout=30)
            m = re.search(r'_id=(\d+), _data=\S*/' + re.escape(os.path.basename(mu['path'])) + r'\b', q)
            self.event('INFO', f"music: track missing on the device, pushed {mu['local']} again"
                       + ('' if m else ' (still not in the media library)'))
        if not m:
            if not getattr(self, '_music_skip', False):
                self._music_skip = True
                self.event('SKIP',
                           f"music: {mu.get('path', '/sdcard/Music/test.mp3')} not in the device's media library")
            return False
        # 15 steps down to 0, then `volume` steps up
        keys = ' '.join(['KEYCODE_VOLUME_DOWN'] * 15 + ['KEYCODE_VOLUME_UP'] * int(mu.get('volume', 3)))
        self.dev.sh(f'input keyevent {keys}', timeout=60)
        self.dev.sh(f'am start -a android.intent.action.VIEW -d content://media/external/audio/media/{m.group(1)} '
                    f'-t {mu.get("mime", "audio/mpeg")} -n {pkg}/{mu.get("activity", ".activities.TrackActivity")}',
                    timeout=30)
        time.sleep(5)
        st = self.music_state(pkg)
        self.event('INFO', f'music: track {m.group(1)} started, session state {st}')
        return True

    def phase_soak(self, p):
        """Soak: everyday use under the hypervisor (device interrupts, timers, IPIs from app activity) with periodic
        targeted stress (IPIs, timers, memory, depending on stress_args).

        For `minutes`: monkey random input confined to the plan's `packages` (`monkey`, default on; `throttle_ms`,
        default 300); a stress-ng burst (stress_burst) of `stress_seconds` (default 120) with `stress_args` every
        `stress_every_min` (default 10) minutes, the first after 60 s (none without stress_args); the screen off for
        `screen_off_s` (default 90) s every `screen_cycle_min` (default 15) minutes; with `music` (music_play) a track
        played in the music app, checked every `check_every_s` (default 300) s and started again when not PLAYING.
        Snapshots every health interval.
        Records: each burst's INFO / SKIP / STRESS_FAIL / DEVICE_LOST, MONKEY_CRASH and MONKEY_ANR (warnings) at the
        end, and what the snapshots find. Restores (also on an abort): monkey stopped, playback paused (a forced
        suspend in a later phase that cuts playback pops), the screen on."""
        mu = p.get('music')
        if mu:
            self.music_play(mu)
            next_music = time.time() + mu.get('check_every_s', 300)
        if p.get('monkey', True):
            self.monkey(True, p)
        end = time.time() + p['minutes'] * 60
        next_stress = time.time() + 60
        next_screen = time.time() + p.get('screen_cycle_min', 15) * 60
        n = 0
        try:
            while time.time() < end:
                if p.get('stress_args') and time.time() >= next_stress:
                    n += 1
                    self.stress_burst(p.get('stress_seconds', 120), p['stress_args'], f'soak{n:03d}')
                    next_stress = time.time() + p.get('stress_every_min', 10) * 60
                if mu and time.time() >= next_music:
                    if self.music_state(mu.get('package', 'com.kylinos.music')) != 'PLAYING':
                        self.music_play(mu)
                    next_music = time.time() + mu.get('check_every_s', 300)
                if time.time() >= next_screen:
                    self.screen(False)
                    self.wait(p.get('screen_off_s', 90), 'screen-off')
                    self.screen(True)
                    next_screen = time.time() + p.get('screen_cycle_min', 15) * 60
                self.wait(min(30, max(1, end - time.time())))
        finally:
            if p.get('monkey', True):
                self.monkey(False)
            if mu:  # do not carry playback into the suspend phases (a forced suspend that cuts playback pops)
                self.dev.sh('cmd media_session dispatch pause', timeout=30)
            self.screen(True)

    # ---- Phase suspend: forced system suspends -----------------------------------------------------------------------
    def suspend_once(self, sleep_s, i):
        """One forced suspend; returns (snapshot after it, seconds until adb came back, wake source); the seconds are
        None when adb never dropped (suspend refused, or adb survived it) or never came back.
        Procedure: snapshot pre-suspend<i>, screen off, wait for audio playback to stop (wait_audio_idle), `rtcwake -m
        mem -s <sleep_s>` started detached as root, up to 30 s for adb to drop (if it does not, sleep_s + 5 s), up to
        sleep_s + 180 s for adb to come back, snapshot suspend<i> (it checks the inventory and the suspend counters).
        Records: RESUME_TIMEOUT (error) when adb is not back; SUSPEND_NOT_ENTERED (warning) when neither the success
        nor the fail count rose; INFO 'suspend <i>: success +<n> fail +<n>, adb back after <s> s | adb never dropped,
        woke by <device>'. Wake source 'reboot' when the boot id changed."""
        h0 = self.snapshot(f'pre-suspend{i}')
        s0, f0 = num(h0, 'suspend_success'), num(h0, 'suspend_fail')
        self.screen(False)
        self.wait_audio_idle()
        t0 = time.time()
        # Detached: USB and adb drop while the device sleeps, so the command must not depend on the adb session.
        self.dev.su(f'nohup rtcwake -m mem -s {sleep_s} > {DEV}/rtcwake.log 2>&1 &', timeout=20)
        dropped = self.dev.wait_drop(30)
        if not dropped:  # still on adb: the suspend was refused or adb survived it
            time.sleep(sleep_s + 5)
        if not self.dev.wait_alive(sleep_s + 180):
            self.event('RESUME_TIMEOUT', f'suspend {i}: no adb {sleep_s + 180} s after a {sleep_s} s alarm')
            return self.snapshot(f'suspend{i}-late'), None, 'unknown'
        back = time.time() - t0 if dropped else None
        h = self.snapshot(f'suspend{i}')
        ds, df = num(h, 'suspend_success') - s0, num(h, 'suspend_fail') - f0
        if h.get('boot_id') != h0.get('boot_id'):
            return h, back, 'reboot'
        if ds < 1 and df < 1:
            self.event('SUSPEND_NOT_ENTERED', f'suspend {i}: success +{ds}, fail +{df} (aborted before suspend?)')
        why = wake_source(h, h0)
        came = f'adb back after {back:.0f} s' if back is not None else 'adb never dropped'
        self.event('INFO', f'suspend {i}: success +{ds} fail +{df}, {came}, woke by {why}')
        return h, back, why

    def wait_audio_idle(self, least=5, most=15):
        """Screen off plays the lock sound (about 3 s on T11206). A forced suspend (rtcwake ignores wake locks) that
        cuts a running playback leaves the speaker amplifier powered across suspend and resume, which pops; natural
        suspend never does this because the audio server holds a wake lock while it plays. So wait for every PCM
        playback stream to stop."""
        time.sleep(least)
        end = time.time() + most - least
        while time.time() < end:
            # No /proc/asound pcm status on this kernel: use the audio server's output threads (all in standby) and,
            # where the board driver logs it, the last speaker amplifier switch (must be off).
            rc, out = self.dev.su('n=$(dumpsys media.audio_flinger 2>/dev/null | grep -c "^  Standby: no"); '
                                  'a=$(dmesg | grep -E "hook_spk_|spk.*on: [01]" | tail -1); '
                                  'case "$a" in *"on: 1"*) a=1;; *) a=0;; esac; echo "AUDIO $n $a"', timeout=30)
            m = re.search(r'AUDIO (\d+) (\d)', out or '')
            if m and m.group(1) == '0' and m.group(2) == '0':
                return True
            time.sleep(1)
        self.event('INFO', 'audio playback still running before suspend')
        return False

    def phase_suspend(self, p):
        """Suspend: system suspend and resume under the hypervisor (every CPU powered down and up through the power
        calls, the wake interrupt), by forced suspends with an alarm wake-up.

        `cycles` (default 50) times suspend_once with `sleep_s` (default 20) s of alarm, then the screen on and
        `awake_s` (default 10) s awake. Records: per cycle those of suspend_once; the snapshots add SUSPEND_FAIL
        (error), SUSPEND_ABORTED (warning) and DEVICE_MISSING (error, a device not back after the resume). Leaves the
        screen on."""
        for i in range(1, p.get('cycles', 50) + 1):
            self.suspend_once(p.get('sleep_s', 20), i)
            self.screen(True)
            time.sleep(p.get('awake_s', 10))
        self.screen(True)

    # ---- Phase memtest: stressapptest --------------------------------------------------------------------------------
    def phase_memtest(self, p):
        """Memtest: memory contents under heavy load (stage-2 mappings, cache maintenance under the hypervisor; a
        fault shows as a data mismatch).

        stressapptest started detached as root for `minutes` (default 30) on `mem_pct` (default 25) % of the
        snapshot's MemAvailable (at least 64 MiB), with -m 4 -i 2 -C 2 -W (4 copy threads, 2 invert threads, 2 CPU
        stress threads, the more CPU-intensive copy); waits minutes + 60 s (snapshots go on), then saves its log as
        stressapptest.log. Records: INFO with size and length, then INFO 'stressapptest PASS', or MEMTEST_FAIL (error)
        when the last 'Status:' line is not PASS or there is none, with the log's error lines. SKIP when stressapptest
        is not built. Nothing to restore (the tool ends by itself)."""
        if not self.tool('stressapptest'):
            return
        h = self.snapshot('pre-memtest')
        mb = max(64, int(num(h, 'mem_avail_kb') / 1024 * p.get('mem_pct', 25) / 100))
        secs = int(p.get('minutes', 30) * 60)
        self.dev.su(f'cd {DEV}/tmp && nohup {DEV}/stressapptest -s {secs} -M {mb} -m 4 -i 2 -C 2 -W '
                    f'> {DEV}/sat.log 2>&1 &', timeout=30)
        self.event('INFO', f'stressapptest {secs} s, {mb} MiB')
        self.wait(secs + 60, 'memtest')
        _, log = self.dev.su(f'cat {DEV}/sat.log', timeout=60)
        self.save('stressapptest.log', log)
        st = re.findall(r'Status: (\w+)', log)
        if st[-1:] != ['PASS']:
            self.event('MEMTEST_FAIL', f'stressapptest status {st[-1:] or "missing"}: ' +
                       ' | '.join(l for l in log.splitlines() if 'error' in l.lower())[:300])
        else:
            self.event('INFO', 'stressapptest PASS')

    # ---- Phase standby: long forced suspends -------------------------------------------------------------------------
    def phase_standby(self, p):
        """Forced suspends of suspend_s each until `hours` have passed (a cycle starts while time is left), then the
        real sleep lengths (seconds until adb came back) and wake sources: STANDBY_SHORT when they were cut short.

        Standby: resume after long deep sleep under the hypervisor. Parameters: `hours` (required), `suspend_s`
        (default 1500), `awake_s` (default 30, screen on). Records: per cycle those of suspend_once (snapshot tags
        suspendstandby<n>); at the end INFO with the cycle count, the median and longest time until adb came back and
        the wake sources (standby_summary), and STANDBY_SHORT (warning) when the median is below half of suspend_s
        (with USB power connected the charger driver arms a wake alarm a few seconds ahead). Leaves the screen on."""
        end, s, cycles = time.time() + p['hours'] * 3600, p.get('suspend_s', 1500), []
        while time.time() < end:
            _, back, why = self.suspend_once(s, f'standby{len(cycles) + 1}')
            cycles.append((back, why))
            self.screen(True)
            time.sleep(p.get('awake_s', 30))
        self.screen(True)
        info, short = standby_summary(cycles, s)
        self.event('INFO', info)
        if short:
            self.event('STANDBY_SHORT', short)

    # ---- Phase latency: cyclictest -----------------------------------------------------------------------------------
    def phase_latency(self, p):
        """Latency: timer injection and the wake-up path under the hypervisor, measured as wake-up latency.

        cyclictest -m -p 90 -i 1000 -t -a -q (memory locked, priority 90, a 1 ms interval, one thread per CPU, pinned)
        for `seconds` (default 60), first on the idle device, then with stress-ng `stress_args` (default '--cpu 4
        --switch 2') running alongside for seconds + 10 s (only when stress-ng is built). Output saved as
        cyclictest-idle.txt and cyclictest-stress.txt. Records: INFO 'cyclictest <idle|stress>: max latency per thread
        [...] us'; there is no threshold, so nothing here is a failure by itself. SKIP when cyclictest is not built.
        Nothing to restore."""
        if not self.tool('cyclictest'):
            return
        for load in ('idle', 'stress'):
            bg = ''
            if load == 'stress' and 'stress-ng' in self.tools:
                bg = (f'{DEV}/stress-ng {p.get("stress_args", "--cpu 4 --switch 2")} --timeout '
                      f'{p.get("seconds", 60) + 10}s --temp-path {DEV}/tmp > /dev/null 2>&1 & ')
            rc, out = self.dev.su(f'cd {DEV}/tmp && {bg}{DEV}/cyclictest -m -p 90 -i 1000 -t -a -q '
                                  f'-D {p.get("seconds", 60)}s', timeout=p.get('seconds', 60) + 120)
            self.save(f'cyclictest-{load}.txt', out)
            mx = [int(x) for x in re.findall(r'Max:\s+(\d+)', out)]
            self.event('INFO', f'cyclictest {load}: max latency per thread {mx} us')

    # ---- Phase el2_stress: device/el2stress.sh, one exit class per step ----------------------------------------------
    def phase_el2_stress(self, p):
        """Steps of device/el2stress.sh, one exit class each, measured: EL2 share per CPU from the EL2 parallel log and
        the hypervisor's hot-path counters (XHYPER_*_INPLACE, SGI, idle-wake lines) from the serial console.

        EL2 stress: each step drives one kind of exit to the hypervisor (the step list and what each one does are in
        device/el2stress.sh's header: ipi, timer, idle, traffic, affinity, hotplug, pmu, radio, copy, gpu, tee, all).
        Parameters: `steps` (default ipi, timer, idle, traffic, affinity, hotplug, pmu, all; a repeated step runs as
        <step>-r2, ... with its own phase, events, files and EL2 summary row), `seconds` per step (default 180).
        glmark2 is installed first (ensure_apks) when the plan's `steps` name gpu or all (not for the default list).
        Per step (<name>: the step, or <step>-r<n>): phase <phase>/<name>, PHASE_START and a kernel-log marker; for
        pmu with the EL2 log the monitor is stopped first (it owns the PMU while it counts) and brought up again
        after; traffic, affinity and all also get USB bulk traffic from the host (not through a remote adb server);
        `el2stress.sh <step> <seconds> <gateway>` (the Wi-Fi default gateway, for the ping flood) started detached as
        root; the step's seconds waited (checkpoints go on), then up to 300 s more for its 'EL2STRESS_DONE <step>'
        line; any not-responding dialog dismissed; output saved as el2stress-<name>.txt, the step's XHYPER_ serial
        lines as el2stress-<name>-serial.txt.
        Records: STRESS_FAIL (error) when EL2STRESS_DONE did not come (plus a snapshot) and for every 'EL2STRESS ...'
        line with an rc other than 0; INFO with the step's part lines; INFO with the hypervisor counters at the end
        (the last *_INPLACE, *_COUNTS, *IDLE_WAKE and *REFUSED line of each name); EL2_LOAD with the EL2 share
        mean/max per CPU over the step; PHASE_END. The steps restore what they change (el2stress.sh); after the last
        step the outer phase gets a new marker."""
        # the Wi-Fi default gateway: the target of the traffic step's ping flood ('' without Wi-Fi)
        _, rt = self.dev.sh('ip route show table all', timeout=30)
        m = re.search(r'default via (\S+) dev wlan0', rt)
        gw = m.group(1) if m else ''
        base, secs = self.phase, int(p.get('seconds', 180))
        if any(s_ in ('gpu', 'all') for s_ in p.get('steps', [])):
            self.ensure_apks(['glmark2'])
        steps = p.get('steps', ['ipi', 'timer', 'idle', 'traffic', 'affinity', 'hotplug', 'pmu', 'all'])
        # a repeated step runs as <step>-r2, ...: its own phase, events, files and EL2 summary row
        for step, name in zip(steps, step_names(steps)):
            self.phase = f'{base}/{name}'
            self.event('PHASE_START', f'{name} {secs} s')
            self.mark_phase()
            t0 = time.time()
            # capture size at the step's start: the step's hypervisor lines are read from here
            soff = os.path.getsize(self.serial) if self.serial and os.path.exists(self.serial) else 0
            if step == 'pmu' and self.el2:  # the monitor owns the PMU while it counts
                self.dev.su('echo 0 > /proc/xcore_stats', timeout=30)
            usb = None
            # USB bulk traffic: device-side USB controller interrupts. Not for a device behind a remote adb server
            # (ADB_SERVER_SOCKET): the stream would cross the network tunnel too.
            if step in ('traffic', 'affinity', 'all') and not os.environ.get('ADB_SERVER_SOCKET'):
                usb = subprocess.Popen(['adb', '-s', self.dev.serial, 'exec-out',
                                        f'timeout {secs} cat /dev/zero'], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            self.dev.su(f'nohup sh {DEV}/el2stress.sh {step} {secs} {gw} > {DEV}/tmp/el2stress-{step}.log 2>&1 &',
                        timeout=30)
            self.wait(secs, name)
            # the script ends after its parts finish: up to 300 s past the step's seconds for its last line
            out, end = '', time.time() + 300
            while time.time() < end:
                _, out = self.dev.su(f'cat {DEV}/tmp/el2stress-{step}.log', timeout=30)
                if f'EL2STRESS_DONE {step}' in out:
                    break
                self.checkpoint(name)
                time.sleep(10)
            if usb:
                usb.kill()
            self.dismiss_anr_dialogs(name)
            self.save(f'el2stress-{name}.txt', out)
            if f'EL2STRESS_DONE {step}' not in out:
                self.event('STRESS_FAIL', f'{name}: no EL2STRESS_DONE within {secs + 300} s')
                self.snapshot(f'{name}-late')
            for l in out.splitlines():
                if l.startswith('EL2STRESS ') and ' rc=' in l and not re.search(r' rc=0\b', l):
                    self.event('STRESS_FAIL', f'{name}: {l[:200]}')
            parts = ' | '.join(l[10 + len(step):].strip() for l in out.splitlines()
                               if l.startswith(f'EL2STRESS {step} '))
            self.event('INFO', f'{name}: {parts[:400]}')
            if step == 'pmu' and self.el2:
                self.el2_up()
            if self.serial and os.path.exists(self.serial):
                with open(self.serial, 'rb') as f:
                    f.seek(soff)
                    hv = [l for l in f.read().splitlines() if b'XHYPER_' in l]
                with open(os.path.join(self.out, f'el2stress-{name}-serial.txt'), 'wb') as o:
                    o.write(b'\n'.join(hv) + b'\n')
                last = {}  # XHYPER_<name> -> its last line in this step
                for l in hv:
                    mm = re.search(rb'(XHYPER_[A-Z0-9_]+)', l)
                    if mm:
                        last[mm.group(1).decode()] = l.decode('utf-8', 'replace')
                keep = [v for k, v in last.items() if k.endswith(('_INPLACE', '_COUNTS', 'IDLE_WAKE', 'REFUSED'))]
                # x[13:]: without the capture's 'HH:MM:SS.mmm ' host stamp
                self.event('INFO', f'{name}: hypervisor counters at end: '
                           + ' || '.join(x[13:200] for x in keep)[:900])
            if self.el2:
                self.el2_pull_scan()
                w = el2_report.window(self.out, t0 + 5, time.time())  # from 5 s after the step's start
                if w:
                    self.event('EL2_LOAD', f'{name}: EL2 share mean/max per CPU ' +
                               ' '.join(f'{c}:{m_:.0%}/{x:.0%}' for c, (m_, x, _) in w.items()))
            self.event('PHASE_END', name)
        self.phase = base
        self.mark_phase()  # back in the outer phase: later kernel lines are not the last step's

    # ---- Phase monitor_soak: the EL2 monitor under load --------------------------------------------------------------
    def phase_monitor_soak(self, p):
        """Monitor soak (optional): the EL2 cycle monitor itself (a driver and a PMU user in the system, extra
        hypercalls around deep idle) under load.

        The monitor is enabled with the el2-monitor.sh script of the xhyper-el2-monitor-android skill (`skill`: its
        path; `artifacts`: the monitor's module directory, environment variables expanded), its output saved as
        monitor-enable.txt; MONITOR_FAIL (error) and the phase ends when it does not print EL2-MONITOR-ENABLED. Then
        for `minutes` (default 30), in rounds: a stress-ng burst of `stress_seconds` (default 60) with `stress_args`
        (when given), /proc/xcore_stats read, MONITOR_FAIL when any CPU's EL2 cycle count went down since the last
        round, 60 s wait. Restores: the monitor stopped with the same script (not after an abort)."""
        script = os.path.expanduser(p.get('skill', '~/.codex/skills/xhyper-el2-monitor-android/scripts/el2-monitor.sh'))
        r = subprocess.run(['bash', script, 'enable', '-s', self.dev.serial, '--out', os.path.join(self.out, 'monitor'),
                            os.path.expanduser(os.path.expandvars(p['artifacts']))],
                           capture_output=True, text=True, errors='replace')
        self.save('monitor-enable.txt', r.stdout + r.stderr)
        if 'EL2-MONITOR-ENABLED' not in r.stdout:
            self.event('MONITOR_FAIL', 'monitor enable failed: ' + (r.stdout + r.stderr).strip().splitlines()[-1][:200])
            return
        end = time.time() + p.get('minutes', 30) * 60
        n, prev = 0, None
        while time.time() < end:
            n += 1
            if p.get('stress_args'):
                self.stress_burst(p.get('stress_seconds', 60), p['stress_args'], f'monitor{n:03d}')
            _, st = self.dev.sh('cat /proc/xcore_stats', timeout=30)
            cur = {int(a): int(b) for a, b, _ in re.findall(r'^(\d+) (\d+) (\d+)', st, re.M)}
            if prev and any(cur.get(c, 0) < v for c, v in prev.items()):
                self.event('MONITOR_FAIL', f'EL2 cycle count went down: {prev} -> {cur}')
            prev = cur
            self.wait(60, 'monitor')
        subprocess.run(['bash', script, 'stop', '-s', self.dev.serial], capture_output=True)

    # ---- Phase battery_rundown: video playback from full charge to the automatic shutdown ----------------------------
    def ensure_media(self, key):
        """(device path, sha256) of media file `key` of third_party/media.lock in /sdcard/Movies/: the device copy when
        its sha256 matches the lock, else pushed from STAB_MEDIA_DIR after checking its size and sha256 there."""
        lock = lock_entries('media.lock', 4)
        if key not in lock:
            raise Abort(f'media {key}: not in third_party/media.lock')
        _, fname, size, sha = lock[key][:4]
        path = f'/sdcard/Movies/{fname}'
        have = lambda: (self.dev.sh(f'sha256sum {path} 2>/dev/null', timeout=300)[1] or '').split()[:1] == [sha]
        if have():
            self.event('INFO', f'media {key}: {path} on the device matches the lock')
            return path, sha
        d = os.environ.get('STAB_MEDIA_DIR', '')
        host = os.path.join(d, fname)
        if not d or not os.path.exists(host):
            raise Abort(f'media {key}: {path} missing or different on the device, and {fname} not found in '
                        f'STAB_MEDIA_DIR ({d or "unset"})')
        if os.path.getsize(host) != int(size) or sha256_file(host) != sha:
            raise Abort(f'media {key}: {host} does not match the size and sha256 in third_party/media.lock')
        self.dev.sh('mkdir -p /sdcard/Movies', timeout=30)
        rc, out = self.dev.adb('push', host, path, timeout=1800)
        if not have():
            raise Abort(f'media {key}: push to {path} failed (adb rc {rc}): {out.strip()[-160:]}')
        self.event('INFO', f'media {key}: pushed {fname} to {path}')
        return path, sha

    def rundown_identity(self, pkg, ver, clip, apk_file):
        """Appends the rundown's conditions to identity.txt: kernel command line and console log level (the vendor
        kernel on T11206 prints ~270 lines a second during playback, which costs power, so the runs compared must have
        the same), the battery's uevent, the player's version and the clip; Abort when the player is not version
        `ver`."""
        _, out = self.dev.sh(f'cat /proc/cmdline; echo "printk $(cat /proc/sys/kernel/printk)"; {BATTERY_DIR}; '
                             'echo "battery $(tr "\\n" " " < $B/uevent)"; '
                             f'dumpsys package {pkg} | grep -m2 -oE "versionName=[^ ]+|versionCode=[0-9]+"', timeout=60)
        with open(os.path.join(self.out, 'identity.txt'), 'a') as f:
            f.write(f'--- battery_rundown\n{out.strip()}\nclip {clip[0]} sha256 {clip[1]}\n')
        m = re.search(r'versionName=(\S+)', out or '')
        if not m:
            raise Abort(f'battery_rundown: {pkg} not installed (put {apk_file} in STAB_APK_DIR)')
        if m.group(1) != ver:
            raise Abort(f'battery_rundown: {pkg} {m.group(1)} installed, the plan wants {ver} (third_party/apks.lock)')

    def rundown_read_settings(self):
        """Every setting the rundown changes, as it is now (restored at the end and on an abort before the unplug)."""
        orig = {}
        for k in RUNDOWN_SETTINGS:
            rc, out = self.dev.sh(f'settings get {k}', timeout=30)
            if rc == 0:
                orig[k] = out.strip()
        orig['volume'] = parse_volume(self.dev.sh('cmd media_session volume --stream 3 --get', timeout=30)[1])
        _, out = self.dev.sh('cmd display get-match-content-frame-rate-pref', timeout=30)
        m = re.findall(r'\b([0-2])\b', out or '')
        orig['match_content'] = int(m[-1]) if m else 1  # 1: Android's default (seamless switches only)
        return orig

    def rundown_restore(self, orig):
        """Puts back the settings read before the run (rundown_restore_cmds) and reads them back; when Wi-Fi was on,
        first waits up to 30 s for it (the next snapshot compares the device inventory with one taken with Wi-Fi on).
        RESTORE_FAIL with the command to run by hand when the device does not answer or a setting stays different;
        returns True when all came back."""
        self.dev.sh('; '.join(rundown_restore_cmds(orig)), timeout=120)
        time.sleep(3)
        if radio_on('global wifi_on', orig.get('global wifi_on')):
            for _ in range(10):
                if 'is enabled' in (self.dev.sh('cmd wifi status | head -1', timeout=30)[1] or ''):
                    break
                time.sleep(3)
        now = self.rundown_read_settings()
        if not any(k in now for k in RUNDOWN_SETTINGS):
            self.event('RESTORE_FAIL', 'settings not restored, the device does not answer; restore them by hand: '
                       + restore_shell(self.dev.serial, orig))
            return False
        diff = [f'{k} {orig[k]} -> {now.get(k)}' for k in RUNDOWN_SETTINGS + ('volume',)
                if orig.get(k) not in (None, '') and not same_setting(k, orig[k], now.get(k))]
        if diff:
            self.event('RESTORE_FAIL', f'settings restored, still different: {", ".join(diff)}; by hand: '
                       + restore_shell(self.dev.serial, orig))
            return False
        self.event('INFO', 'settings restored')
        return True

    def rundown_stop(self, pkg):
        """Sampler (its pid file removed: it ends before its next sample) and player stopped."""
        self.dev.su(f'rm -f {RUNDOWN}/rundown.pid', timeout=30)
        self.dev.sh(f'am force-stop {pkg}', timeout=30)

    def rundown_up(self):
        """Whether the device answers on adb with its boot completed (its settings can be written)."""
        return self.dev.alive() and (self.dev.sh('getprop sys.boot_completed', timeout=10)[1] or '').strip() == '1'

    def rundown_abandon(self, stage, orig, pkg, path):
        """The run stopped after the settings were applied and before they were put back (an abort, a timeout, an
        exception, Ctrl-C or SIGTERM; stage: before the unplug, away, back): on a device that answers with its boot
        completed, sampler and player stopped and settings restored; otherwise RESTORE_FAIL saying what stays on the
        device and how to restore the settings and get the samples."""
        if self.rundown_up():
            self.rundown_stop(pkg)
            self.rundown_restore(orig)
            return
        when = {'before': 'before the USB unplug', 'away': 'while the device was away'}.get(stage, 'after its return')
        self.event('RESTORE_FAIL', f'the run stopped {when} and the device does not answer (USB unplugged, or not '
                   'booted): settings stay as set' + (', the sampler and the player run until the automatic shutdown; '
                                                      f'samples in {path} on the device (copy them to '
                                                      'rundown-device.csv in the run directory, then rundown_report.py '
                                                      '<run dir>)' if path else '')
                   + f'; once it is back, restore the settings with: {restore_shell(self.dev.serial, orig)}')

    def rundown_wait_full(self, p):
        """Waits on USB power until the battery reads 100 % and Full (charge_timeout_h, else Abort)."""
        end, shown = time.time() + p['charge_timeout_h'] * 3600, None
        while True:
            _, out = self.dev.sh(f'{BATTERY_DIR}; echo "BAT $(cat $B/capacity) $(cat $B/status)"', timeout=30)
            m = re.search(r'BAT (\d+) (.+)', out or '')
            cap, status = (int(m.group(1)), m.group(2).strip()) if m else (None, '?')
            if cap is not None and cap >= 100 and status == 'Full':
                self.event('INFO', 'battery full (100 %, Full)')
                return
            if time.time() >= end:
                raise Abort(f"battery_rundown: battery not full after {p['charge_timeout_h']} h on USB "
                            f"({cap} %, {status})")
            if shown != (cap // 10 if cap is not None else None, status):  # an INFO per 10 % step or status change
                shown = (cap // 10 if cap is not None else None, status)
                self.event('INFO', f'charging: {cap} % {status}; waiting for 100 % and Full')
            self.wait(60, 'charging')

    def rundown_start_sampler(self, p, pkg):
        """device/rundown.sh started detached as root (it survives the USB unplug) on a new file; (path, columns)."""
        path = f"{RUNDOWN}/rundown-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        _, out = self.dev.su(f'mkdir -p {RUNDOWN} && rm -f {RUNDOWN}/rundown.pid; '
                             f'setsid nohup sh {DEV}/rundown.sh {path} '
                             f"{int(p['sample_s'])} {pkg} > /dev/null 2>&1 < /dev/null & sleep 3; head -1 {path}",
                             timeout=60)
        head = next((l.strip() for l in (out or '').splitlines() if l.startswith('epoch,')), '')
        if not head:
            raise Abort('battery_rundown: the sampler did not start: ' + ' '.join((out or '').split())[-200:])
        self.event('INFO', f"rundown sampler started: {path}, a sample every {p['sample_s']} s")
        return path, head.split(',')

    def rundown_start_player(self, p, pkg, clip):
        """Grants the player its permissions and plays the clip from its start in the video player.
        TODO(vlc-prefs): the one-time preparation (VLC_PREP: player introduction closed, repeat one) is done by hand on
        each install; automate it by writing VLC's shared preferences (/data/data/<pkg>/shared_prefs/) once the keys
        that hold the two are read from a prepared device."""
        for perm in ('READ_MEDIA_VIDEO', 'READ_MEDIA_AUDIO', 'POST_NOTIFICATIONS'):  # failures do not matter
            self.dev.sh(f'pm grant {pkg} android.permission.{perm}', timeout=30)
        self.dev.sh(f'appops set {pkg} MANAGE_EXTERNAL_STORAGE allow', timeout=30)
        self.screen(True)
        _, out = self.dev.sh(f'am force-stop {pkg}; sleep 1; '
                             f'am start -W -a android.intent.action.VIEW -d file://{clip} '
                             f"-t video/mp4 --ez from_start true -n {pkg}/{p['activity']}", timeout=90)
        self.save('rundown-player-start.txt', out or '')
        time.sleep(15)

    def rundown_state(self, pkg):
        """What the started rundown looks like (checked by rundown_checks before the unplug)."""
        st = {'playback': self.music_state(pkg)}
        st['focus'] = self.dev.sh('dumpsys window | grep -m1 mCurrentFocus', timeout=60)[1] or ''
        st['pid'] = ((self.dev.sh(f'pidof {pkg}', timeout=30)[1] or '').split() or [''])[0]
        st['hw'] = hw_decoder(self.dev.sh('dumpsys media.resource_manager', timeout=60)[1], st['pid'])
        st['backlight'] = (self.dev.sh('cat "$(ls -d /sys/class/backlight/* | head -1)/actual_brightness"',
                                       timeout=30)[1] or '').strip()
        st['vsync'] = []
        for i in range(3):  # three reads over about 10 s
            if i:
                time.sleep(5)
            o = self.dev.sh("dumpsys SurfaceFlinger | grep -m1 -oE 'vsyncRate=[0-9.]+'", timeout=60)[1] or ''
            st['vsync'].append(o.strip().partition('=')[2])
        for k, cmd in (('airplane', 'settings get global airplane_mode_on'), ('wifi', 'cmd wifi status | head -1'),
                       ('bluetooth', 'settings get global bluetooth_on')):
            st[k] = (self.dev.sh(cmd, timeout=30)[1] or '').strip()
        st['volume'] = parse_volume(self.dev.sh('cmd media_session volume --stream 3 --get', timeout=30)[1])
        st['sampler'] = 'SAMPLER_UP' in (self.dev.su(f'kill -0 "$(cat {RUNDOWN}/rundown.pid)" 2>/dev/null && '
                                                     'echo SAMPLER_UP', timeout=30)[1] or '')
        return st

    def rundown_wait_unplug(self, p):
        """Waits for the USB unplug (unplug_timeout_min, else Abort): adb gone, and still gone 5 s later (a moment
        without an answer, e.g. an adb server restarted by another tool, is not the unplug). Returns the boot id the
        run is on."""
        boot = (self.last or {}).get('boot_id')
        end = time.time() + p['unplug_timeout_min'] * 60
        while True:
            if not self.dev.wait_drop(max(0, end - time.time())):
                raise Abort(f"battery_rundown: USB still connected {p['unplug_timeout_min']} min after the request")
            time.sleep(5)
            if not self.dev.alive():
                return boot
            self.event('INFO', 'adb did not answer for a moment but the device is back: not the unplug, still waiting')

    def rundown_follow(self, live, off):
        """Feeds the serial capture's growth since byte `off` to live (RundownLive) and scans it for fatal markers (no
        SERIAL_STALE: the line goes quiet after the shutdown); returns the new offset. A shorter file is a new capture
        (restarted): both offsets start over."""
        try:
            end = os.path.getsize(self.serial)
        except OSError:
            return off
        off = 0 if end < off else off  # a new capture file
        if end < self.serial_scan_off:
            self.serial_scan_off = 0
        was, msgs = live.off, []
        with open(self.serial, 'rb') as f:
            f.seek(off)
            while off < end:
                b = f.read(min(8 << 20, end - off))
                if not b:
                    break
                msgs += live.feed(b, time.time())
                off += len(b)
        msgs += live.feed(b'', time.time())  # the quiet check, also without new bytes
        for m in msgs:
            self.event('INFO', m)
        self.serial_marks = (self.serial_marks + [(time.time(), end)])[-50:]
        if live.off and not was:
            self.serial_extract('rundown-end', before_s=900)
        if end > self.serial_scan_off:
            self.serial_scan()
        return off

    def rundown_away(self, p, head, soff):
        """USB unplugged: no health snapshots, no lost-device handling and no SERIAL_STALE while the device is away on
        purpose (self.away, cleared on its return). With a serial capture the sampler's lines go to rundown-live.csv;
        a failure of this live view ends only the view. Returns once the device is back on adb with its boot completed
        (return_timeout_h after the unplug, else Abort); on an Abort or Ctrl-C the report is written from the live
        view first."""
        t0, told, self.away = time.time(), False, True
        end = t0 + p['return_timeout_h'] * 3600
        self.event('INFO', f"USB unplugged: device away; after the automatic shutdown plug USB back in and power the "
                   f"device on (waiting up to {p['return_timeout_h']} h)" + (
                       '; live samples from the serial console in rundown-live.csv' if self.serial else
                       '; no --serial-log: no live view'))
        live = RundownLive(os.path.join(self.out, 'rundown-live.csv'), head, p['sample_s']) if self.serial else None
        view = bool(live)

        # one update of the live view (rundown_follow); an exception ends only the view, never the run
        def follow():
            nonlocal soff, view
            try:
                soff = self.rundown_follow(live, soff)
            except Exception as e:  # a side channel: it never ends the run
                view = False
                self.event('INFO', f'rundown: live view stopped ({e!r}); waiting for the device goes on')
        try:
            while True:
                if view:
                    follow()
                if self.dev.alive():
                    if (self.dev.sh('getprop sys.boot_completed', timeout=10)[1] or '').strip() == '1':
                        break
                    if not told:
                        told = True
                        self.event('INFO', 'device on adb, boot not completed (charger mode?): power it on')
                if time.time() >= end:
                    raise Abort(f"battery_rundown: device not back {p['return_timeout_h']} h after the unplug")
                time.sleep(30)
        except BaseException:
            if live and live.n:  # what the serial console carried is the result so far
                rundown_report.report_dir(self.out)
                self.event('INFO', f'rundown-summary.md written from the live view ({live.n} samples); after pulling '
                           'the device file into rundown-device.csv, rundown_report.py <run dir> writes it again')
            raise
        if view:
            follow()
        self.away = False
        self.event('INFO', f'device back on adb and booted {rundown_report.hm(time.time() - t0)} after the unplug')

    def rundown_collect(self, path, pkg):
        """After the return: sampler and player stopped (both still run when USB came back before a shutdown), the
        device's sample file pulled into rundown-device.csv."""
        self.rundown_stop(pkg)
        rc, data = self.dev.su(f'cat {path}', timeout=120)
        if rc == 0 and (data or '').startswith('epoch,'):
            self.save('rundown-device.csv', data.replace('\r', ''))
        else:
            self.event('RUNDOWN_INVALID', f'device sample file {path} not read back (rc {rc}); the report uses '
                       'rundown-live.csv if there is one')

    def rundown_finish(self, boot):
        """After the return, with the samples pulled and the settings restored: a new boot is expected (no
        UNEXPECTED_REBOOT), the boot reasons are recorded, the report written. A new boot although the device's samples
        show the battery did not run down (none on battery: it restarted before the unplug; the last one above 5 %:
        it restarted or shut down on battery) is an unexpected reboot after all (UNEXPECTED_REBOOT with its boot
        reasons and pstore files)."""
        prev = self.last
        self.expect_reboot = True
        try:
            h = self.snapshot('rundown-back')
        finally:
            self.expect_reboot = False
        new = h.get('boot_id') != boot
        _, br = self.dev.sh('getprop sys.boot.reason.last; getprop persist.sys.boot.reason.history | head -3',
                            timeout=30)
        self.save('rundown-boot-reason.txt', br or '')
        self.event('INFO', ('new boot after the rundown, as expected' if new else
                            'same boot as before the unplug: USB came back before a shutdown')
                   + '; boot reason (last, history): ' + ' | '.join((br or '').split())[:200])
        res = rundown_report.report_dir(self.out)
        if 'duration_s' in res:
            self.event('INFO', f"rundown: {rundown_report.hm(res['duration_s'])} on battery, "
                       f"{rundown_report.f0(res['start_cap'], ' %')} to {rundown_report.f0(res['end_cap'], ' %')}"
                       + (f", {res['energy_wh']:.2f} Wh, average {res['avg_w']:.3f} W" if 'energy_wh' in res else '')
                       + ' (rundown-summary.md)')
        for k, v in res['flags'].items():
            first = (f", first {rundown_report.hm(v['first_s'])} into the run at {v['cap']:.0f} %"
                     if v['first_s'] is not None and v['cap'] is not None else '')
            self.event('RUNDOWN_INVALID', f"{k}: {v['count']} samples{first}: {rundown_report.FLAGS[k]}"
                       + (f" ({v['detail']})" if v['detail'] else ''))
        early = ('never_unplugged' in res['flags'] or 'not_run_down' in res['flags'])
        if new and early and prev and res['samples'] and res.get('source') == 'rundown-device.csv':
            self.event('INFO', 'the device restarted before its battery ran down ('
                       + ('never on battery' if 'never_unplugged' in res['flags'] else
                          f"last sample {rundown_report.f0(res.get('end_cap'), ' %')}") + '): an unexpected reboot')
            self.unexpected_reboot(prev, h)

    def phase_battery_rundown(self, p):
        """Video playback battery life from full charge to the automatic shutdown (README, battery_rundown): identity
        and settings recorded, battery charged to full, settings applied, sampler and player started and checked; then
        the operator unplugs USB; the device is away until it is powered on again; then sampler and player are
        stopped, the sample file pulled, settings restored (before the first snapshot of the new boot), report
        written. Whenever the run stops before that restore (an abort, a timeout, an exception, Ctrl-C or SIGTERM, in
        any stage), a device that answers gets the same stop and restore, and one that does not gets a RESTORE_FAIL
        with the command.

        Exercises the whole device's power use under the hypervisor over a long playback (interrupt injection, idle
        entry and exit, frequency changes on every cluster). Parameters (defaults in rundown_report.DEFAULTS): `apk`
        (vlc: key in third_party/apks.lock), `media` (bbb60: key in third_party/media.lock), `brightness` (128, raw
        backlight), `refresh_hz` (90), `volume` (3 of 15), `airplane` (true), `sample_s` (60), `charge_timeout_h` (4),
        `unplug_timeout_min` (30), `return_timeout_h` (24), `activity` (VLC's video player activity).
        Files: identity.txt (appended), rundown-settings.json (original settings and the restore commands),
        rundown-settings-apply.txt, rundown-player-start.txt, rundown-start-state.json, rundown-live.csv (with a
        serial capture), rundown-device.csv, rundown-boot-reason.txt, rundown-summary.md and rundown.json.
        Records: Abort when the player or clip is missing or wrong, the battery is not full in time, a start check
        fails (rundown_checks), USB is not unplugged or the device not back in time; INFO for the progress (charging,
        ready, unplug, live samples, back, result); RUNDOWN_INVALID (warning) per validity flag of the report and
        when the device's sample file is not read back;
        RESTORE_FAIL (warning); UNEXPECTED_REBOOT (error) when a new boot came although the samples show the battery
        did not run down. Restores: the settings, the sampler and the player stopped (see above)."""
        p = {**rundown_report.DEFAULTS, **p}
        apk = lock_entries('apks.lock', 6).get(p['apk'])
        if not apk:
            raise Abort(f"battery_rundown: apk {p['apk']} not in third_party/apks.lock")
        pkg, ver = apk[2], apk[3]
        clip = self.ensure_media(p['media'])
        self.ensure_apks([p['apk']])
        self.rundown_identity(pkg, ver, clip, apk[1])
        orig = self.rundown_read_settings()
        self.save('rundown-settings.json', json.dumps({'original': orig, 'restore': rundown_restore_cmds(orig),
                                                      'restore_shell': restore_shell(self.dev.serial, orig)},
                                                     indent=1) + '\n')
        self.event('INFO', 'rundown: settings before the run: ' + ', '.join(f'{k}={v}' for k, v in orig.items()))
        stage, path = 'charging', None  # nothing changed while charging; then before (the unplug), away, back, restored
        try:
            self.rundown_wait_full(p)
            stage = 'before'
            self.save('rundown-settings-apply.txt', self.dev.sh('; '.join(rundown_apply_cmds(p)), timeout=120)[1] or '')
            soff = os.path.getsize(self.serial) if self.serial and os.path.exists(self.serial) else 0
            path, head = self.rundown_start_sampler(p, pkg)
            self.rundown_start_player(p, pkg, clip[0])
            st = self.rundown_state(pkg)
            self.save('rundown-start-state.json', json.dumps(st, indent=1) + '\n')
            bad = rundown_checks(st, p, pkg)
            if bad:
                raise Abort('battery_rundown not started: ' + '; '.join(bad))
            self.event('INFO', f"rundown ready: {pkg} playing {clip[0]} (hardware decoder {st['hw'][1] or '?'}), "
                       f"backlight {st['backlight']}, refresh {'/'.join(st['vsync'])} Hz, volume {st['volume']}")
            msg = ("UNPLUG USB NOW: the rundown starts when USB is unplugged "
                   f"(waiting up to {p['unplug_timeout_min']} min)")
            print('\n' + '=' * 100 + f'\n    {msg}\n' + '=' * 100 + '\n', flush=True)
            self.event('INFO', msg)
            boot = self.rundown_wait_unplug(p)
            stage = 'away'
            self.rundown_away(p, head, soff)
            stage = 'back'
            self.rundown_collect(path, pkg)
            self.rundown_restore(orig)
            stage = 'restored'
            self.rundown_finish(boot)
        finally:
            if stage in ('before', 'away', 'back'):
                self.rundown_abandon(stage, orig, pkg, path)

    # ---- Phase tee_idle: Trusty IRQ-off reports with deep idle on and off --------------------------------------------
    def cpuidle_states(self):
        """{disable file of every CPU's idle state 1 and above (the deep ones; state 0 is WFI): its value}."""
        _, out = self.dev.su(f'for f in {CPUIDLE}; do [ -e "$f" ] && echo "$f=$(cat $f)"; done', timeout=60)
        st = {}
        for line in (out or '').splitlines():
            m = re.fullmatch(r'(/sys/\S+/cpuidle/state(\d+)/disable)=(\d+)', line.strip())
            if m and int(m.group(2)) >= 1:
                st[m.group(1)] = m.group(3)
        return st

    def cpuidle_write(self, values):
        """Writes {disable file: value}; returns the files that do not read back their value."""
        self.dev.su('; '.join(f'echo {v} > {f}' for f, v in values.items()), timeout=60)
        now = self.cpuidle_states()
        return [f for f, v in values.items() if now.get(f) != str(v)]

    def kmsg(self, text):
        """Writes `text` as a line into the device's kernel log (/dev/kmsg, as root)."""
        self.dev.su(f'echo {shlex.quote(text)} > /dev/kmsg', timeout=30)

    def tee_idle_block(self, b, out, klog, soff, thr):
        """Result of one tee_idle block (its load's output `out`): TEE ops and failures, and the Trusty IRQ-off
        reports between the block's markers in the device's copy of the kernel log and, with a capture, on the serial
        console; an INFO event, and TEE_IRQSOFF when any of them reached thr ms."""
        r = {'name': b['name'], 'deep_idle': b['deep'], 'seconds': b['seconds'], 'ops': None, 'failures': None,
             'trusty_irq': None, 'device': None, 'serial': None}
        m = re.search(r'ops=(\d+) failures=(\d+) trusty_irq=(-?\d+)', out or '')
        if m:
            r.update(ops=int(m.group(1)), failures=int(m.group(2)), trusty_irq=int(m.group(3)))
        _, k = self.dev.su(f'cat {klog}', timeout=120)
        rep = tee_block((k or '').splitlines(), self.phase)
        r['device'] = tee_stats(rep, thr) if rep is not None else None
        if self.serial and os.path.exists(self.serial):
            with open(self.serial, 'rb') as f:
                f.seek(soff)
                lines = f.read().decode('utf-8', 'replace').splitlines()
            rep = tee_block(lines, self.phase)
            r['serial'] = tee_stats(rep, thr) if rep is not None else None
        fmt = lambda s: (f"{s['ge_thr']} (>= 100 ms: {s['ge100']}; longest {s['longest']} ms; sites {s['sites']})"
                         if s else 'n/a (no block markers in this copy)')
        idle = 'on' if b['deep'] else 'off'
        self.event('INFO', f"{b['name']}: deep idle {idle}; TEE ops {r['ops']}, failures {r['failures']}, Trusty "
                   f"interrupts {r['trusty_irq']}; Trusty IRQ-off reports >= {thr:g} ms: device log {fmt(r['device'])}"
                   + (f"; serial {fmt(r['serial'])}" if self.serial else ''))
        if r['device'] is None and r['serial'] is None:
            self.event('SKIP', f"{b['name']}: the block's markers are in no copy of the kernel log "
                       '(device/klogwatch.sh not running, no serial capture): nothing counted')
        s = tee_best(r)
        if s and s['ge_thr']:
            self.event('TEE_IRQSOFF', f"{b['name']}: {s['ge_thr']} Trusty IRQ-off reports >= {thr:g} ms "
                       f"with deep idle {idle} (longest {s['longest']} ms, call sites {s['sites']})")
        return r

    def phase_tee_idle(self, p):
        """The TEE load of el2stress.sh's tee step without cyclictest, in blocks with the CPUs' deep idle states on or
        off (default: on, off, on again, 600 s each): a Trusty call that kept the Host's IRQs off for hundreds of ms
        showed up only while CPUs used deep idle (T11206, 2026-10-14: 6 / 0 / 1 reports >= 30 ms before the fix,
        0 / 0 / 0 after). Each block is bracketed by 'STAB TEE_IDLE <block> start|end' kernel-log markers; a detached
        `dmesg -w` follower on the device (klogwatch.sh) keeps the IRQ-off reports and markers, so no serial capture is
        needed; with one, its count is reported too, as a cross-check. The original deep idle settings are restored at
        the end and on any abort or Ctrl-C (RESTORE_FAIL when they do not read back); the load and the follower are
        stopped only while their pids still run them (stop_cmd). Results in tee-idle.json and a table in summary.md.

        Exercises the forwarding of TEE calls (SMCs) and the Trusty notification interrupt under the hypervisor, with
        CPUs entering and leaving deep idle. Parameters: `blocks` (default TEE_IDLE_BLOCKS; each with `name`,
        `deep_idle` (default true) and `seconds` (default 600)), `workers` (TEE workers, default 2), `threshold_ms`
        (default 30). Per block: phase <phase>/<block>, PHASE_START and a phase marker; every deep idle state (1 and
        above) of every CPU enabled or disabled; the block's start marker; `TEE_CYCLIC=0 TEE_WORKERS=<workers>
        el2stress.sh tee <seconds>` started detached; the block's seconds waited (checkpoints go on), then up to 300 s
        more for 'EL2STRESS_DONE tee'; the end marker, 3 s; output saved as tee-idle-<block>.txt; the reports counted
        (tee_idle_block); PHASE_END.
        Records: per block INFO with TEE ops, failures, Trusty interrupts and the report counts of each copy;
        TEE_IRQSOFF (warning) when a copy has reports at or above threshold_ms; INFO when some idle states kept another
        value; STRESS_FAIL (error) when EL2STRESS_DONE did not come (the load is then stopped) or a part's rc is not 0;
        SKIP when the device has no deep idle states or a block's markers are in no copy of the kernel log;
        RESTORE_FAIL (warning) or INFO for the restore. Files: tee-idle-cpuidle.txt (the original values),
        tee-idle-<block>.txt, tee-idle.json."""
        blocks = p.get('blocks', TEE_IDLE_BLOCKS)
        w, thr, base = int(p.get('workers', 2)), float(p.get('threshold_ms', 30)), self.phase
        orig = self.cpuidle_states()
        if not orig:
            self.event('SKIP', 'tee_idle: no cpuidle states 1 and above on this device')
            return
        self.save('tee-idle-cpuidle.txt', ''.join(f'{f}={v}\n' for f, v in orig.items()))
        klog = f'{DEV}/tmp/tee-idle-klog.txt'  # the follower's copy of the IRQ-off reports and markers (pid in .pid)
        # stops the follower (only while its pid still runs klogwatch or its awk) and removes its pid file
        watch = stop_cmd(f'$(cat {klog}.pid 2>/dev/null)', 'klogwatch|awk') + f'; rm -f {klog}.pid'
        results, pid = [], None
        try:
            # a follower left by an earlier run that was killed is stopped first
            self.dev.su(f'{watch}; rm -f {klog}; setsid nohup sh {DEV}/klogwatch.sh {klog} '
                        '"irqsoff_warn|STAB TEE_IDLE" > /dev/null 2>&1 < /dev/null &', timeout=30)
            for b in blocks:
                # the plan's block with its defaults: name, deep (deep idle on), seconds
                b = {'name': b['name'], 'deep': bool(b.get('deep_idle', True)), 'seconds': int(b.get('seconds', 600))}
                self.phase = f"{base}/{b['name']}"
                self.event('PHASE_START', f"{b['name']}: deep idle {'on' if b['deep'] else 'off'}, {b['seconds']} s, "
                           f'{w} TEE workers')
                self.mark_phase()
                miss = self.cpuidle_write({f: 0 if b['deep'] else 1 for f in orig})
                if miss:
                    self.event('INFO', f"{b['name']}: {len(miss)} of {len(orig)} deep idle states kept another value: "
                               + ' '.join(miss)[:200])
                soff = os.path.getsize(self.serial) if self.serial and os.path.exists(self.serial) else 0
                self.kmsg(f'STAB TEE_IDLE {self.phase} start')
                log = f"{DEV}/tmp/tee-idle-{b['name']}.log"
                _, out = self.dev.su(f"TEE_CYCLIC=0 TEE_WORKERS={w} nohup sh {DEV}/el2stress.sh tee {b['seconds']} "
                                     f'> {log} 2>&1 & echo "TEEPID $!"', timeout=30)
                m = re.search(r'TEEPID (\d+)', out or '')
                pid = m.group(1) if m else None
                self.wait(b['seconds'], b['name'])
                out, end = '', time.time() + 300
                while time.time() < end:
                    _, out = self.dev.su(f'cat {log}', timeout=30)
                    if 'EL2STRESS_DONE tee' in (out or ''):
                        pid = None
                        break
                    self.checkpoint(b['name'])
                    time.sleep(10)
                self.kmsg(f'STAB TEE_IDLE {self.phase} end')
                time.sleep(3)  # the end marker and the last reports into the device's copy and the capture
                self.save(f"tee-idle-{b['name']}.txt", out or '')
                if 'EL2STRESS_DONE tee' not in (out or ''):
                    self.event('STRESS_FAIL', f"{b['name']}: no EL2STRESS_DONE within {b['seconds'] + 300} s")
                    if pid:  # not into the next block
                        self.dev.su(stop_cmd(pid, 'el2stress'), timeout=30)
                        pid = None
                for line in (out or '').splitlines():
                    if line.startswith('EL2STRESS ') and ' rc=' in line and not re.search(r' rc=0\b', line):
                        self.event('STRESS_FAIL', f"{b['name']}: {line[:200]}")
                results.append(self.tee_idle_block(b, out, klog, soff, thr))
                self.event('PHASE_END', b['name'])
        finally:
            if pid:  # interrupted: the load's workers stop with their script
                self.dev.su(stop_cmd(pid, 'el2stress'), timeout=30)
            self.dev.su(watch, timeout=30)
            miss = self.cpuidle_write(orig)
            if miss:
                self.event('RESTORE_FAIL', f'tee_idle: {len(miss)} of {len(orig)} deep idle states not restored (a '
                           'reboot puts them back to the defaults): ' + ' '.join(miss)[:200]
                           + '; original values in tee-idle-cpuidle.txt')
            else:
                self.event('INFO', f'tee_idle: deep idle settings restored ({len(orig)} states)')
            self.phase = base
            self.save('tee-idle.json', json.dumps({'threshold_ms': thr, 'workers': w, 'blocks': results}, indent=1)
                      + '\n')
        self.mark_phase()  # back in the outer phase: later kernel lines are not the last block's

    # ---- Driver: execute ---------------------------------------------------------------------------------------------
    def execute(self, expect_sha):
        """The whole run: plan.json saved, setup, then each phase of the plan as '<n>-<type>' (PHASE_START with the
        plan entry, a phase marker, phase_<type>(entry), not-responding dialogs dismissed, a snapshot, PHASE_END with
        the minutes into the run). An Abort, Ctrl-C / SIGTERM or a harness error (traceback in harness-error.txt) ends
        the phases with an ABORT event, so the run is FAIL. Always, at the end: monkey stopped, a last serial scan,
        with the EL2 log a last pull, the EL2 report (el2-summary.md) and the monitor and its sampling loop stopped,
        and summary.md written."""
        self.save('plan.json', json.dumps(self.plan, indent=2, ensure_ascii=False))
        t0 = time.time()
        try:
            self.setup(expect_sha)
            for i, p in enumerate(self.plan['phases'], 1):
                self.phase = f"{i}-{p['type']}"
                self.event('PHASE_START', json.dumps(p, ensure_ascii=False))
                self.mark_phase()
                getattr(self, 'phase_' + p['type'])(p)
                self.dismiss_anr_dialogs('phase-end')
                self.snapshot('phase-end')
                self.event('PHASE_END', f'{(time.time() - t0) / 60:.0f} min into the run')
        except Abort as e:
            self.event('ABORT', str(e))
        except KeyboardInterrupt:
            self.event('ABORT', 'interrupted')
        except Exception as e:  # a harness bug must never end as PASS
            import traceback
            self.save('harness-error.txt', traceback.format_exc())
            self.event('ABORT', f'harness error {e!r} (traceback in harness-error.txt)')
        finally:
            self.dev.sh('pkill -f com.android.commands.monkey', timeout=20)
            try:
                self.serial_scan()
            except Exception as e:
                self.event('SERIAL_STALE', f'final serial scan failed: {e!r}')
            if self.el2:
                try:
                    self.el2_pull_scan()
                    n = el2_report.report(self.out)
                    self.event('INFO', f'EL2 report el2-summary.md ({n} anomalies; counts {dict(self.el2_counts)})')
                    self.dev.su(f'kill $(cat {DEV}/el2/el2log.pid) 2>/dev/null; echo 0 > /proc/xcore_stats', timeout=30)
                except Exception as e:  # the report must not hide the run's own summary
                    self.event('EL2_LOG_DOWN', f'EL2 report failed: {e!r}')
            write_summary(self.out)


# ==== Report: summary.md ==============================================================================================
def count(evs):
    """Findings in these events: an aggregated event carries `count`."""
    return sum(e.get('count', 1) for e in evs)


def rundown_lines(out):
    """The battery rundown's line of summary.md (rundown.json written by rundown_report), or none."""
    try:
        with open(os.path.join(out, 'rundown.json')) as f:
            r = json.load(f)
    except (OSError, ValueError):
        return []
    fl = ', '.join(f"{k} x{v['count']}" for k, v in r['flags'].items()) or 'no flags'
    if 'duration_s' not in r:
        return [f'- battery rundown: rundown-summary.md (no run on battery; validity: {fl})']
    return [f"- battery rundown: rundown-summary.md ({rundown_report.hm(r['duration_s'])} on battery"
            + (f", {r['energy_wh']:.2f} Wh, average {r['avg_w']:.3f} W" if 'energy_wh' in r else '')
            + f'; validity: {fl})']


def write_summary(out):
    """Writes summary.md of run directory `out` from its events.jsonl and prints its path and verdict: FAIL when any
    event is an error (ERRORS) or an ABORT, else PASS. Contents: the boot_a identity line, pointers to the EL2 and
    battery rundown reports when they exist, event / error / warning counts and a count per kind, the TEE idle table,
    the phase starts and ends, every error, and the first 20 warnings of each kind."""
    evs = [json.loads(l) for l in open(os.path.join(out, 'events.jsonl')) if l.strip()]
    errors = [e for e in evs if e['kind'] in ERRORS]
    warns = [e for e in evs if e['kind'] in WARNINGS]
    phases = [e for e in evs if e['kind'] in ('PHASE_START', 'PHASE_END', 'ABORT')]
    ident = open(os.path.join(out, 'identity.txt')).read().splitlines()[0] if os.path.exists(
        os.path.join(out, 'identity.txt')) else '?'
    verdict = 'FAIL' if errors or any(e['kind'] == 'ABORT' for e in evs) else 'PASS'
    L = [f'# Stability run {os.path.basename(out)}: {verdict}', '', f'- identity: `{ident}`',
         *(['- EL2 parallel log: el2-summary.md, el2-per-second.csv']
           if os.path.exists(os.path.join(out, 'el2-summary.md')) else []),
         *rundown_lines(out),
         f'- events: {count(evs)}; errors {count(errors)}; warnings {count(warns)} (an event with a count stands for '
         'that many findings of one kind)', '', '| kind | count |', '|---|---|']
    kinds = collections.Counter()
    for e in evs:
        kinds[e['kind']] += e.get('count', 1)
    for k, n in sorted(kinds.items()):
        L.append(f'| {k} | {n} |')
    L += tee_idle_lines(out)
    L += ['', '## Phases', ''] + [f"- {e['t']} {e['phase']} {e['kind']} {e['msg'][:160]}" for e in phases]
    L += ['', '## Errors', ''] + [f"- {e['t']} {e['phase']} {e['kind']}: {e['msg']}" for e in errors]
    L += ['', '## Warnings (first 20 events of each kind)', '']
    for k in sorted({e['kind'] for e in warns}):
        ws = [e for e in warns if e['kind'] == k]
        L += [f'### {k} ({count(ws)})', ''] + [f"- {e['t']} {e['phase']}: {e['msg'][:200]}" for e in ws[:20]]
        L += ([f'- ... {len(ws) - 20} more events'] if len(ws) > 20 else []) + ['']
    open(os.path.join(out, 'summary.md'), 'w').write('\n'.join(L) + '\n')
    print(f'summary: {os.path.join(out, "summary.md")} -> {verdict}')


# ==== Command line ====================================================================================================
def main():
    """`stab.py run ...` runs a plan on a device; `stab.py report DIR` rewrites DIR/summary.md (see the module
    docstring, which is the --help text)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('-s', '--serial', default=os.environ.get('ANDROID_SERIAL'))
    r.add_argument('--plan', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--expect-boot-sha', default='')
    r.add_argument('--serial-log', default='',
                   help='line file of a serial capture running alongside (e.g. tools/perf/t11206-serial-capture.sh '
                        'writes serial.txt): scanned for fatal markers, windows saved on resets and losses')
    r.add_argument('--no-el2', action='store_true',
                   help='ignore the plan\'s el2_monitor (an image without the hypervisor, e.g. the stock baseline)')
    rp = sub.add_parser('report')
    rp.add_argument('dir')
    a = ap.parse_args()
    if a.cmd == 'report':
        return write_summary(a.dir)
    if not a.serial:
        sys.exit('no serial: -s or ANDROID_SERIAL')
    plan = json.load(open(a.plan))
    if a.no_el2:
        plan.pop('el2_monitor', None)
    os.makedirs(a.out, exist_ok=True)
    dev = Device(a.serial)
    run = Run(dev, a.out, plan)
    run.serial = a.serial_log or None

    # SIGTERM ends the run like Ctrl-C: the phases' cleanup runs and the summary is written
    def on_term(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, on_term)
    run.execute(a.expect_boot_sha)


if __name__ == '__main__':
    main()
