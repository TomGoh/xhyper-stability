#!/usr/bin/env python3
"""Tests of the TEE idle phase (phase tee_idle in host/stab.py, device/klogwatch.sh) on synthetic kernel lines in the
device's and the serial capture's formats, and a scripted fake device.

  python3 -m unittest discover -s tests -v       (from the repository root)
"""
# Structure:
#   Fixtures                      synthetic irqsoff reports (report), block markers (mark), serial capture format
#   Report parsing                Reports: stab.trusty_reports
#   Counts and summary            Best: stab.tee_best, stab.tee_idle_lines, stab.stop_cmd
#   Blocks                        Blocks: stab.tee_block, stab.tee_stats
#   Kernel log follower           KlogWatch: device/klogwatch.sh
#   Fake tablet                   TeeTablet: a scripted device for the phase
#   Phase                         Phase: Run.phase_tee_idle end to end on a TeeTablet
import contextlib, io, json, os, re, shlex, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

# the modules under test live in the repository's host/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'host'))
import stab  # noqa: E402

# ==== Fixtures ========================================================================================================

BOOT = '0b0e1d2c-1111-4222-8333-944455556666'


def report(t, cpu, ms, trusty=True, site='0xe4', comm='kworker/u16:3'):
    """A synthetic irqsoff report in the device's dmesg format: the report line, then the 'disabled IRQ at:' and
    'enabled IRQ at:' records whose stack lines carry no time stamp."""
    # the stack below each record: a Trusty standard call (site: the offset in trusty_std_call32), or an interrupt
    stack = ['trusty_std_call_helper_reschedule+0x6c/0x220 [trusty]', f'trusty_std_call32+{site}/0x2b8 [trusty]',
             'kick_vqs+0xe8/0x16c [trusty_virtio]'] if trusty else ['el1_interrupt+0x28/0x64', 'el1h_64_irq+0x7c/0x80']
    return ([f'[{t:12.6f}] irqsoff_warn: C{cpu} T:<{100 + cpu}>{comm} D:{ms:.3f}ms F:{t - ms / 1000:.6f}s',
             f'[{t + 0.001:12.6f}] irqsoff_warn: C{cpu} disabled IRQ at:', 'trace_hardirqs_off+0x188/0x1b4'] + stack
            + [f'[{t + 0.002:12.6f}] irqsoff_warn: C{cpu} enabled IRQ at:', 'trace_hardirqs_on+0x198/0x1c4'] + stack)


def mark(t, name, what):
    """The kernel-log marker the phase writes at a block's start or end (what: 'start' or 'end')."""
    return f'[{t:12.6f}] STAB TEE_IDLE {name} {what}'


def serial(lines, cpu, pid=812, host='11:13:13'):
    """Device-format lines as a serial capture prints them: host time, kernel time and caller on every line."""
    out, t = [], 0.0
    for i, l in enumerate(lines):
        # a line without a time stamp (a stack line) gets the kernel time of the stamped line above it
        kt = stab.ktime(l)
        t = kt if kt is not None else t
        body = l.split('] ', 1)[1] if kt is not None else l
        out.append(f'{host}.{i % 1000:03d} [{t:12.6f}][T{pid}@C{cpu}] {body}')
    return out


# ==== Report parsing ==================================================================================================

