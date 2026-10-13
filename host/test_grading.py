#!/usr/bin/env python3
"""Tests of the kernel-line grading in host/stab.py on synthetic lines in the device's and the capture's formats.

  python3 -m unittest discover -s host -p 'test_*.py' -v
"""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare  # noqa: E402
import stab  # noqa: E402

BOOT, BOOT2 = '0b0e1d2c-1111-4222-8333-944455556666', '7a7b7c7d-1111-4222-8333-944455556666'
TRUSTY = '[20552.230000] irqsoff_warn: C2 T:<32600>kworker/u16:0 D:820.000ms F:20551.410000s'  # ends 20552.230
TAIL = '[20552.240000] preemptoff_warn: C5 T:<5300>tail D:812.000ms F:20551.428000s'
YLOG = '[ 3895.850000] preemptoff_warn: C2 T:<600>ylogkat D:129.000ms F:3895.721000s'
SLEEP = '[18764.070000] preemptoff_warn: C3 T:<8100>stress-ng-sleep D:593.000ms F:18763.477000s E:154.000 ms'


def serial_lines(t0, t1, nbytes, caller='T633@C2'):
    """Capture lines ('HH:MM:SS.mmm [ktime][caller] text') stamped evenly in [t0, t1) carrying nbytes on the wire."""
    text = ' sprd-wlan: ' + 'x' * 90
    per = len(f'[{t0:12.6f}][{caller}]{text}') + 2
    n = max(1, round(nbytes / per))
    return ''.join(f'22:47:12.{i % 1000:03d} [{t0 + (t1 - t0) * i / n:12.6f}][{caller}]{text}\n'
                   for i in range(n)).encode()


def hist_of(*chunks):
    h = stab.ConsoleHist()
    for c in chunks:
        h.feed(c)
    return h


class ConsoleBound(unittest.TestCase):
    """Rule 1: a long preempt/IRQ-off while the console printed at line rate is a warning, with bytes and share."""

    def test_line_rate_burst_is_warn(self):
        k, why = stab.kernel_grade(YLOG, hist=hist_of(serial_lines(3895.50, 3895.86, 14000)))
        self.assertEqual(k, 'KERNEL_WARN')
        self.assertRegex(why, r'console-bound: \d+\.\d KiB .* \d+% of the line rate')

    def test_slow_printing_is_bad(self):  # register dump at ~4.4 ms a read: slow, not line rate
        self.assertEqual(stab.kernel_grade(SLEEP, hist=hist_of(serial_lines(18763.27, 18764.07, 2000)))[0],
                         'KERNEL_BAD')

    def test_no_capture_is_bad(self):
        self.assertEqual(stab.kernel_grade(YLOG)[0], 'KERNEL_BAD')

    def test_half_line_rate_threshold(self):
        line = '[ 1000.600000] preemptoff_warn: C1 T:<9>writer D:100.000ms F:1000.500000s'
        for nbytes, want in ((4000, 'KERNEL_BAD'), (5300, 'KERNEL_WARN')):  # capacity in D: 9216 bytes
            h = hist_of(serial_lines(1000.32, 1000.58, nbytes))
            n, share = stab.console_load(h, 1000.5, 100.0)
            self.assertAlmostEqual(share, n / 9216.0)
            self.assertLess(abs(n - nbytes), 120)
            self.assertEqual(stab.kernel_grade(line, hist=h)[0], want, (nbytes, n, share))

    def test_irqsoff_console_bound_is_warn(self):
        line = '[24430.850000] irqsoff_warn: C5 T:<26100>hackbench D:106.000ms F:24430.744000s E:11.000ms'
        self.assertEqual(stab.kernel_grade(line, hist=hist_of(serial_lines(24430.73, 24430.95, 24000)))[0],
                         'KERNEL_WARN')

    def test_bytes_outside_window_do_not_count(self):
        h = hist_of(serial_lines(3894.0, 3895.4, 60000), serial_lines(3895.9, 3896.5, 60000))
        self.assertEqual(stab.kernel_grade(YLOG, hist=h)[0], 'KERNEL_BAD')

    def test_reset_on_new_boot(self):
        h = hist_of(serial_lines(5000.0, 5000.5, 20000), serial_lines(10.0, 10.5, 3000))
        self.assertEqual(h.bytes_in(4999, 5001), 0)
        self.assertGreater(h.bytes_in(9.9, 10.6), 2500)

    def test_pruned_after_two_hours(self):
        h = hist_of(serial_lines(100.0, 100.5, 5000), serial_lines(7400.0, 7400.5, 5000))
        self.assertEqual(h.bytes_in(99, 101), 0)
        self.assertGreater(h.bytes_in(7399, 7401), 4500)

    def test_partial_line_waits_for_the_rest(self):
        b = serial_lines(50.0, 50.05, 300)
        h = hist_of(b[:40])
        self.assertEqual(h.bytes_in(0, 100), 0)
        h.feed(b[40:])
        self.assertAlmostEqual(h.bytes_in(0, 100), len(b) - 12 * b.count(b'\n'), delta=1)  # minus host time, plus CR LF

    def test_unstamped_line_counts_at_last_time(self):
        h = hist_of(serial_lines(60.0, 60.01, 100) + b'22:47:13.000 ' + b'y' * 498 + b'\n')
        self.assertGreater(h.bytes_in(59.9, 60.1), 500)


