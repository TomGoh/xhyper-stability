#!/usr/bin/env python3
"""stab.py: stability and stress runs for an Android device whose OS runs under a hypervisor.

  stab.py run    -s SERIAL --plan plans/<plan>.json --out DIR [--expect-boot-sha PREFIX]
  stab.py report DIR

A run executes the plan's phases in order (reboot_loop, soak, suspend, memtest, standby, monitor_soak) and takes a
health snapshot (device/health.sh) at least every `health_interval_s`. Failures are classified from the host side,
because a hypervisor fault rarely leaves a trace inside Android: the device dropping off adb (DEVICE_LOST / DEVICE_GONE),
a boot id that changed without a planned reboot (UNEXPECTED_REBOOT, with the boot reasons and the pstore files of the
previous boot pulled), a boot that never completes (BOOT_FAIL); then Android-side findings (new kernel oops/lockup/
watchdog lines, crash-buffer entries, ANRs, tombstones), stress-tool failures and suspend failures. Every event goes to
events.jsonl and log.txt; summary.md is (re)written at the end, also after Ctrl-C or an abort. Python 3 stdlib only.
"""
import argparse, collections, json, os, re, shlex, signal, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import el2_report  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV = '/data/local/tmp/stab'
ERRORS = {'SERIAL_FATAL', 'UNEXPECTED_REBOOT', 'DEVICE_GONE', 'BOOT_FAIL', 'STRESS_FAIL', 'MEMTEST_FAIL', 'SUSPEND_FAIL',
          'KERNEL_BAD', 'IDENTITY_MISMATCH', 'RESUME_TIMEOUT', 'MONITOR_FAIL'}