class Reports(unittest.TestCase):
    """Guards stab.trusty_reports, which picks the irqsoff reports whose own stack holds a Trusty standard call.
    Fixture: report() records with and without a Trusty stack, interleaved with other CPUs' records or other callers'
    lines, in the device's format and the serial capture's (serial()), and one record whose line breaks dmesg printed
    as \\x0a. Asserts the (cpu, ms, site, comm) of the reports found."""

    def test_trusty_and_not(self):
        rs = stab.trusty_reports(report(1000.3, 2, 243.235) + report(1010.3, 1, 84.142, trusty=False)
                                 + report(1020.3, 0, 50.0, site='0xac'))
        self.assertEqual([(r['cpu'], r['ms'], r['site'], r['comm']) for r in rs],
                         [(2, 243.235, '+0xe4', 'kworker/u16:3'), (0, 50.0, '+0xac', 'kworker/u16:3')])

    def test_other_cpus_record_does_not_count(self):
        """A report without a Trusty stack, followed by another CPU's Trusty 'disabled IRQ at:' record (the device log
        is in kernel order, records of several CPUs interleave): not a Trusty report."""
        other = report(1000.5, 5, 300.0)[1:7]  # C5's 'disabled IRQ at:' record and the line of its next record
        c1 = report(1000.3, 1, 84.0, trusty=False)
        self.assertEqual(stab.trusty_reports(c1[:5] + other + c1[5:]), [])  # between C1's two records

    def test_stack_after_this_cpus_next_report_does_not_count(self):
        lines = report(1000.3, 1, 84.0, trusty=False)[:1] + report(1000.4, 1, 60.0)
        self.assertEqual([r['ms'] for r in stab.trusty_reports(lines)], [60.0])

    def test_serial_format_with_other_callers_between(self):
        """Serial format: another caller's lines between a report's lines do not break it, and another caller's
        Trusty stack does not make a plain report a Trusty one."""
        a = serial(report(1000.3, 2, 243.235), cpu=2)
        noise = serial([f'[{1000.3 + i / 1000:12.6f}] noise: line {i}' for i in range(6)], cpu=4, pid=77)
        lines = a[:2] + noise[:3] + a[2:5] + noise[3:] + a[5:]
        rs = stab.trusty_reports(lines)
        self.assertEqual([(r['cpu'], r['ms'], r['site']) for r in rs], [(2, 243.235, '+0xe4')])
        noisy_trusty = serial(report(1000.5, 4, 70.0)[1:], cpu=4, pid=77)  # another caller's Trusty stack
        plain = serial(report(1000.3, 2, 90.0, trusty=False), cpu=2)
        self.assertEqual(stab.trusty_reports(plain[:2] + noisy_trusty + plain[2:]), [])

    def test_escaped_record(self):  # a dmesg that prints a record's line breaks as \x0a: one line, still found
        rec = report(1000.3, 3, 120.0)
        lines = [rec[0], rec[1] + '\\x0a' + '\\x0a'.join(rec[2:6])]
        self.assertEqual([r['ms'] for r in stab.trusty_reports(lines)], [120.0])


# ==== Counts and summary ==============================================================================================

class Best(unittest.TestCase):
    """Guards stab.tee_best (which copy of a block's counts, device log or serial, is reported), stab.tee_idle_lines
    (the summary.md table from tee-idle.json) and stab.stop_cmd (the guarded stop command). Fixture: hand-made count
    dicts as stab.tee_stats returns them. Asserts the copy chosen, a table row and the exact command text."""

    def test_copy_with_more_reports(self):
        """The copy with more reports at or above the threshold wins, the device's on a tie; the table row shows
        the chosen copy's counts."""
        dev = {'reports': 1, 'ge_thr': 0, 'ge100': 0, 'longest': [12.0], 'sites': {}}
        ser = {'reports': 2, 'ge_thr': 2, 'ge100': 1, 'longest': [140.0, 40.0], 'sites': {'+0xe4': 2}}
        self.assertIs(stab.tee_best({'device': dev, 'serial': ser}), ser)
        self.assertIs(stab.tee_best({'device': ser, 'serial': dict(ser)}), ser)  # a tie: the device's
        self.assertIsNone(stab.tee_best({'device': None, 'serial': None}))
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'tee-idle.json'), 'w') as f:
                json.dump({'threshold_ms': 30, 'blocks': [{'name': 'A1', 'deep_idle': True, 'seconds': 600, 'ops': 9,
                                                           'failures': 0, 'device': dev, 'serial': ser}]}, f)
            self.assertIn('| A1 | on | 600 | 9 / 0 | 0 (0) | 2 (1) | 140, 40 | +0xe4 x2 |', stab.tee_idle_lines(d))

    def test_stop_command(self):
        self.assertEqual(stab.stop_cmd('$(cat /x.pid 2>/dev/null)', 'klogwatch|awk'),
                         'for p in $(cat /x.pid 2>/dev/null); do tr "\\0" " " < /proc/$p/cmdline 2>/dev/null | '
                         "grep -qE 'klogwatch|awk' && { pkill -P $p; kill $p; }; done")


# ==== Blocks ==========================================================================================================

