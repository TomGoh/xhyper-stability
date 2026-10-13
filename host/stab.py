#!/usr/bin/env python3
"""stab.py: stability and stress runs for an Android device whose OS runs under a hypervisor.

  stab.py run    -s SERIAL --plan plans/<plan>.json --out DIR [--expect-boot-sha PREFIX]
  stab.py report DIR

A run executes the plan's phases in order (reboot_loop, soak, suspend, memtest, standby, monitor_soak) and takes a
health snapshot (device/health.sh) at least every `health_interval_s`. Failures are classified from the host side,
because a hypervisor fault rarely leaves a trace inside Android: the device dropping off adb (DEVICE_LOST / DEVICE_GONE),
a boot id that changed without a planned reboot (UNEXPECTED_REBOOT, with the boot reasons and the pstore files of the
previous boot pulled), a boot that never completes (BOOT_FAIL); then Android-side findings (new kernel oops/lockup/
watchdog lines, crash-buffer entries, ANRs, tombstones), stress-tool failures and suspend failures. Kernel lines are
attributed by their kernel time to the phase they happened in (a `STAB PHASE_START` marker in the kernel log at every
phase start), not to the snapshot that collected them. Every event goes to events.jsonl and log.txt; summary.md is
(re)written at the end, also after Ctrl-C or an abort. Python 3 stdlib only.
"""
import argparse, collections, json, os, re, shlex, signal, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import el2_report  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV = '/data/local/tmp/stab'
ERRORS = {'DEVICE_MISSING', 'SERIAL_FATAL', 'UNEXPECTED_REBOOT', 'DEVICE_GONE', 'BOOT_FAIL', 'STRESS_FAIL', 'MEMTEST_FAIL', 'SUSPEND_FAIL',
          'KERNEL_BAD', 'IDENTITY_MISMATCH', 'RESUME_TIMEOUT', 'MONITOR_FAIL'}
WARNINGS = {'PC_MODE', 'ANR_DIALOG', 'HV_WAKEUP_AUDIT', 'SERIAL_STALE', 'EL2_LOG_DOWN', 'EL2_MISSING', 'EL2_HIGH', 'EL2_COUNT_DOWN', 'KERNEL_WARN', 'SUSPEND_ABORTED', 'DEVICE_LOST', 'PROBE_TIMEOUT', 'KERNEL_LOG_CAPPED', 'HEALTH_DEGRADED', 'CRASH', 'ANR', 'TOMBSTONE', 'SUSPEND_NOT_ENTERED', 'SKIP', 'MONKEY_CRASH',
            'MONKEY_ANR', 'BOOT_REASON'}


class Abort(Exception):
    pass