WARNINGS = {'SERIAL_STALE', 'EL2_LOG_DOWN', 'EL2_MISSING', 'EL2_HIGH', 'EL2_COUNT_DOWN', 'KERNEL_WARN', 'SUSPEND_ABORTED', 'DEVICE_LOST', 'CRASH', 'ANR', 'TOMBSTONE', 'SUSPEND_NOT_ENTERED', 'SKIP', 'MONKEY_CRASH',
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

    def health(self, marker=''):
        rc, out = self.su(f'sh {DEV}/health.sh {shlex.quote(marker)}' if marker else f'sh {DEV}/health.sh', timeout=90)
        if rc != 0 or 'boot_id=' not in out:
            return None
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
    m = re.match(r'\[\s*([0-9.]+)\]', line)
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


def kernel_kind(line, long_ms=100.0, windows=()):
    """KERNEL_BAD for crashes, lockups, stalls; KERNEL_WARN for WARN_ON splats and short preempt-off reports; None for
    the continuation lines of a report. preemptoff_warn is a vendor debug print (`D:<ms>` = how long preemption was off);
    717 ms of it preceded the watchdog reset of 2026-10-12, so a long one stays an error."""
    if KERNEL_ERROR.search(line):
        return 'KERNEL_BAD'
    m = re.search(r'preemptoff_warn: .* D:([0-9.]+)ms(?: F:([0-9.]+)s)?', line)
    if m:
        d = float(m.group(1)); f = float(m.group(2)) if m.group(2) else ktime(line) - d / 1000
        if d < long_ms:
            return 'KERNEL_WARN'
        # Printing a splat to the synchronous serial console (loglevel 8, 921600 baud on T11206) keeps preemption off
        # for most of a second; such an interval is a consequence of that warning, not a separate stall.
        if any(f <= e + 0.1 and f + d / 1000 >= s0 for s0, e in windows):
            return 'KERNEL_WARN'
        return 'KERNEL_BAD'
    if 'preemptoff_warn' in line or '---[ end trace' in line:
        return None
    return 'KERNEL_WARN'


def num(h, k, default=0):
    try:
        return int(float(h.get(k, default) or default))
    except ValueError:
        return default


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

    # ------------------------------------------------------------------ records
    def event(self, kind, msg='', **kw):
        rec = {'t': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'phase': self.phase, 'kind': kind, 'msg': msg, **kw}
        self.ev.write(json.dumps(rec, ensure_ascii=False) + '\n'); self.ev.flush()
        sev = 'ERROR' if kind in ERRORS else 'WARN' if kind in WARNINGS else ''
        line = f"{rec['t']} [{self.phase}] {sev + ' ' if sev else ''}{kind} {msg}"
        self.logf.write(line + '\n'); self.logf.flush()
        print(line, flush=True)

    def save(self, name, text):
        with open(os.path.join(self.out, name), 'w') as f:
            f.write(text)

    # ------------------------------------------------------------------ health
    def snapshot(self, tag=''):
        h = self.dev.health(f'{self.phase} {self.nsnap + 1} {tag}'.strip())
        if self.serial:
            self.serial_marks.append((time.time(), os.path.getsize(self.serial) if os.path.exists(self.serial) else 0))
            self.serial_marks = self.serial_marks[-50:]
        if h is None:
            return self.lost(tag)
        self.nsnap += 1
        self.save(f'health/{self.nsnap:04d}-{self.phase}{"-" + tag if tag else ""}.txt', h['_raw'])
        prev, self.last = self.last, h
        if prev is None:  # baseline: what is already there is not a finding
            self.known_dumps = set(h.get('pstore_dumps', '').split())
            for s in ('kernel_bad', 'crash_buffer', 'events'):
                self.seen.update(h['_sec'].get(s, []))
            return h
        if h['boot_id'] != prev['boot_id']:
            if self.expect_reboot:
                self.expect_reboot = False
            else:
                self.unexpected_reboot(prev, h)
            for s in ('kernel_bad', 'crash_buffer', 'events'):  # new boot: compare from here on
                self.seen.update(h['_sec'].get(s, []))
            new_dumps = set(h.get('pstore_dumps', '').split()) - self.known_dumps
            if new_dumps:
                self.known_dumps |= new_dumps
                self.event('KERNEL_BAD', f'new panic/oops record in pstore after the reboot: {" ".join(sorted(new_dumps))}')
            if self.el2:
                self.el2_up()
            return h
        for s, kind in (('kernel_bad', 'KERNEL_BAD'), ('crash_buffer', 'CRASH'), ('events', 'ANR')):
            for line in h['_sec'].get(s, []):
                if line not in self.seen:
                    self.seen.add(line)
                    if s == 'kernel_bad':
                        k = kernel_kind(line, windows=printout_windows(h['_sec'].get('kernel_bad', [])))
                        if k:
                            self.event(k, line[:300])
                    elif kind != 'ANR' or 'am_anr' in line:
                        self.event(kind, line[:300])
        for k, kind in (('tombstones', 'TOMBSTONE'), ('anr_files', 'ANR')):
            if num(h, k) > num(prev, k):
                self.event(kind, f'{k} {num(prev, k)} -> {num(h, k)}')
        new_dumps = set(h.get('pstore_dumps', '').split()) - self.known_dumps
        if new_dumps:
            self.known_dumps |= new_dumps
            self.event('KERNEL_BAD', f'new panic/oops record in pstore: {" ".join(sorted(new_dumps))}')
        if num(h, 'suspend_fail') > num(prev, 'suspend_fail') and h.get('suspend_last_failed_dev', '').startswith('alarmtimer'):
            # alarmtimer refuses suspend when an alarm is due within 2 s: the kernel's normal behaviour, not a failure
            self.event('SUSPEND_ABORTED', f"fail {num(prev, 'suspend_fail')} -> {num(h, 'suspend_fail')}: alarm due "
                       f"(last_failed_dev={h.get('suspend_last_failed_dev')})")
        elif num(h, 'suspend_fail') > num(prev, 'suspend_fail'):
            self.event('SUSPEND_FAIL', f"fail {num(prev, 'suspend_fail')} -> {num(h, 'suspend_fail')}, "
                       f"last_failed_dev={h.get('suspend_last_failed_dev')} step={h.get('suspend_last_failed_step')}")
        return h

    def lost(self, tag):
        self.event('DEVICE_LOST', f'no adb/health answer ({tag}); waiting up to {self.lost_timeout} s')
        t0 = time.time()
        if not self.dev.wait_alive(self.lost_timeout):
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
            self.event('DEVICE_GONE', 'back on adb but health snapshot fails'); raise Abort('health fails')
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
        self.dev.su(f'mkdir -p {DEV}/xhmon {DEV}/el2 && chmod 777 {DEV}/xhmon {DEV}/el2')
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
               'n=$(tail -n +2 /proc/xcore_stats | awk "\\$2 > 0" | wc -l); '
               '[ "$n" -gt 0 ] || echo 1 > /proc/xcore_stats; '
               f'pid=$(cat {DEV}/el2/el2log.pid 2>/dev/null); '
               'if [ -z "$pid" ] || ! tr "\\0" " " < /proc/$pid/cmdline 2>/dev/null | grep -q el2log; then '
               f'nohup sh {DEV}/xhmon/el2log.sh {DEV}/el2 {iv} > /dev/null 2>&1 & echo STARTED; fi; echo EL2_UP')
        rc, out = self.dev.su(cmd, timeout=120)
        if 'EL2_UP' not in out:
            self.event('EL2_LOG_DOWN', 'monitor/log not up: ' + ' '.join(out.split())[-200:])
            return False
        self.event('INFO', 'EL2 log up' + (' (loop started)' if 'STARTED' in out else ''))
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
        files = [os.path.join(REPO, 'device', 'health.sh')]
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
            self.event('INFO', f'serial capture {self.serial} from byte {self.serial_scan_off}')
        self.snapshot('baseline')
        if self.el2:
            self.el2_setup()
        self.next_check = time.time() + self.interval

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

    def phase_soak(self, p):
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
                if time.time() >= next_screen:
                    self.screen(False); self.wait(p.get('screen_off_s', 90), 'screen-off'); self.screen(True)
                    next_screen = time.time() + p.get('screen_cycle_min', 15) * 60
                self.wait(min(30, max(1, end - time.time())))
        finally:
            if p.get('monkey', True):
                self.monkey(False)
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
                getattr(self, 'phase_' + p['type'])(p)
                self.snapshot('phase-end')
                self.event('PHASE_END', f'{(time.time() - t0) / 60:.0f} min into the run')
        except Abort as e:
            self.event('ABORT', str(e))
        except KeyboardInterrupt:
            self.event('ABORT', 'interrupted')
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
         f'- events: {len(evs)}; errors {len(errors)}; warnings {len(warns)}', '',
         '| kind | count |', '|---|---|']
    for k, n in sorted(collections.Counter(e['kind'] for e in evs).items()):
        L.append(f'| {k} | {n} |')
    L += ['', '## Phases', ''] + [f"- {e['t']} {e['phase']} {e['kind']} {e['msg'][:160]}" for e in phases]
    L += ['', '## Errors', ''] + [f"- {e['t']} {e['phase']} {e['kind']}: {e['msg']}" for e in errors] + \
         ['', '## Warnings', ''] + [f"- {e['t']} {e['phase']} {e['kind']}: {e['msg'][:200]}" for e in warns[:200]]
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