class Blocks(unittest.TestCase):
    """Guards stab.tee_block (the Trusty reports between a block's start and end markers) and stab.tee_stats (their
    counts). Fixture: device_log(), a device log with an older run's block, block A (start, five reports, end) and
    block B (start, no end yet); and a serial capture where the end marker falls between a report and its stack.
    Asserts the counts per block and threshold, None for a block without markers, and the reports kept across the
    end marker."""

    # the block names of the phase's events (phase/block)
    A, B = '3-tee_idle/A1-idle-on', '3-tee_idle/B-idle-off'

    def device_log(self):
        """Kernel log lines: an older run's block A, then block A with four Trusty reports (243, 30, 29.999 and
        100 ms) and one other, then block B with one Trusty report and no end marker."""
        old = [mark(10.0, self.A, 'start')] + report(11.0, 1, 500.0) + [mark(12.0, self.A, 'end')]  # an older run
        a = ([mark(990.0, self.A, 'start')] + report(1000.3, 2, 243.235) + report(1010.3, 1, 84.0, trusty=False)
             + report(1020.3, 0, 30.0) + report(1030.3, 5, 29.999) + report(1040.3, 6, 100.0, site='0xac')
             + [mark(1600.0, self.A, 'end')])
        b = [mark(1601.0, self.B, 'start')] + report(1700.0, 3, 50.0)  # no end marker yet
        return old + a + b

    def test_counts_per_block_and_threshold(self):
        log = self.device_log()
        a = stab.tee_stats(stab.tee_block(log, self.A), 30)
        self.assertEqual(a, {'reports': 4, 'ge_thr': 3, 'ge100': 2, 'longest': [243.235, 100.0, 30.0, 29.999],
                             'sites': {'+0xe4': 2, '+0xac': 1}})
        self.assertEqual(stab.tee_stats(stab.tee_block(log, self.B), 30)['ge_thr'], 1)
        self.assertIsNone(stab.tee_block(log, '3-tee_idle/A2-idle-on'))
        self.assertEqual(stab.tee_stats([], 30), {'reports': 0, 'ge_thr': 0, 'ge100': 0, 'longest': [], 'sites': {}})

    def test_split_across_the_end_marker(self):
        """Serial capture: a report printed before the end marker, its stack after it (the marker came from another
        CPU in between) is the block's; a report after the end marker is not."""
        rec = serial(report(1599.9, 2, 77.0), cpu=2)
        endm = serial([mark(1600.0, self.A, 'end')], cpu=6, pid=900)
        later = serial(report(1600.5, 3, 88.0), cpu=3, pid=813)
        lines = serial([mark(990.0, self.A, 'start')], cpu=6, pid=900) + rec[:1] + endm + rec[1:] + later
        rs = stab.tee_block(lines, self.A)
        self.assertEqual([(r['ms'], r['site']) for r in rs], [(77.0, '+0xe4')])


# ==== Kernel log follower =============================================================================================

SH = shutil.which('dash') or shutil.which('sh')
WATCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'device', 'klogwatch.sh')


@unittest.skipUnless(SH and shutil.which('awk') and shutil.which('pkill'), 'needs sh, awk and pkill')
class KlogWatch(unittest.TestCase):
    """device/klogwatch.sh with a stand-in dmesg: the matching records with their continuation lines, nothing else;
    stopped through its pid file.

    Asserts the lines written to its output file, that its pid file holds two pids (the follower and its filter),
    and that both are gone after the stop."""

    def test_filter_and_stop(self):
        # other records with continuation lines around one Trusty report and a marker
        log = (['[  900.000000] binder: noise', '  noise continuation'] + report(1000.3, 2, 243.235)
               + ['[ 1000.400000] other: line', 'other continuation', mark(1001.0, 'x', 'start')])
        want = report(1000.3, 2, 243.235) + [mark(1001.0, 'x', 'start')]
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'bin'))
            with open(os.path.join(d, 'log.txt'), 'w') as f:
                f.write('\n'.join(log) + '\n')
            # the stand-in dmesg prints the log, then stays alive like dmesg -w
            stub = os.path.join(d, 'bin', 'dmesg')
            with open(stub, 'w') as f:
                f.write(f'#!/bin/sh\ncat {shlex.quote(os.path.join(d, "log.txt"))}\nexec sleep 5\n')
            os.chmod(stub, 0o755)
            out = os.path.join(d, 'k.txt')
            p = subprocess.Popen([SH, WATCH, out, 'irqsoff_warn|STAB TEE_IDLE'], start_new_session=True,
                                 env=dict(os.environ, PATH=os.path.join(d, 'bin') + os.pathsep + os.environ['PATH']))
            # wait up to 10 s for every wanted line, then stop it the way the phase does
            end = time.time() + 10
            while time.time() < end and not (os.path.exists(out) and open(out).read().count('\n') >= len(want)):
                time.sleep(0.05)
            with open(out + '.pid') as f:
                pids = f.read().split()
            subprocess.run(['sh', '-c', f'pkill -P {pids[0]}; kill {" ".join(pids)}'], capture_output=True)
            self.assertIsNotNone(p.wait(timeout=10))
            with open(out) as f:
                self.assertEqual(f.read().splitlines(), want)
            self.assertEqual(len(pids), 2)
            end = time.time() + 5  # the filter gone too (a killed child can stay a zombie for a moment)
            while time.time() < end and subprocess.run(['kill', '-0', pids[1]], capture_output=True).returncode == 0:
                time.sleep(0.05)
            self.assertNotEqual(subprocess.run(['kill', '-0', pids[1]], capture_output=True).returncode, 0)


