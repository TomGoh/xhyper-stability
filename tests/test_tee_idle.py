#!/usr/bin/env python3
"""Tests of the TEE idle phase (phase tee_idle in host/stab.py, device/klogwatch.sh) on synthetic kernel lines in the
device's and the serial capture's formats, and a scripted fake device.

  python3 -m unittest discover -s host -p 'test_*.py' -v
"""
import contextlib, io, json, os, re, shlex, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'host'))
import stab  # noqa: E402

BOOT = '0b0e1d2c-1111-4222-8333-944455556666'


def report(t, cpu, ms, trusty=True, site='0xe4', comm='kworker/u16:3'):
    """A synthetic irqsoff report in the device's dmesg format: the report line, then the 'disabled IRQ at:' and
    'enabled IRQ at:' records whose stack lines carry no time stamp."""
    stack = ['trusty_std_call_helper_reschedule+0x6c/0x220 [trusty]', f'trusty_std_call32+{site}/0x2b8 [trusty]',
             'kick_vqs+0xe8/0x16c [trusty_virtio]'] if trusty else ['el1_interrupt+0x28/0x64', 'el1h_64_irq+0x7c/0x80']
    return ([f'[{t:12.6f}] irqsoff_warn: C{cpu} T:<{100 + cpu}>{comm} D:{ms:.3f}ms F:{t - ms / 1000:.6f}s',
             f'[{t + 0.001:12.6f}] irqsoff_warn: C{cpu} disabled IRQ at:', 'trace_hardirqs_off+0x188/0x1b4'] + stack
            + [f'[{t + 0.002:12.6f}] irqsoff_warn: C{cpu} enabled IRQ at:', 'trace_hardirqs_on+0x198/0x1c4'] + stack)


def mark(t, name, what):
    return f'[{t:12.6f}] STAB TEE_IDLE {name} {what}'


def serial(lines, cpu, pid=812, host='11:13:13'):
    """Device-format lines as a serial capture prints them: host time, kernel time and caller on every line."""
    out, t = [], 0.0
    for i, l in enumerate(lines):
        kt = stab.ktime(l)
        t = kt if kt is not None else t
        body = l.split('] ', 1)[1] if kt is not None else l
        out.append(f'{host}.{i % 1000:03d} [{t:12.6f}][T{pid}@C{cpu}] {body}')
    return out


class Reports(unittest.TestCase):
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


class Best(unittest.TestCase):
    def test_copy_with_more_reports(self):
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


class Blocks(unittest.TestCase):
    A, B = '3-tee_idle/A1-idle-on', '3-tee_idle/B-idle-off'

    def device_log(self):
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


SH = shutil.which('dash') or shutil.which('sh')
WATCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'device', 'klogwatch.sh')


@unittest.skipUnless(SH and shutil.which('awk') and shutil.which('pkill'), 'needs sh, awk and pkill')
class KlogWatch(unittest.TestCase):
    """device/klogwatch.sh with a stand-in dmesg: the matching records with their continuation lines, nothing else;
    stopped through its pid file."""

    def test_filter_and_stop(self):
        log = (['[  900.000000] binder: noise', '  noise continuation'] + report(1000.3, 2, 243.235)
               + ['[ 1000.400000] other: line', 'other continuation', mark(1001.0, 'x', 'start')])
        want = report(1000.3, 2, 243.235) + [mark(1001.0, 'x', 'start')]
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'bin'))
            with open(os.path.join(d, 'log.txt'), 'w') as f:
                f.write('\n'.join(log) + '\n')
            stub = os.path.join(d, 'bin', 'dmesg')
            with open(stub, 'w') as f:
                f.write(f'#!/bin/sh\ncat {shlex.quote(os.path.join(d, "log.txt"))}\nexec sleep 5\n')
            os.chmod(stub, 0o755)
            out = os.path.join(d, 'k.txt')
            p = subprocess.Popen([SH, WATCH, out, 'irqsoff_warn|STAB TEE_IDLE'], start_new_session=True,
                                 env=dict(os.environ, PATH=os.path.join(d, 'bin') + os.pathsep + os.environ['PATH']))
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


class TeeTablet:
    """A scripted device: cpuidle disable files (CPU 0 state 2 disabled before the phase), the kernel log copy of
    klogwatch.sh, a TEE load that adds a Trusty IRQ-off report while every deep idle state is enabled, and a process
    table (pid: command line) for the stop commands (stab.stop_cmd)."""
    serial = 'fake'
    KLOG = f'{stab.DEV}/tmp/tee-idle-klog.txt'

    def __init__(self):
        self.idle = {f'/sys/devices/system/cpu/cpu{c}/cpuidle/state{s}/disable': '0' for c in range(8) for s in range(4)}
        self.idle['/sys/devices/system/cpu/cpu0/cpuidle/state2/disable'] = '1'
        self.original = dict(self.idle)
        self.klog, self.t, self.cmds, self.killed, self.fail = [], 100.0, [], [], None
        self.follows = True  # False: klogwatch.sh keeps no copy (e.g. a dmesg without -w)
        self.procs, self.watch_pids, self.readonly = {}, [], False  # readonly: cpuidle writes do not take

    @property
    def watch(self):
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
        return cmd.split('; done', 1)[1].lstrip('; ')

    def health(self, marker='', since=None, boot=None):
        return {'boot_id': BOOT, 'suspend_fail': '0', 'dmesg_last': '0', '_raw': 'x', '_sec': {}}

    def adb(self, *args, timeout=60):
        return 0, ''

    def sh(self, cmd, timeout=60):
        return self.su(cmd, timeout)

    def su(self, cmd, timeout=60):
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


class Phase(unittest.TestCase):
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
        self.r.ev.close(); self.r.logf.close()
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def events(self, *kinds):
        with open(os.path.join(self.tmp.name, 'events.jsonl')) as f:
            return [e for e in map(json.loads, f) if not kinds or e['kind'] in kinds]

    def test_blocks(self):
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
        self.assertEqual([e['phase'] for e in self.events('PHASE_START')], ['3-tee_idle/A1-idle-on', '3-tee_idle/B-idle-off'])
        self.assertEqual(self.r.phase, '3-tee_idle')
        stab.write_summary(self.tmp.name)
        with open(os.path.join(self.tmp.name, 'summary.md')) as f:
            text = f.read()
        self.assertIn('## TEE idle: Trusty IRQ-off reports >= 30 ms (>= 100 ms)', text)
        self.assertIn('| A1-idle-on | on | 5 | 120 / 0 | 1 (0) | n/a | 46 | +0xe4 x1 |', text)
        self.assertIn('| B-idle-off | off | 5 | 120 / 0 | 0 (0) | n/a | - | - |', text)

    def test_interrupt_restores_deep_idle(self):
        calls = []

        def wait(s, t=''):
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
        self.dev.follows = False
        self.r.wait = lambda s, t='': None
        self.r.phase_tee_idle(self.PLAN)
        self.assertEqual([e['msg'][:41] for e in self.events('SKIP')],
                         ["A1-idle-on: the block's markers are in no", "B-idle-off: the block's markers are in no"])
        self.assertEqual(self.events('TEE_IRQSOFF'), [])
        self.assertIn('device log n/a (no block markers in this copy)', self.events('INFO')[0]['msg'])

    def test_no_cpuidle(self):
        self.dev.idle = {}
        self.r.phase_tee_idle({})
        self.assertEqual([e['kind'] for e in self.events()], ['SKIP'])


if __name__ == '__main__':
    unittest.main()