class Device:
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args, timeout=60):
        try:
            p = subprocess.run(['adb', '-s', self.serial, *args], capture_output=True, text=True,
                               errors='replace', timeout=timeout)
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return None, 'TIMEOUT'

    def sh(self, cmd, timeout=60):
        return self.adb('shell', cmd, timeout=timeout)

    def su(self, cmd, timeout=60):  # tablets: `su 0`, never `adb root`
        return self.sh('su 0 sh -c ' + shlex.quote(cmd), timeout=timeout)

    def alive(self):
        rc, _ = self.adb('shell', 'true', timeout=10)
        return rc == 0

    def wait_alive(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self.alive():
                return True
            time.sleep(5)
        return False

    def wait_drop(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive():
                return True
            time.sleep(1)
        return False

    def wait_boot(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            rc, out = self.sh('getprop sys.boot_completed', timeout=10)
            if rc == 0 and out.strip() == '1':
                return True
            time.sleep(5)
        return False

    def health(self, marker='', since=None, boot=None):
        """Parsed snapshot, or None; self.fail then says why: 'timeout' (no answer in 180 s) or the failure.
        since, boot: the previous snapshot's dmesg_last and boot_id (only newer kernel lines on that boot)."""
        args = ' '.join(shlex.quote(str(a)) for a in (marker, since or '', boot or ''))
        # 180 s: under the all-at-once EL2 stress a snapshot (dumpsys, logcat) took over 90 s on T11206 (2026-10-13)
        rc, out = self.su(f'sh {DEV}/health.sh {args}', timeout=180)
        if rc != 0 or 'boot_id=' not in out:
            self.fail = 'timeout' if rc is None else f'rc={rc}: ' + ' '.join(out.split())[-120:]
            return None
        self.fail = None
        return parse_health(out)


def parse_health(out):
    """health.sh output -> {key: value, '_raw': out, '_sec': {section: [lines]}}."""
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


# Fatal markers on the serial console (Linux console and the hypervisor console share the UART on T11206): the
# hypervisor's panic handler (core/kruntime/src/lang_items.rs prints 'XHYPER PANIC:' then 'XHYPER_PANIC_HOLD'), the
# Manager's fatal stop, hypervisor boot errors, a Linux panic, and the boot loader's record of the previous reset.
SERIAL_FATAL = re.compile(rb'XHYPER PANIC|XHYPER_PANIC_HOLD|MANAGER_FATAL|XHYPER_[A-Z_]*ERROR|Kernel panic|'
                          rb'exception_reboot_reason|exception_panic_reason|wdt fiq')
KERNEL_ERROR = re.compile(r'BUG:|Oops|Unable to handle|soft lockup|hard LOCKUP|rcu.*(stall|self-detected)|'
                          r'blocked for more than|Kernel panic|SError|synchronous external abort|Unhandled')


def ko_load_order(d):
    """Module names of the .ko files in d, ordered by their depends= (a topological sort)."""
    deps = {}
    for f in sorted(os.listdir(d)):
        if f.endswith('.ko'):
            b = open(os.path.join(d, f), 'rb').read()
            n = re.search(rb'\x00name=([^\x00]+)', b); dp = re.search(rb'\x00depends=([^\x00]*)', b)
            deps[n.group(1).decode() if n else f[:-3]] = [x for x in (dp.group(1).decode().split(',') if dp else []) if x]
    order = []
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


def ktime(line):
    m = re.match(r'\[\s*(\d+(?:\.\d+)?)\]', line)
    return float(m.group(1)) if m else None


def printout_windows(lines):
    """[start, end] of every WARN/BUG splat in the kernel log (from the 'WARNING: CPU' line to its '---[ end trace')."""
    out, start = [], None
    for l in lines:
        if re.search(r'WARNING: CPU|BUG:|Oops', l):
            start = ktime(l)
        elif '---[ end trace' in l and start is not None:
            out.append((start, ktime(l) or start)); start = None
    return out


OFF_REPORT = re.compile(r'\b(preemptoff|irqsoff)_warn: C(\d+) T:<\d+>(.*?) D:\s*(\d+(?:\.\d+)?)\s*ms'
                        r'(?: F:(\d+(?:\.\d+)?)s)?')
CONTEXT_LINE = re.compile(r'(preemptoff|irqsoff)_warn|---\[ end trace|Resume caused by|Wakeup pending')
WAKE_IRQ = re.compile(r'Resume caused by (?:misconfigured )?IRQ \d+,? (\S+)')
SERIAL_BPS = 92160      # bytes per second of the serial console: 921600 baud 8N1 (T11206)
CONSOLE_SHARE = 0.5     # bytes stamped in [F - CONSOLE_LEAD, F + D] >= this share of what the line carries in D
CONSOLE_LEAD = 0.2
KERNEL_CAP = 2000       # health.sh KCAP: newest kernel lines per snapshot
GROUP_FIRST = 3         # preempt/IRQ-off reports written one by one per (phase, group); the rest are counted
REASON_CLASS = {'console-bound:': 'console', 'waits': 'wait', 'inside': 'splat'}  # first word of kernel_grade's why
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
        self.bins, self.part, self.top, self.cur, self.pruned = {}, b'', None, None, None

    def feed(self, chunk):
        lines = (self.part + chunk).split(b'\n')
        self.part = lines.pop()
        if len(self.part) > self.PART_MAX:  # no line end in sight: count it as a line
            lines.append(self.part); self.part = b''
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


def attribute(marks, boot, t):
    """Phase of the latest (boot, kernel time, phase) marker at or before kernel time t in boot `boot`, else None."""
    best = max(((k, i, ph) for i, (b, k, ph) in enumerate(marks) if b == boot and t is not None and k <= t),
               default=None)
    return best[2] if best else None


def num(h, k, default=0):
    try:
        return int(float(h.get(k, default) or default))
    except ValueError:
        return default


def fnum(h, k):
    try:
        return float(h.get(k))
    except (TypeError, ValueError):
        return None


class Run:
    def __init__(self, dev, out, plan):
        self.dev, self.out, self.plan = dev, out, plan
        os.makedirs(os.path.join(out, 'health'), exist_ok=True)
        self.ev = open(os.path.join(out, 'events.jsonl'), 'a')
        self.logf = open(os.path.join(out, 'log.txt'), 'a')
        self.last = None
        self.seen = set()
        self.nsnap = 0
        self.expect_reboot = False
        self.phase = 'setup'
        self.interval = plan.get('health_interval_s', 300)
        self.lost_timeout = plan.get('lost_timeout_s', 900)
        self.next_check = 0
        self.tools = set()
        self.known_dumps = set()
        self.el2 = plan.get('el2_monitor')
        self.el2_order = []
        self.el2_counts = collections.Counter()
        self.scanner = None
        self.serial = None          # path of a serial capture's line file (host-stamped), written by another process
        self.serial_scan_off = 0
        self.serial_marks = []      # (epoch, file size) at each snapshot
        self.serial_stale = False
        self.nserial = 0
        self.console, self.console_off = ConsoleHist(), 0  # serial bytes per kernel time (own offset into the capture)
        self.irq_kept = []          # irqsoff intervals of the last 3 snapshots (a waiter's report can come later)
        self.marks = []             # (boot_id, kernel time, phase) of every STAB PHASE_START marker
        self.probe_retry = False
        self.good_t = time.time()   # time of the last good snapshot
        self.held, self.held_once = [], set()  # reports whose console bytes had not reached the capture yet
        self.group_n = collections.Counter()  # (phase, group) -> preempt/IRQ-off reports seen
        self.kept_lines = []        # kernel lines of the previous snapshot (a WARN splat can span two snapshots)
        self.degraded = None        # boot id for which HEALTH_DEGRADED was reported

    # ------------------------------------------------------------------ records
    def event(self, kind, msg='', phase=None, **kw):
        """phase: the phase the finding belongs to when it is not the current one (kernel lines, by kernel time)."""
        rec = {'t': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'phase': phase or self.phase, 'kind': kind, 'msg': msg, **kw}
        self.ev.write(json.dumps(rec, ensure_ascii=False) + '\n'); self.ev.flush()
        sev = 'ERROR' if kind in ERRORS else 'WARN' if kind in WARNINGS else ''
        line = f"{rec['t']} [{rec['phase']}] {sev + ' ' if sev else ''}{kind} {msg}"
        self.logf.write(line + '\n'); self.logf.flush()
        print(line, flush=True)

    def save(self, name, text):
        with open(os.path.join(self.out, name), 'w') as f:
            f.write(text)

    # ------------------------------------------------------------------ health
    def snapshot(self, tag=''):
        last = self.last or {}
        h = self.dev.health(f'{self.phase} {self.nsnap + 1} {tag}'.strip(), last.get('dmesg_last'), last.get('boot_id'))
        if self.serial:
            self.serial_marks.append((time.time(), os.path.getsize(self.serial) if os.path.exists(self.serial) else 0))
            self.serial_marks = self.serial_marks[-50:]
        if h is None:
            return self.lost(tag)
        self.nsnap, self.good_t = self.nsnap + 1, time.time()
        name = re.sub(r'[^A-Za-z0-9_.-]+', '_', f'{self.phase}{"-" + tag if tag else ""}')
        self.save(f'health/{self.nsnap:04d}-{name}.txt', h['_raw'])
        prev, self.last = self.last, h
        if (prev is None or h.get('boot_id') != prev.get('boot_id')) and (h.get('pc_mode') == '1' or h.get('desk_visible') == '1'):
            self.event('PC_MODE', f"device is in desktop (PC) mode (kylin_rotation_state_saved_before_pc_mode="
                       f"{h.get('pc_mode')}, Desk visible={h.get('desk_visible')}): the plans expect tablet mode")
        if prev is None:  # baseline: what is already there is not a finding
            self.known_dumps = set(h.get('pstore_dumps', '').split())
            self.inventory = set(h['_sec'].get('inventory', []))
            for s in ('kernel_bad', 'crash_buffer', 'events'):
                self.seen.update(h['_sec'].get(s, []))
            self.irq_kept = [irq_intervals(h['_sec'].get('kernel_bad', []))]
            return h
        if h['boot_id'] != prev['boot_id'] or tag.startswith('suspend'):
            self.check_inventory(h, tag)
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
                self.event('KERNEL_BAD', f'new panic/oops record in pstore after the reboot: {" ".join(sorted(new_dumps))}')
            if self.el2:
                self.el2_up()
            return h
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
            self.event('SUSPEND_ABORTED', f"fail {num(prev, 'suspend_fail')} -> {num(h, 'suspend_fail')}: wake interrupt "
                       f"pending from {adev} ({h.get('wakeup_reason', '')[:200]})")
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
        """Grades the unseen lines of boot `boot` (grade_kernel); context: the lines for WARN splat windows."""
        irqs = {i for batch in self.irq_kept for i in batch}
        windows = printout_windows(context)
        more = {}  # (phase, group, kind) -> [count, max D, its F]
        for line in lines:
            if line in self.seen:
                continue
            off = parse_off(line)
            if (hold and hist is not None and hist.top is not None and off and off[3] >= 100.0
                    and line not in self.held_once and hist.top < off[4] + off[3] / 1000):
                self.held.append(line); self.held_once.add(line)
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
            if self.group_n[(ph, group, kind)] <= GROUP_FIRST:
                self.event(kind, line[:300] + (f' [{why}]' if why else '') + tail, phase=ph, group=group)
                continue
            m = more.setdefault((ph, group, kind), [0, 0.0, 0.0])
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
                    self.console.feed(b); self.console_off += len(b)
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

    def serial_extract(self, why, before_s=600, cap=30 << 20):
        """Serial lines from about before_s seconds before now to the end of the capture, into serial-NNN-<why>.txt."""
        if not self.serial or not os.path.exists(self.serial):
            return
        end = os.path.getsize(self.serial)
        start = next((off for t, off in reversed(self.serial_marks) if t <= time.time() - before_s),
                     self.serial_marks[0][1] if self.serial_marks else max(0, end - cap))
        start = max(start, end - cap)
        self.nserial += 1
        name = f'serial-{self.nserial:03d}-{why}.txt'
        with open(self.serial, 'rb') as f, open(os.path.join(self.out, name), 'wb') as o:
            f.seek(start); o.write(f.read(end - start))
        self.event('INFO', f'serial window saved: {name} ({(end - start) >> 10} KiB)')

    def serial_scan(self):
        if not self.serial or not os.path.exists(self.serial):
            return
        end = os.path.getsize(self.serial)
        if end == self.serial_scan_off:
            if not self.serial_stale:
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

    def checkpoint(self, tag=''):
        if time.time() >= self.next_check:
            self.snapshot(tag)
            self.serial_scan()
            if self.el2:
                self.el2_pull_scan()
            self.next_check = time.time() + self.interval

    # ------------------------------------------------------------------ EL2 parallel log
    def el2_setup(self):
        art = os.path.expanduser(os.path.expandvars(self.el2['artifacts']))
        if '$' in art or not os.path.isdir(art):
            self.event('EL2_LOG_DOWN', f"monitor module directory not found: {self.el2['artifacts']} -> {art} "
                       '(set XHYPER_MONITOR_ARTIFACTS)'); self.el2 = None; return
        self.el2_order = ko_load_order(art)
        kos = [os.path.join(art, f'{m}.ko') for m in self.el2_order]
        # Logs of earlier runs were pulled by those runs; a stale log would be scanned again as this run's data.
        self.dev.su(f'kill $(cat {DEV}/el2/el2log.pid 2>/dev/null) 2>/dev/null; rm -f {DEV}/el2/*.log {DEV}/el2/el2log.pid; '
                    f'mkdir -p {DEV}/xhmon {DEV}/el2 && chmod 777 {DEV}/xhmon {DEV}/el2')
        rc, out = self.dev.adb('push', *kos, os.path.join(REPO, 'device', 'el2log.sh'), f'{DEV}/xhmon/', timeout=300)
        if rc != 0:
            self.event('EL2_LOG_DOWN', 'push of monitor modules failed: ' + out[-200:]); self.el2 = None; return
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
        self.dev.adb('pull', f'{DEV}/el2/.', os.path.join(self.out, 'el2'), timeout=300)
        if not self.scanner:
            return
        for kind, ep, msg in self.scanner.scan():
            self.el2_counts[kind] += 1
            if self.el2_counts[kind] <= 20:  # the rest is in el2-summary.md
                self.event(kind, msg)
            elif self.el2_counts[kind] == 21:
                self.event(kind, 'more of these: counted in el2-summary.md, not logged one by one')

    def wait(self, seconds, tag=''):
        end = time.time() + seconds
        while time.time() < end:
            self.checkpoint(tag)
            time.sleep(min(15, max(0, end - time.time())))

    # ------------------------------------------------------------------ setup
    def setup(self, expect_sha):
        if not self.dev.alive():
            raise Abort(f'device {self.dev.serial} not reachable')
        self.dev.su(f'mkdir -p {DEV}/tmp && chmod 777 {DEV} {DEV}/tmp')
        files = [os.path.join(REPO, 'device', 'health.sh'), os.path.join(REPO, 'device', 'el2stress.sh')]
        tdir = os.path.join(REPO, 'third_party', 'out', 'aarch64')
        for t in ('stress-ng', 'stressapptest', 'cyclictest', 'hackbench'):
            if os.path.exists(os.path.join(tdir, t)):
                files.append(os.path.join(tdir, t)); self.tools.add(t)
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
        """Install the APKs named in third_party/apks.lock from STAB_APK_DIR, after checking their sha256."""
        lock = {}
        for line in open(os.path.join(REPO, 'third_party', 'apks.lock')):
            f = line.split()
            if len(f) >= 6 and not line.startswith('#'):
                lock[f[0]] = f
        d = os.environ.get('STAB_APK_DIR', '')
        for k in keys:
            if k not in lock:
                self.event('SKIP', f'apk {k}: not in third_party/apks.lock'); continue
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
                self.event('SKIP', f'apk {k}: {fname} sha256 {h[:16]} is not the locked {sha[:16]}'); continue
            args = ['install', '-r'] + ([flags] if flags != '-' else []) + [path]
            rc, out = self.dev.adb(*args, timeout=600)
            self.event('INFO' if 'Success' in out else 'SKIP', f'apk {k} ({pkg} {ver}): ' + out.strip().splitlines()[-1][:120])

    def dismiss_anr_dialogs(self, tag, tries=4):
        """A not-responding dialog left on screen blocks every later UI step: answer 'wait' (等待) and record it."""
        for _ in range(tries):
            _, f = self.dev.sh('dumpsys window | grep -m1 mCurrentFocus', timeout=60)
            if 'Not Responding' not in f:
                return
            _, x = self.dev.sh('uiautomator dump /sdcard/stab-ui.xml >/dev/null 2>&1; cat /sdcard/stab-ui.xml', timeout=90)
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
        if name in self.tools:
            return True
        self.event('SKIP', f'{name} not built (third_party/out/aarch64/{name}); step skipped')
        return False

    def screen(self, on):
        if on:
            self.dev.sh('input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard', timeout=30)
        else:
            self.dev.sh('input keyevent KEYCODE_SLEEP', timeout=30)

    # ------------------------------------------------------------------ phases
    def phase_reboot_loop(self, p):
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

    def stress_burst(self, seconds, args, tag):
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
        _, out = self.dev.sh('dumpsys media_session', timeout=30)
        m = re.search(r'package=' + re.escape(pkg) + r'.*?state=PlaybackState \{state=([A-Z_]+)|package='
                      + re.escape(pkg) + r'[\s\S]{0,800}?state=([A-Z_]+)\(', out)
        return (m.group(1) or m.group(2)) if m else 'NONE'

    def music_play(self, mu):
        """Plays mu['path'] in the music app's track page through a VIEW intent on its MediaStore entry (the way
        workspace tools/perf/t11206-music.sh drives the Kylin music app). Volume set with key events (the media_session
        volume command does not move the speaker index on T11206)."""
        pkg = mu.get('package', 'com.kylinos.music')
        _, q = self.dev.sh('content query --uri content://media/external/audio/media --projection _id:_data', timeout=30)
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
                self.event('SKIP', f"music: {mu.get('path', '/sdcard/Music/test.mp3')} not in the device's media library")
            return False
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
                    self.screen(False); self.wait(p.get('screen_off_s', 90), 'screen-off'); self.screen(True)
                    next_screen = time.time() + p.get('screen_cycle_min', 15) * 60
                self.wait(min(30, max(1, end - time.time())))
        finally:
            if p.get('monkey', True):
                self.monkey(False)
            if mu:  # do not carry playback into the suspend phases (a forced suspend that cuts playback pops)
                self.dev.sh('cmd media_session dispatch pause', timeout=30)
            self.screen(True)

    def suspend_once(self, sleep_s, i):
        h0 = self.snapshot(f'pre-suspend{i}')
        s0, f0 = num(h0, 'suspend_success'), num(h0, 'suspend_fail')
        self.screen(False)
        self.wait_audio_idle()
        t0 = time.time()
        # Detached: USB and adb drop while the device sleeps, so the command must not depend on the adb session.
        self.dev.su(f'nohup rtcwake -m mem -s {sleep_s} > {DEV}/rtcwake.log 2>&1 &', timeout=20)
        if not self.dev.wait_drop(30):  # still on adb: the suspend was refused or adb survived it
            time.sleep(sleep_s + 5)
        if not self.dev.wait_alive(sleep_s + 180):
            self.event('RESUME_TIMEOUT', f'suspend {i}: no adb {sleep_s + 180} s after a {sleep_s} s alarm')
            return self.snapshot(f'suspend{i}-late')
        back = time.time() - t0
        h = self.snapshot(f'suspend{i}')
        ds, df = num(h, 'suspend_success') - s0, num(h, 'suspend_fail') - f0
        if h.get('boot_id') != h0.get('boot_id'):
            return h
        if ds < 1 and df < 1:
            self.event('SUSPEND_NOT_ENTERED', f'suspend {i}: success +{ds}, fail +{df} (aborted before suspend?)')
        self.event('INFO', f'suspend {i}: success +{ds} fail +{df}, adb back after {back:.0f} s')
        return h

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
        for i in range(1, p.get('cycles', 50) + 1):
            self.suspend_once(p.get('sleep_s', 20), i)
            self.screen(True)
            time.sleep(p.get('awake_s', 10))
        self.screen(True)

    def phase_memtest(self, p):
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

    def phase_standby(self, p):
        cycles = max(1, int(p['hours'] * 3600 / (p.get('suspend_s', 1500) + 60)))
        for i in range(1, cycles + 1):
            self.suspend_once(p.get('suspend_s', 1500), f'standby{i}')
            self.screen(True)
            time.sleep(p.get('awake_s', 30))
        self.screen(True)

    def phase_latency(self, p):
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

    def phase_el2_stress(self, p):
        """Steps of device/el2stress.sh, one exit class each, measured: EL2 share per CPU from the EL2 parallel log and
        the hypervisor's hot-path counters (XHYPER_*_INPLACE, SGI, idle-wake lines) from the serial console."""
        _, rt = self.dev.sh('ip route show table all', timeout=30)
        m = re.search(r'default via (\S+) dev wlan0', rt)
        gw = m.group(1) if m else ''
        base, secs = self.phase, int(p.get('seconds', 180))
        if any(s_ in ('gpu', 'all') for s_ in p.get('steps', [])):
            self.ensure_apks(['glmark2'])
        for step in p.get('steps', ['ipi', 'timer', 'idle', 'traffic', 'affinity', 'hotplug', 'pmu', 'all']):
            self.phase = f'{base}/{step}'
            self.event('PHASE_START', f'{step} {secs} s')
            self.mark_phase()
            t0 = time.time()
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
            self.wait(secs, step)
            out, end = '', time.time() + 300
            while time.time() < end:
                _, out = self.dev.su(f'cat {DEV}/tmp/el2stress-{step}.log', timeout=30)
                if f'EL2STRESS_DONE {step}' in out:
                    break
                self.checkpoint(step)
                time.sleep(10)
            if usb:
                usb.kill()
            self.dismiss_anr_dialogs(step)
            self.save(f'el2stress-{step}.txt', out)
            if f'EL2STRESS_DONE {step}' not in out:
                self.event('STRESS_FAIL', f'{step}: no EL2STRESS_DONE within {secs + 300} s')
                self.snapshot(f'{step}-late')
            for l in out.splitlines():
                if l.startswith('EL2STRESS ') and ' rc=' in l and not re.search(r' rc=0\b', l):
                    self.event('STRESS_FAIL', f'{step}: {l[:200]}')
            parts = ' | '.join(l[10 + len(step):].strip() for l in out.splitlines()
                               if l.startswith(f'EL2STRESS {step} '))
            self.event('INFO', f'{step}: {parts[:400]}')
            if step == 'pmu' and self.el2:
                self.el2_up()
            if self.serial and os.path.exists(self.serial):
                with open(self.serial, 'rb') as f:
                    f.seek(soff)
                    hv = [l for l in f.read().splitlines() if b'XHYPER_' in l]
                with open(os.path.join(self.out, f'el2stress-{step}-serial.txt'), 'wb') as o:
                    o.write(b'\n'.join(hv) + b'\n')
                last = {}
                for l in hv:
                    mm = re.search(rb'(XHYPER_[A-Z0-9_]+)', l)
                    if mm:
                        last[mm.group(1).decode()] = l.decode('utf-8', 'replace')
                keep = [v for k, v in last.items() if k.endswith(('_INPLACE', '_COUNTS', 'IDLE_WAKE', 'REFUSED'))]
                self.event('INFO', f'{step}: hypervisor counters at end: ' + ' || '.join(x[13:200] for x in keep)[:900])
            if self.el2:
                self.el2_pull_scan()
                w = el2_report.window(self.out, t0 + 5, time.time())
                if w:
                    self.event('EL2_LOAD', f'{step}: EL2 share mean/max per CPU ' +
                               ' '.join(f'{c}:{m_:.0%}/{x:.0%}' for c, (m_, x, _) in w.items()))
            self.event('PHASE_END', step)
        self.phase = base
        self.mark_phase()  # back in the outer phase: later kernel lines are not the last step's

    def phase_monitor_soak(self, p):
        script = os.path.expanduser(p.get('skill', '~/.codex/skills/xhyper-el2-monitor-android/scripts/el2-monitor.sh'))
        r = subprocess.run(['bash', script, 'enable', '-s', self.dev.serial, '--out', os.path.join(self.out, 'monitor'),
                            os.path.expanduser(os.path.expandvars(p['artifacts']))], capture_output=True, text=True, errors='replace')
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

    # ------------------------------------------------------------------ driver
    def execute(self, expect_sha):
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


def count(evs):
    """Findings in these events: an aggregated event carries `count`."""
    return sum(e.get('count', 1) for e in evs)


def write_summary(out):
    evs = [json.loads(l) for l in open(os.path.join(out, 'events.jsonl')) if l.strip()]
    errors = [e for e in evs if e['kind'] in ERRORS]
    warns = [e for e in evs if e['kind'] in WARNINGS]
    phases = [e for e in evs if e['kind'] in ('PHASE_START', 'PHASE_END', 'ABORT')]
    ident = open(os.path.join(out, 'identity.txt')).read().splitlines()[0] if os.path.exists(
        os.path.join(out, 'identity.txt')) else '?'
    verdict = 'FAIL' if errors or any(e['kind'] == 'ABORT' for e in evs) else 'PASS'
    L = [f'# Stability run {os.path.basename(out)}: {verdict}', '', f'- identity: `{ident}`',
         *(['- EL2 parallel log: el2-summary.md, el2-per-second.csv'] if os.path.exists(os.path.join(out, 'el2-summary.md')) else []),
         f'- events: {count(evs)}; errors {count(errors)}; warnings {count(warns)} (an event with a count stands for '
         'that many findings of one kind)', '', '| kind | count |', '|---|---|']
    kinds = collections.Counter()
    for e in evs:
        kinds[e['kind']] += e.get('count', 1)
    for k, n in sorted(kinds.items()):
        L.append(f'| {k} | {n} |')
    L += ['', '## Phases', ''] + [f"- {e['t']} {e['phase']} {e['kind']} {e['msg'][:160]}" for e in phases]
    L += ['', '## Errors', ''] + [f"- {e['t']} {e['phase']} {e['kind']}: {e['msg']}" for e in errors]
    L += ['', '## Warnings (first 20 events of each kind)', '']
    for k in sorted({e['kind'] for e in warns}):
        ws = [e for e in warns if e['kind'] == k]
        L += [f'### {k} ({count(ws)})', ''] + [f"- {e['t']} {e['phase']}: {e['msg'][:200]}" for e in ws[:20]]
        L += ([f'- ... {len(ws) - 20} more events'] if len(ws) > 20 else []) + ['']
    open(os.path.join(out, 'summary.md'), 'w').write('\n'.join(L) + '\n')
    print(f'summary: {os.path.join(out, "summary.md")} -> {verdict}')


def main():
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
    def on_term(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, on_term)
    run.execute(a.expect_boot_sha)


if __name__ == '__main__':
    main()