# ==== Fake tablet =====================================================================================================

class TeeTablet:
    """A scripted device: cpuidle disable files (CPU 0 state 2 disabled before the phase), the kernel log copy of
    klogwatch.sh, a TEE load that adds a Trusty IRQ-off report while every deep idle state is enabled, and a process
    table (pid: command line) for the stop commands (stab.stop_cmd)."""
    serial = 'fake'
    # the follower's copy of the kernel log on the device
    KLOG = f'{stab.DEV}/tmp/tee-idle-klog.txt'

    def __init__(self):
        """Eight CPUs with four idle states each, all enabled except CPU 0 state 2; no processes running."""
        # idle: disable file path -> '0' or '1'; original: the values before the phase
        self.idle = {f'/sys/devices/system/cpu/cpu{c}/cpuidle/state{s}/disable': '0'
                     for c in range(8) for s in range(4)}
        self.idle['/sys/devices/system/cpu/cpu0/cpuidle/state2/disable'] = '1'
        self.original = dict(self.idle)
        # klog: the kernel log lines written so far; t: the kernel time of the last one; cmds: every command sent;
        # killed: pids stopped through stop commands
        self.klog, self.t, self.cmds, self.killed, self.fail = [], 100.0, [], [], None
        self.follows = True  # False: klogwatch.sh keeps no copy (e.g. a dmesg without -w)
        # procs: pid -> command line; watch_pids: the pids in klogwatch.sh's pid file
        self.procs, self.watch_pids, self.readonly = {}, [], False  # readonly: cpuidle writes do not take

    @property
    def watch(self):
        """'running' while the follower from the pid file is still in the process table, else 'stopped'."""
        return 'running' if self.watch_pids and self.watch_pids[0] in self.procs else 'stopped'

    def stop(self, cmd):
        """stab.stop_cmd: the processes of its pid list whose command line matches its pattern (and their children)."""
        m = re.match(r"for p in (.+?); do .*?grep -qE '?([^' ]+?)'? && ", cmd)
        pids = self.watch_pids if m.group(1).startswith('$(cat') else m.group(1).split()
        for p in pids:
            if p in self.procs and re.search(m.group(2), self.procs[p]):
                self.killed.append(p)
                for q in [q for q, c in self.procs.items() if q == p or c.endswith(f'parent={p}')]:
                    del self.procs[q]
        # the rest of the command line after the stop loop
        return cmd.split('; done', 1)[1].lstrip('; ')

    def health(self, marker='', since=None, boot=None):
        """A minimal snapshot of the one boot."""
        return {'boot_id': BOOT, 'suspend_fail': '0', 'dmesg_last': '0', '_raw': 'x', '_sec': {}}

    def adb(self, *args, timeout=60):
        """Succeeds with no output."""
        return 0, ''

    def sh(self, cmd, timeout=60):
        """As su()."""
        return self.su(cmd, timeout)

    def su(self, cmd, timeout=60):
        """Records cmd and answers the phase's commands: the cpuidle listing, the boot id for mark_phase, stop
        commands, and per '; ' part: cpuidle writes, kernel-log markers, starting klogwatch.sh and the TEE load,
        reading the load's log and the follower's copy of the kernel log."""
        self.cmds.append(cmd)
        if cmd.startswith('for f in /sys/devices/system/cpu/cpu*/cpuidle'):
            return 0, ''.join(f'{f}={v}\n' for f, v in self.idle.items())
        if 'random/boot_id' in cmd:  # mark_phase
            return 0, BOOT + '\n'
        if cmd.startswith('for p in '):
            cmd = self.stop(cmd)
        out = []
        for c in cmd.split('; '):
            w = shlex.split(c) if "'" in c else c.split()
            if c.startswith('echo ') and 'cpuidle' in c:
                if not self.readonly:
                    self.idle[w[3]] = w[1]
            elif c.startswith('echo ') and c.endswith('> /dev/kmsg'):
                self.t += 1
                self.klog.append(f'[{self.t:12.6f}] {w[1]}')
            elif 'klogwatch.sh' in c:
                self.procs.update({'4000': 'sh /data/local/tmp/stab/klogwatch.sh x', '4001': 'awk -v r=x parent=4000'})
                self.watch_pids = ['4000', '4001']
            elif 'el2stress.sh tee' in c:
                # env: the environment assignments the load was started with
                self.env = w[:2]
                self.procs['777'] = 'sh /data/local/tmp/stab/el2stress.sh tee 5'
                if all(v == '0' for f, v in self.idle.items() if not f.endswith('state0/disable')):
                    self.t += 1
                    self.klog += report(self.t, 2, 45.5)
                out.append('TEEPID 777')
            elif c.startswith('cat ') and c.endswith('.log'):
                out.append('EL2STRESS tee tee workers=2 ops=120 failures=0 trusty_irq=480 cyclictest=off rc=0\n'
                           'EL2STRESS_DONE tee')
            elif c == f'cat {self.KLOG}' and self.follows:
                out.append('\n'.join(self.klog))
        return 0, '\n'.join(out)