class CrossCallWait(unittest.TestCase):
    """Rule 3: a preempt-off inside another CPU's irqsoff waited for it; the irqsoff is the finding."""
    IRQS = stab.irq_intervals([TRUSTY])

    def test_waiter_names_the_irqsoff(self):
        self.assertEqual(stab.kernel_grade(TAIL, irqs=self.IRQS),
                         ('KERNEL_WARN', 'waits on C2 irqsoff 820 ms (kworker/u16:0)'))

    def test_irqsoff_itself_stays_bad(self):
        quiet = hist_of(serial_lines(20551.2, 20552.3, 3000))
        self.assertEqual(stab.kernel_grade(TRUSTY, irqs=self.IRQS, hist=quiet)[0], 'KERNEL_BAD')

    def test_same_cpu_is_bad(self):
        same = '[20552.240000] preemptoff_warn: C2 T:<5300>tail D:812.000ms F:20551.428000s'
        self.assertEqual(stab.kernel_grade(same, irqs=self.IRQS)[0], 'KERNEL_BAD')

    def test_not_inside_is_bad(self):
        early = '[20552.200000] preemptoff_warn: C5 T:<5300>tail D:812.000ms F:20551.384000s'  # starts 26 ms before
        late = '[20552.300000] preemptoff_warn: C5 T:<5300>tail D:812.000ms F:20551.480000s'   # ends 62 ms after
        apart = '[20560.300000] preemptoff_warn: C5 T:<5300>tail D:150.000ms F:20560.150000s'
        for line in (early, late, apart):
            self.assertEqual(stab.kernel_grade(line, irqs=self.IRQS)[0], 'KERNEL_BAD', line)

    def test_edges(self):
        ok = '[20552.240000] preemptoff_warn: C5 T:<5300>tail D:832.900ms F:20551.407000s'  # -3 ms, +9.9 ms
        self.assertEqual(stab.kernel_grade(ok, irqs=self.IRQS)[0], 'KERNEL_WARN')
        # the irqsoff CPU prints its own report before it takes the IPI: overnight waiters ended up to 24.4 ms after
        lag = '[20552.260000] preemptoff_warn: C4 T:<7800>tail D:825.900ms F:20551.430000s'  # +25.9 ms
        over = '[20552.270000] preemptoff_warn: C4 T:<7800>tail D:835.900ms F:20551.430000s'  # +35.9 ms
        self.assertEqual(stab.kernel_grade(lag, irqs=self.IRQS)[0], 'KERNEL_WARN')
        self.assertEqual(stab.kernel_grade(over, irqs=self.IRQS)[0], 'KERNEL_BAD')

    def test_irqsoff_does_not_wait(self):
        other = '[20552.240000] irqsoff_warn: C5 T:<5300>tail D:812.000ms F:20551.428000s'
        self.assertEqual(stab.kernel_grade(other, irqs=self.IRQS)[0], 'KERNEL_BAD')

    def test_context_lines(self):
        for line in ('[20552.231000] irqsoff_warn: C2 disabled IRQ at:', '[  219.500000] Wakeup pending. Abort CPU',
                     '[  220.400000] Resume caused by misconfigured IRQ 134 26100000.pcie wakeup'):
            self.assertEqual(stab.kernel_grade(line), (None, ''), line)
        self.assertEqual(stab.kernel_grade('[ 10.0] irqsoff_warn: C1 T:<5>a b D:43.000ms F:9.900000s')[0],
                         'KERNEL_WARN')


class FakeDev:
    serial = 'fake'

    def __init__(self, healths=(), alive=(), boot=BOOT, su_out=''):
        """healths: snapshots in order; None = a probe that timed out, 'rc1' = one that failed at once."""
        self.healths, self.alive, self.boot, self.su_out = list(healths), list(alive), boot, su_out
        self.fail, self.args = None, []

    def health(self, marker='', since=None, boot=None):
        self.args.append((since, boot))
        h = self.healths.pop(0) if self.healths else None
        self.fail = None if isinstance(h, dict) else 'timeout' if h is None else 'rc=1: su: not found'
        return h if isinstance(h, dict) else None

    def su(self, cmd, timeout=60):
        return 0, self.su_out

    def sh(self, cmd, timeout=60):
        return 0, self.boot + '\n' if 'boot_id' in cmd else ''

    def adb(self, *args, timeout=60):
        return 0, ''

    def wait_alive(self, timeout):
        return self.alive.pop(0) if self.alive else True

    def wait_boot(self, timeout):
        return True