# ==== Phase ===========================================================================================================

class Phase(unittest.TestCase):
    """Guards Run.phase_tee_idle end to end on a TeeTablet with time.sleep patched out: two 5 s blocks (deep idle on,
    then off), a follower left by an earlier run, Ctrl-C during a block, a reboot during a block (stale pids,
    read-only cpuidle files), no copy of the kernel log, and a device without cpuidle. Asserts the deep idle states
    restored, the processes stopped (and only those whose command line still matches), the load's environment,
    tee-idle.json, the events (TEE_IRQSOFF, PHASE_START, RESTORE_FAIL, SKIP, INFO) and the summary.md table."""

    PLAN = {'blocks': [{'name': 'A1-idle-on', 'deep_idle': True, 'seconds': 5},
                       {'name': 'B-idle-off', 'deep_idle': False, 'seconds': 5}]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.sleep = mock.patch.object(stab.time, 'sleep')
        self.sleep.start()
        self.dev = TeeTablet()
        self.r = stab.Run(self.dev, self.tmp.name, {})
        self.r.phase = '3-tee_idle'

    def tearDown(self):
        self.sleep.stop()
        self.r.ev.close()
        self.r.logf.close()
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def events(self, *kinds):
        """The events written so far, only those of the given kinds when any are given."""
        with open(os.path.join(self.tmp.name, 'events.jsonl')) as f:
            return [e for e in map(json.loads, f) if not kinds or e['kind'] in kinds]

    def test_blocks(self):
        """Both blocks run: a Trusty report only with deep idle on (TEE_IRQSOFF), the old follower stopped first,
        idle states restored, tee-idle.json, the events and the summary.md table."""
        self.r.wait = lambda s, t='': None
        # a follower left by an earlier run that was killed: stopped before the new one starts
        self.dev.procs = {'3000': 'sh /data/local/tmp/stab/klogwatch.sh old', '3001': 'awk -v r=old parent=3000'}
        self.dev.watch_pids = ['3000', '3001']
        self.r.phase_tee_idle(self.PLAN)
        self.assertEqual(self.dev.idle, self.dev.original)
        self.assertEqual(self.dev.watch, 'stopped')
        self.assertEqual(self.dev.killed, ['3000', '4000'])  # the load finished in time: not stopped
        self.assertEqual(self.dev.env, ['TEE_CYCLIC=0', 'TEE_WORKERS=2'])
        with open(os.path.join(self.tmp.name, 'tee-idle.json')) as f:
            t = json.load(f)
        a, b = t['blocks']
        self.assertEqual((a['name'], a['deep_idle'], a['ops'], a['failures'], a['device']['ge_thr'], a['serial']),
                         ('A1-idle-on', True, 120, 0, 1, None))
        self.assertEqual((b['deep_idle'], b['device']['ge_thr']), (False, 0))
        warn = self.events('TEE_IRQSOFF')
        self.assertEqual([(e['phase'], e['msg']) for e in warn],
                         [('3-tee_idle/A1-idle-on', "A1-idle-on: 1 Trusty IRQ-off reports >= 30 ms with deep idle on "
                                                    "(longest [45.5] ms, call sites {'+0xe4': 1})")])
        self.assertEqual([e['phase'] for e in self.events('PHASE_START')],
                         ['3-tee_idle/A1-idle-on', '3-tee_idle/B-idle-off'])
        self.assertEqual(self.r.phase, '3-tee_idle')
        stab.write_summary(self.tmp.name)
        with open(os.path.join(self.tmp.name, 'summary.md')) as f:
            text = f.read()
        self.assertIn('## TEE idle: Trusty IRQ-off reports >= 30 ms (>= 100 ms)', text)
        self.assertIn('| A1-idle-on | on | 5 | 120 / 0 | 1 (0) | n/a | 46 | +0xe4 x1 |', text)
        self.assertIn('| B-idle-off | off | 5 | 120 / 0 | 0 (0) | n/a | - | - |', text)

    def test_interrupt_restores_deep_idle(self):
        """Ctrl-C during block B (its wait, the phase's second): load and follower stopped, idle states restored, only
        the finished block A in tee-idle.json."""
        calls = []

        def wait(s, t=''):
            """Stands in for Run.wait: raises KeyboardInterrupt at the second call."""
            calls.append(t)
            if len(calls) == 2:
                raise KeyboardInterrupt()
        self.r.wait = wait
        with self.assertRaises(KeyboardInterrupt):
            self.r.phase_tee_idle(self.PLAN)
        self.assertEqual(self.dev.idle, self.dev.original)
        self.assertEqual(self.dev.watch, 'stopped')
        self.assertEqual(self.dev.killed, ['777', '4000'])
        self.assertEqual(self.dev.procs, {})
        with open(os.path.join(self.tmp.name, 'tee-idle.json')) as f:
            self.assertEqual([b['name'] for b in json.load(f)['blocks']], ['A1-idle-on'])

    def test_stale_pids_after_a_reboot(self):
        """The device rebooted during a block and its pids belong to other processes now: nothing is killed; the deep
        idle states that do not read back are a RESTORE_FAIL."""
        def wait(s, t=''):
            """Stands in for Run.wait: the reboot (other processes on the pids, cpuidle read-only), then Ctrl-C."""
            self.dev.procs = {'777': 'system_server', '4000': 'surfaceflinger', '4001': 'logd'}
            self.dev.readonly = True
            raise KeyboardInterrupt()
        self.r.wait = wait
        with self.assertRaises(KeyboardInterrupt):
            self.r.phase_tee_idle(self.PLAN)
        self.assertEqual(self.dev.killed, [])
        self.assertEqual(len(self.dev.procs), 3)
        fail = self.events('RESTORE_FAIL')
        self.assertEqual(len(fail), 1)
        self.assertIn('tee_idle: 1 of 24 deep idle states not restored (a reboot puts them back to the defaults): '
                      '/sys/devices/system/cpu/cpu0/cpuidle/state2/disable', fail[0]['msg'])

    def test_no_copy_of_the_kernel_log(self):
        """The follower kept no copy: each block is a SKIP, no TEE_IRQSOFF, the device log counts read n/a."""
        self.dev.follows = False
        self.r.wait = lambda s, t='': None
        self.r.phase_tee_idle(self.PLAN)
        self.assertEqual([e['msg'][:41] for e in self.events('SKIP')],
                         ["A1-idle-on: the block's markers are in no", "B-idle-off: the block's markers are in no"])
        self.assertEqual(self.events('TEE_IRQSOFF'), [])
        self.assertIn('device log n/a (no block markers in this copy)', self.events('INFO')[0]['msg'])

    def test_no_cpuidle(self):  # a device without cpuidle states: one SKIP, nothing else
        self.dev.idle = {}
        self.r.phase_tee_idle({})
        self.assertEqual([e['kind'] for e in self.events()], ['SKIP'])


if __name__ == '__main__':
    unittest.main()