def H(boot=BOOT, kernel=(), **kv):
    h = {'boot_id': boot, 'suspend_fail': '0', 'dmesg_last': '0', '_raw': 'x', '_sec': {'kernel_bad': list(kernel)}}
    h.update({k: str(v) for k, v in kv.items()})
    return h


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.runs = []

    def tearDown(self):
        for r in self.runs:
            r.ev.close(); r.logf.close()
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def reset(self):
        self.tearDown(); self.setUp()

    def run_of(self, *healths, phase='p', **kw):
        r = stab.Run(FakeDev(healths, **kw), self.tmp.name, {})
        r.phase = phase
        self.runs.append(r)
        return r

    def events(self, *kinds):
        with open(os.path.join(self.tmp.name, 'events.jsonl')) as f:
            evs = [json.loads(l) for l in f]
        return [e for e in evs if not kinds or e['kind'] in kinds]


class SnapshotGrading(RunCase):
    """Rules 1 and 3 inside Run.snapshot: histogram brought up to date, irqsoff intervals collected first."""

    def test_waiter_before_its_irqsoff_in_one_snapshot(self):
        r = self.run_of(H(), H(kernel=[TAIL, TRUSTY]))
        r.snapshot('baseline'); r.snapshot()
        kinds = {e['msg'][:60]: e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')}
        self.assertEqual(kinds, {TAIL[:60]: 'KERNEL_WARN', TRUSTY[:60]: 'KERNEL_BAD'})
        self.assertIn('[waits on C2 irqsoff 820 ms (kworker/u16:0)]', self.events('KERNEL_WARN')[0]['msg'])

    def test_irqsoff_kept_for_two_snapshots(self):
        for gap, want in ((2, 'KERNEL_WARN'), (3, 'KERNEL_BAD')):
            self.reset()
            r = self.run_of(H(), H(kernel=[TRUSTY]), *[H() for _ in range(gap - 1)], H(kernel=[TAIL]))
            for _ in range(gap + 2):
                r.snapshot()
            self.assertEqual(self.events('KERNEL_BAD', 'KERNEL_WARN')[-1]['kind'], want, gap)

    def test_histogram_updated_before_grading(self):
        cap = os.path.join(self.tmp.name, 'serial.txt')
        open(cap, 'wb').close()
        r = self.run_of(H(), H(kernel=[YLOG]))
        r.serial = cap
        r.snapshot('baseline')
        with open(cap, 'ab') as f:  # the burst reaches the capture after the baseline, before the next snapshot
            f.write(serial_lines(3895.50, 3895.86, 14000))
        r.snapshot()
        self.assertEqual([e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')], ['KERNEL_WARN'])


PCIE127 = ('[  9477.100000] Wakeup pending. Abort CPU freeze | '
           '[  9477.900000] Resume caused by misconfigured IRQ 127 26100000.pcie wakeup')
PCIE134 = ('[  219.500000] Wakeup pending. Abort CPU freeze | '
           '[  220.400000] Resume caused by misconfigured IRQ 134 26100000.pcie wakeup')


class SuspendAbort(RunCase):
    """Rule 2: a suspend aborted by a named wake interrupt is SUSPEND_ABORTED; anything else stays SUSPEND_FAIL."""

    def kind(self, reason, last=100.0, dev='', rose=False, was=''):
        """rose: a device step counter rose (the device in last_failed_dev failed now); was: last_failed_dev before."""
        self.reset()
        r = self.run_of(H(dmesg_last=last, suspend_failed_suspend=2, suspend_last_failed_dev=was),
                        H(suspend_fail=1, suspend_failed_suspend=3 if rose else 2, suspend_last_failed_dev=dev,
                          wakeup_reason=reason))
        r.snapshot(); r.snapshot('suspend1')
        evs = self.events('SUSPEND_ABORTED', 'SUSPEND_FAIL')
        self.assertEqual(len(evs), 1)
        return evs[0]['kind'], evs[0]['msg']

    def test_pcie_wake_under_hypervisor_and_stock(self):
        for reason in (PCIE127, PCIE134):
            k, msg = self.kind(reason)
            self.assertEqual(k, 'SUSPEND_ABORTED')
            self.assertIn('wake interrupt pending from 26100000.pcie', msg)

    def test_other_cases_stay_fail(self):
        resume_only = '[  220.400000] Resume caused by IRQ 104, 64400000.spi:pmic@0:rtc@200'
        for reason, last, dev, rose in (('', 100.0, '', False), (resume_only, 100.0, '', False),
                                        (PCIE134, 300.0, '', False), (PCIE134, 100.0, 'mmc0', True)):
            self.assertEqual(self.kind(reason, last, dev, rose)[0], 'SUSPEND_FAIL', (reason, last, dev))

    def test_alarmtimer(self):
        k, msg = self.kind('', dev='alarmtimer.0.auto', rose=True)
        self.assertEqual((k, 'alarm due' in msg), ('SUSPEND_ABORTED', True))

    def test_last_failed_dev_is_sticky(self):  # the kernel keeps the last device that failed for the whole boot
        stale = 'alarmtimer.0.auto'
        k, msg = self.kind(PCIE127, dev=stale, was=stale)
        self.assertEqual(k, 'SUSPEND_ABORTED')
        self.assertIn('wake interrupt pending from 26100000.pcie', msg)
        self.assertEqual(self.kind('', dev=stale, was=stale)[0], 'SUSPEND_FAIL')  # e.g. freezing of tasks failed

    def test_wake_info(self):  # (abort device, wake device)
        rtc = '64400000.spi:pmic@0:rtc@200'
        self.assertEqual(stab.wake_info(PCIE134), ('26100000.pcie', '26100000.pcie'))
        self.assertEqual(stab.wake_info(f'[24504.100000] Resume caused by IRQ 104, {rtc}'), (None, rtc))
        self.assertEqual(stab.wake_info(PCIE134, since=230.0), (None, None))
        self.assertEqual(stab.wake_info(f'last_resume_reason 104 {rtc} '), (None, rtc))

    def test_abort_paired_in_kernel_order(self):
        """An auto-suspend's RTC resume before the abort is not its cause; the kv's newest three lines can lose the
        'Wakeup pending' line to other 'Abort' lines: the kernel_bad section has them all, in order."""
        sec = [f'[  210.000000] Resume caused by IRQ 104, 64400000.spi:pmic@0:rtc@200', *PCIE134.split(' | '),
               '[  221.000000] Resume caused by IRQ 104, 64400000.spi:pmic@0:rtc@200']
        self.assertEqual(stab.wake_info(sec, since=100.0), ('26100000.pcie', '64400000.spi:pmic@0:rtc@200'))
        kv = ' | '.join(f'[  22{i}.500000] Abort: thing {i}' for i in range(3))
        r = self.run_of(H(dmesg_last=100), H(kernel=sec, suspend_fail=1, wakeup_reason=kv))
        r.snapshot(); r.snapshot('suspend1')
        self.assertEqual([e['kind'] for e in self.events('SUSPEND_ABORTED', 'SUSPEND_FAIL')], ['SUSPEND_ABORTED'])
        self.assertIn('from 26100000.pcie', self.events('SUSPEND_ABORTED')[0]['msg'])


class ProbeTimeout(RunCase):
    """Rule 4: no health answer but adb back within 60 s on the same boot is a probe timeout, not a lost device."""

    def test_same_boot_is_probe_timeout(self):
        r = self.run_of(H(), None, H())
        r.snapshot('baseline')
        self.assertEqual(r.snapshot('x')['boot_id'], BOOT)
        kinds = [e['kind'] for e in self.events() if e['kind'] != 'PHASE_START']
        self.assertEqual(kinds, ['INFO', 'PROBE_TIMEOUT'])
        self.assertRegex(self.events('PROBE_TIMEOUT')[0]['msg'], r'back after \d+ s, same boot')

    def test_new_boot_is_device_lost(self):
        r = self.run_of(H(), None, H(boot=BOOT2), H(boot=BOOT2), boot=BOOT2)
        r.snapshot('baseline'); r.snapshot('x')
        kinds = [e['kind'] for e in self.events()]
        self.assertIn('DEVICE_LOST', kinds); self.assertIn('UNEXPECTED_REBOOT', kinds)
        self.assertNotIn('PROBE_TIMEOUT', kinds)

    def test_not_back_in_60_s_is_device_lost(self):
        r = self.run_of(H(), None, H(), H(), alive=[False, True])
        r.snapshot('baseline'); r.snapshot('x')
        self.assertEqual([e['kind'] for e in self.events('DEVICE_LOST', 'PROBE_TIMEOUT')], ['DEVICE_LOST'])

    def gone(self, *healths, **kw):
        r = self.run_of(H(), *healths, **kw)
        r.snapshot('baseline')
        with self.assertRaises(stab.Abort):
            r.snapshot('x')
        return [e['kind'] for e in self.events() if e['kind'] not in ('INFO',)], self.events('DEVICE_GONE')[-1]['msg']

    def test_probe_keeps_timing_out(self):  # live device, same boot: one retry, then an error, not a silent PASS
        kinds, msg = self.gone(None, None)
        self.assertEqual(kinds, ['PROBE_TIMEOUT', 'PROBE_TIMEOUT', 'DEVICE_GONE'])
        self.assertIn('health probe fails on a live device', msg)

    def test_probe_fails_at_once(self):  # su broken, rc=1: not a timeout; the old lost-device path ends it
        kinds, msg = self.gone('rc1', 'rc1')
        self.assertEqual(kinds, ['DEVICE_LOST', 'DEVICE_GONE'])
        self.assertIn('health probe fails (rc=1', msg)

    def test_one_timeout_after_a_long_gap_retries(self):  # e.g. the first probe after a 1500 s standby
        r = self.run_of(H(), None, H(dmesg_last=5))
        r.snapshot('baseline')
        r.good_t -= r.lost_timeout + 1000
        self.assertEqual(r.snapshot('x')['dmesg_last'], '5')
        self.assertEqual([e['kind'] for e in self.events('PROBE_TIMEOUT', 'DEVICE_GONE', 'DEVICE_LOST')],
                         ['PROBE_TIMEOUT'])

    def test_since_and_boot_passed_to_the_probe(self):
        r = self.run_of(H(dmesg_last=123.5), H())
        r.snapshot('baseline'); r.snapshot()
        self.assertEqual(r.dev.args, [(None, None), ('123.5', BOOT)])


class Attribution(RunCase):
    """Rule 5: a kernel line belongs to the phase whose PHASE_START marker is the latest one before its start."""
    MARKS = [(BOOT, 20300.0, '6-el2_stress/tee'), (BOOT, 20700.0, '6-el2_stress/pmu'), (BOOT2, 5.0, '7-standby')]

    def test_step_boundary(self):
        self.assertEqual(stab.attribute(self.MARKS, BOOT, 20551.43), '6-el2_stress/tee')
        self.assertEqual(stab.attribute(self.MARKS, BOOT, 20712.1), '6-el2_stress/pmu')
        self.assertIsNone(stab.attribute(self.MARKS, BOOT, 100.0))

    def test_reboot_restarts_kernel_time(self):
        self.assertEqual(stab.attribute(self.MARKS, BOOT2, 20551.43), '7-standby')
        self.assertIsNone(stab.attribute(self.MARKS, BOOT2, 3.0))
        self.assertIsNone(stab.attribute(self.MARKS, 'other-boot', 20551.43))

    def test_mark_phase_reads_back_kernel_time(self):
        r = self.run_of(phase='6-el2_stress/tee', su_out=f'{BOOT}\n[20300.123456] STAB PHASE_START 6-el2_stress/tee\n')
        r.mark_phase()
        self.assertEqual(r.marks, [(BOOT, 20300.123456, '6-el2_stress/tee')])

    def test_marker_not_read_back(self):  # M1: lines after the last snapshot go to the collecting phase, not to tee
        tee = '[20699.000000] preemptoff_warn: C7 T:<10300>tail D:228.000ms F:20698.700000s'
        pmu = '[20731.900000] preemptoff_warn: C6 T:<10800>tail D:135.000ms F:20731.760000s'
        r = self.run_of(H(dmesg_last=20700.0), H(kernel=[tee, pmu]), phase='6-el2_stress/pmu', su_out='TIMEOUT')
        r.marks = [(BOOT, 20300.0, '6-el2_stress/tee')]
        r.snapshot('baseline')
        r.mark_phase()
        self.assertEqual(r.marks[-1], (BOOT, 20700.0, None))
        r.snapshot()
        evs = self.events('KERNEL_BAD')
        self.assertEqual([e['phase'] for e in evs], ['6-el2_stress/tee', '6-el2_stress/pmu'])
        self.assertIn('(collected in 6-el2_stress/pmu)', evs[0]['msg'])
        self.assertNotIn('collected in', evs[1]['msg'])

    def test_event_goes_to_the_step_it_happened_in(self):
        late_tee = '[20712.300000] preemptoff_warn: C7 T:<10300>tail D:228.000ms F:20699.100000s'
        in_pmu = '[20731.900000] preemptoff_warn: C6 T:<10800>tail D:135.000ms F:20731.760000s'
        r = self.run_of(H(), H(kernel=[late_tee, in_pmu]), phase='6-el2_stress/pmu')
        r.marks = list(self.MARKS)
        r.snapshot('baseline'); r.snapshot()
        evs = self.events('KERNEL_BAD')
        self.assertEqual([e['phase'] for e in evs], ['6-el2_stress/tee', '6-el2_stress/pmu'])
        self.assertTrue(evs[0]['msg'].endswith('(collected in 6-el2_stress/pmu)'))
        self.assertNotIn('collected in', evs[1]['msg'])


class PhaseMarkers(RunCase):
    """Rule 5: every run phase and every el2_stress step writes its marker, and the outer phase is marked again."""

    def test_execute_marks_every_phase(self):
        r = self.run_of()
        calls = []
        r.mark_phase, r.setup, r.dismiss_anr_dialogs = lambda: calls.append(r.phase), lambda sha: None, lambda t: None
        r.snapshot, r.phase_noop = lambda tag='': H(), lambda p: None
        r.plan = {'phases': [{'type': 'noop'}, {'type': 'noop'}]}
        r.execute('')
        self.assertEqual(calls, ['1-noop', '2-noop'])

    def test_el2_stress_marks_each_step(self):
        r = self.run_of(phase='6-el2_stress', su_out='EL2STRESS_DONE ipi\nEL2STRESS_DONE timer\n')
        calls = []
        r.mark_phase, r.dismiss_anr_dialogs = lambda: calls.append(r.phase), lambda t: None
        r.wait = lambda s, t='': None
        r.phase_el2_stress({'steps': ['ipi', 'timer'], 'seconds': 1})
        self.assertEqual(calls, ['6-el2_stress/ipi', '6-el2_stress/timer', '6-el2_stress'])
        self.assertEqual([e['phase'] for e in self.events('PHASE_START')], ['6-el2_stress/ipi', '6-el2_stress/timer'])


def trusty(i, phase_f=1000.0):
    f = phase_f + i
    return f'[{f + 0.3:.6f}] irqsoff_warn: C{i % 8} T:<{100 + i}>kworker/u16:{i % 5} D:300.000ms F:{f:.6f}s'


class Grouping(RunCase):
    """M4: per phase, the first 3 reports of a group are written; the rest are one event per snapshot with a count."""

    def test_first_three_then_counted(self):
        r = self.run_of(H(), H(kernel=[trusty(i) for i in range(10)]), H(kernel=[trusty(i) for i in range(10, 15)]))
        r.snapshot('baseline'); r.snapshot(); r.snapshot()
        bad = self.events('KERNEL_BAD')
        self.assertEqual([e.get('count', 1) for e in bad], [1, 1, 1, 7, 5])
        self.assertTrue(bad[3]['msg'].startswith('7 more like this: irqsoff kworker/uN:N none, max 300.000 ms'))
        self.assertEqual({e['group'] for e in bad}, {'irqsoff kworker/uN:N none'})
        stab.write_summary(self.tmp.name)
        with open(os.path.join(self.tmp.name, 'summary.md')) as f:
            text = f.read()
        self.assertTrue(text.startswith('# Stability run') and ': FAIL' in text.splitlines()[0])
        self.assertIn('| KERNEL_BAD | 15 |', text)
        self.assertIn('errors 15', text)

    def test_groups_per_phase(self):
        r = self.run_of(H(), H(kernel=[trusty(i) for i in range(10)]))
        r.marks = [(BOOT, 0.0, 'a'), (BOOT, 1005.0, 'b')]
        r.snapshot('baseline'); r.snapshot()
        got = [(e['phase'], e.get('count', 1)) for e in self.events('KERNEL_BAD')]
        self.assertEqual(sorted(got), sorted([('a', 1)] * 3 + [('b', 1)] * 3 + [('a', 2), ('b', 2)]))

    def test_warnings_listed_per_kind(self):
        r = self.run_of()
        for i in range(25):
            r.event('SKIP', f'skip {i}')
        r.event('PROBE_TIMEOUT', 'late one')
        stab.write_summary(self.tmp.name)
        with open(os.path.join(self.tmp.name, 'summary.md')) as f:
            text = f.read()
        self.assertIn('### SKIP (25)', text); self.assertIn('- ... 5 more events', text)
        self.assertIn('### PROBE_TIMEOUT (1)', text); self.assertNotIn('skip 20', text)

    def test_compare_groups_irqsoff(self):
        r = self.run_of(H(), H(kernel=[trusty(i) for i in range(10)]))
        r.snapshot('baseline'); r.snapshot()
        r.event('KERNEL_BAD', '[ 9.300000] irqsoff_warn: C1 T:<77>kworker/u16:7 D:250.000ms F:9.000000s')  # older run
        r.event('KERNEL_WARN', '[ 9.300000] irqsoff_warn: C1 T:<77>kworker/u16:7 D:40.000ms F:9.000000s')
        r.event('KERNEL_WARN', '[ 9.4] WARNING: oddity (collected in 2-soak)')
        k = compare.load(self.tmp.name)['kernel']
        self.assertEqual(k['KERNEL_BAD irq-off >=100ms kworker/uN:N'], 11)
        self.assertEqual(k['KERNEL_WARN WARNING: oddity'], 1)
        r.event('KERNEL_BAD', '5 more like this: preemptoff Signal Catcher none, max 150.000 ms at F=9.000000s',
                group='preemptoff Signal Catcher none', count=5)
        self.assertEqual(compare.load(self.tmp.name)['kernel']['KERNEL_BAD preempt-off >=100ms Signal Catcher'], 5)
        self.assertEqual(sum(v for x, v in k.items() if x.startswith('KERNEL_WARN')), 1)


SH = shutil.which('sh')
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'device', 'health.sh')
OLD = '[  140.000000] preemptoff_warn: C1 T:<9>old D:120.000ms F:139.880000s'
NEW = ['[  150.000000] preemptoff_warn: C1 T:<9>a D:120.000ms F:149.880000s',
       '[  150.000100] preemptoff_warn: C1 disabled preempt at:', '[  150.000100] trace_preempt_off+0x74/0x7c',
       '[  200.000000] irqsoff_warn: C2 T:<9>kworker/u16:0 D:300.000ms F:199.700000s',
       '[  200.000200] irqsoff_warn: C2 enabled IRQ at:', '[  205.000000] Wakeup pending. Abort CPU freeze',
       '[  206.000000] Resume caused by misconfigured IRQ 127 26100000.pcie wakeup', '[  210.000000] binder: noise']


@unittest.skipUnless(SH, 'needs a POSIX sh')
class HealthScript(unittest.TestCase):
    """M2: device/health.sh's kernel section, run with sh on a synthetic kernel log (test hooks STAB_DMESG and
    STAB_BOOT_ID; the device-only commands in it fail harmlessly here)."""

    def run_script(self, log, since='', boot='', tmpdir=None, path=None):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'dmesg.txt')
            with open(f, 'w') as o:
                o.write(''.join(l + '\n' for l in log))
            env = dict(os.environ, STAB_DMESG=f, STAB_BOOT_ID=BOOT, TMPDIR=tmpdir or d, PATH=path or os.environ['PATH'])
            p = subprocess.run([SH, SCRIPT, '', since, boot], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(sorted(os.listdir(d)), ['dmesg.txt'])  # its temporary files are gone
        return stab.parse_health(p.stdout)

    def test_no_writable_tmp_uses_the_pipe(self):  # /data full or unwritable: the old pipe, the last 200 lines
        h = self.run_script([OLD] + NEW, since='150.000000', boot=BOOT, tmpdir='/nonexistent/stab-test')
        self.assertEqual((h['kernel_src'], h['dmesg_last']), ('pipe', '210.000000'))
        self.assertEqual(h['_sec']['kernel_bad'], [OLD, NEW[0], NEW[3], NEW[5], NEW[6]])

    def test_no_awk_keeps_the_last_200(self):
        with tempfile.TemporaryDirectory() as b:  # a PATH with the tools the script uses, but no awk
            for t in ('cat', 'cut', 'date', 'tr', 'sed', 'head', 'tail', 'grep', 'wc', 'ls', 'rm', 'stat', 'sort'):
                if shutil.which(t):
                    os.symlink(shutil.which(t), os.path.join(b, t))
            h = self.run_script([OLD] + NEW, since='150.000000', boot=BOOT, path=b)
        self.assertEqual(h['kernel_src'], 'last200')
        self.assertEqual(h['_sec']['kernel_bad'], [OLD, NEW[0], NEW[3], NEW[5], NEW[6]])

    def test_same_boot_only_newer_lines(self):
        h = self.run_script([OLD] + NEW, since='150.000000', boot=BOOT)
        self.assertEqual(h['_sec']['kernel_bad'], [NEW[0], NEW[3], NEW[5], NEW[6]])  # >= since, no '... at:' lines
        self.assertEqual((h['kernel_new'], h['dmesg_last'], h['boot_id'], h['kernel_src']),
                         ('4', '210.000000', BOOT, 'file'))
        self.assertEqual(h['wakeup_reason'], ' | '.join(NEW[5:7]))  # the kv: matching lines only

    def test_new_boot_last_200(self):
        log = [f'[ {100 + i}.000000] irqsoff_warn: C1 T:<9>w D:5.000ms F:{99 + i}.990000s' for i in range(250)]
        for boot in ('7a7b7c7d-0000-4000-8000-000000000000', ''):
            h = self.run_script(log, since='300.000000', boot=boot)
            self.assertEqual(h['_sec']['kernel_bad'], log[50:])
            self.assertNotIn('kernel_new', h)

    def test_dmesg_last_skips_continuation_lines(self):
        # A multi-line kernel record prints its later lines without a time stamp; when one ends the log, dmesg_last
        # must still be the newest stamped time (an empty one disabled the newer-than filter and the wake source's
        # lower bound in the 2026-10-14 smoke run).
        h = self.run_script([OLD] + NEW + ['  in loopcheck_send_pcie'], since='150.000000', boot=BOOT)
        self.assertEqual((h['dmesg_last'], h['kernel_src']), ('210.000000', 'file'))

    def test_cap_keeps_the_newest(self):
        log = [f'[ {1000 + i}.000000] irqsoff_warn: C1 T:<9>w D:5.000ms F:{999 + i}.990000s' for i in range(2100)]
        h = self.run_script(log, since='1000.000000', boot=BOOT)
        self.assertEqual((h['kernel_new'], len(h['_sec']['kernel_bad'])), ('2100', 2000))
        self.assertEqual(h['_sec']['kernel_bad'][-1], log[-1])


class SmallFixes(RunCase):
    def test_degraded_probe_warned_once_per_boot(self):
        r = self.run_of(H(), H(kernel_src='pipe'), H(kernel_src='pipe'), H(boot=BOOT2), H(boot=BOOT2, dmesg_last=''),
                        H(boot=BOOT2, kernel_src='file'))
        for _ in range(6):
            r.snapshot()
        msgs = [e['msg'] for e in self.events('HEALTH_DEGRADED')]
        self.assertEqual(len(msgs), 2)
        self.assertIn("kernel_src=pipe, dmesg_last='0'", msgs[0])
        self.assertIn("kernel_src=None, dmesg_last=''", msgs[1])

    def test_held_report_graded_on_reboot(self):
        r = self.run_of(H(), H(kernel=[YLOG]), H(boot=BOOT2))
        r.serial = self.capture(serial_lines(3895.0, 3895.6, 2000), serial_lines(3895.6, 3895.84, 12000))
        r.snapshot('baseline'); r.snapshot()
        self.assertEqual(r.held, [YLOG])  # its report's own time is not in the capture yet
        r.expect_reboot = True
        r.snapshot()
        self.assertEqual([e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')], ['KERNEL_WARN'])

    def test_splat_window_across_snapshots(self):
        warn = '[  500.000000] WARNING: CPU: 2 PID: 9 at drivers/x.c:10 f+0x1/0x2'
        end = '[  500.900000] ---[ end trace 0000000000000000 ]---'
        off = '[  500.600000] preemptoff_warn: C2 T:<9>x D:300.000ms F:500.300000s'
        r = self.run_of(H(), H(kernel=[warn]), H(kernel=[end, off]))
        r.snapshot('baseline'); r.snapshot(); r.snapshot()
        self.assertIn('[inside a WARN splat printout]', self.events('KERNEL_WARN')[-1]['msg'])

    def test_cap_hit_is_a_warning(self):
        r = self.run_of(H(), H(kernel_new=2100))
        r.snapshot('baseline'); r.snapshot()
        self.assertEqual(len(self.events('KERNEL_LOG_CAPPED')), 1)

    def test_hist_reset_at_time_zero(self):  # a boot that died 60 s in: the next one is never 100 s "behind"
        h = hist_of(serial_lines(59.0, 60.0, 5000), b'21:00:00.000 [    0.000000][    T0] Booting Linux\n',
                    serial_lines(59.0, 60.0, 3000))
        self.assertLess(h.bytes_in(58.9, 60.1), 3100)

    def test_hist_long_partial_line(self):
        h = hist_of(serial_lines(60.0, 60.01, 100), b'z' * 70000)
        self.assertEqual(h.part, b'')
        self.assertGreater(h.bytes_in(59.9, 60.1), 70000)

    def test_unreadable_capture(self):
        r = self.run_of()
        r.serial = os.path.join(self.tmp.name, 'gone', 'serial.txt')
        self.assertIsNone(r.console_update())

    def test_bad_numbers(self):
        self.assertIsNone(stab.ktime('[.] x'))
        self.assertEqual(stab.kernel_grade('[ 1.0] preemptoff_warn: C1 T:<1>a D:.ms F:.s'), (None, ''))

    def test_watchdog_resume_stays_a_warning(self):  # it matched the old 'watchdog' grep: KERNEL_WARN as before
        self.assertEqual(stab.kernel_grade('[ 5.0] Resume caused by IRQ 87, 1000.watchdog')[0], 'KERNEL_WARN')

    def capture(self, *chunks):
        cap = os.path.join(self.tmp.name, 'serial.txt')
        with open(cap, 'ab') as f:
            for c in chunks:
                f.write(c)
        return cap

    def test_report_waits_for_its_console_bytes(self):
        r = self.run_of(H(), H(kernel=[YLOG]), H(), H())
        r.serial = self.capture(serial_lines(3895.0, 3895.6, 2000))
        r.snapshot('baseline'); r.snapshot()
        self.assertEqual(self.events('KERNEL_BAD', 'KERNEL_WARN'), [])  # capture behind F + D: held
        self.capture(serial_lines(3895.6, 3895.86, 12000))
        r.snapshot()
        self.assertEqual([e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')], ['KERNEL_WARN'])

    def test_held_once_only(self):
        r = self.run_of(H(), H(kernel=[YLOG]), H())
        r.serial = self.capture(serial_lines(3895.0, 3895.6, 2000))
        r.snapshot('baseline'); r.snapshot(); r.snapshot()
        self.assertEqual([e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')], ['KERNEL_BAD'])


if __name__ == '__main__':
    unittest.main()
