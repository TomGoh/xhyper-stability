#!/usr/bin/env python3
"""Tests of the kernel-line grading in host/stab.py on synthetic lines in the device's and the capture's formats.

  python3 -m unittest discover -s tests -v       (from the repository root)
"""
# Besides the grading rules themselves (stab.kernel_grade, ConsoleHist), the tests drive stab.Run's health snapshots
# on a fake device (FakeDev) for suspend aborts, probe timeouts, phase attribution and grouping, run compare.py on the
# events written, run device/health.sh's kernel section under a local sh, and run the standby phase on a fake clock.
#
# Structure:
#   Fixtures                      kernel lines in the device's format, serial capture lines, histograms
#   Rule 1: console-bound         ConsoleBound
#   Rule 3: cross-CPU waits       CrossCallWait
#   Fake device and run harness   FakeDev, H, RunCase
#   Grading inside snapshots      SnapshotGrading
#   Rule 2: suspend aborts        SuspendAbort
#   Rule 4: probe timeouts        ProbeTimeout
#   Rule 5: phase attribution     Attribution, PhaseMarkers
#   M4: grouping                  Grouping (also compare.py)
#   M2: device/health.sh          HealthScript
#   Small fixes                   SmallFixes
#   Rule 6: standby               Standby
import contextlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

# the modules under test live in the repository's host/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'host'))
import compare  # noqa: E402
import stab  # noqa: E402

# ==== Fixtures ========================================================================================================

# two boot ids
BOOT, BOOT2 = '0b0e1d2c-1111-4222-8333-944455556666', '7a7b7c7d-1111-4222-8333-944455556666'
# preempt/IRQ-off reports as the device's kernel log prints them: [end time] <type>_warn: C<cpu> T:<pid>task D:<length>
# F:<start time>; TRUSTY: an 820 ms irqsoff on CPU 2, TAIL: a preempt-off on CPU 5 inside it
TRUSTY = '[20552.230000] irqsoff_warn: C2 T:<32600>kworker/u16:0 D:820.000ms F:20551.410000s'  # ends 20552.230
TAIL = '[20552.240000] preemptoff_warn: C5 T:<5300>tail D:812.000ms F:20551.428000s'
YLOG = '[ 3895.850000] preemptoff_warn: C2 T:<600>ylogkat D:129.000ms F:3895.721000s'
SLEEP = '[18764.070000] preemptoff_warn: C3 T:<8100>stress-ng-sleep D:593.000ms F:18763.477000s E:154.000 ms'


def serial_lines(t0, t1, nbytes, caller='T633@C2'):
    """Capture lines ('HH:MM:SS.mmm [ktime][caller] text') stamped evenly in [t0, t1) carrying nbytes on the wire."""
    text = ' sprd-wlan: ' + 'x' * 90
    # per: bytes of one line on the wire (without the host time, plus CR LF)
    per = len(f'[{t0:12.6f}][{caller}]{text}') + 2
    n = max(1, round(nbytes / per))
    return ''.join(f'22:47:12.{i % 1000:03d} [{t0 + (t1 - t0) * i / n:12.6f}][{caller}]{text}\n'
                   for i in range(n)).encode()


def hist_of(*chunks):
    """A stab.ConsoleHist fed the given capture chunks in order."""
    h = stab.ConsoleHist()
    for c in chunks:
        h.feed(c)
    return h


# ==== Rule 1: console-bound ===========================================================================================

class ConsoleBound(unittest.TestCase):
    """Rule 1: a long preempt/IRQ-off while the console printed at line rate is a warning, with bytes and share.

    Guards stab.kernel_grade with a ConsoleHist, stab.console_load and ConsoleHist itself. Fixture: synthetic capture
    bursts (serial_lines) of a chosen size and time span around a report. Asserts the grade (KERNEL_WARN when the
    bytes stamped in [F - 0.2 s, F + D] reach half the line's capacity in D, else KERNEL_BAD), the reason text, and
    the histogram's byte counts across new boots, pruning, partial lines and lines without a kernel time."""

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
        """4000 bytes in the window grade BAD, 5300 WARN; console_load counts them within 120 bytes."""
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

    def test_reset_on_new_boot(self):  # kernel time far behind the newest one: a new boot, the old bytes are gone
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


# ==== Rule 3: cross-CPU waits =========================================================================================

class CrossCallWait(unittest.TestCase):
    """Rule 3: a preempt-off inside another CPU's irqsoff waited for it; the irqsoff is the finding.

    Guards stab.kernel_grade with irqs (stab.irq_intervals) and its context-line handling. Fixture: the TRUSTY
    irqsoff (CPU 2, 20551.410 to 20552.230) and preempt-off reports placed inside, around and outside it. Asserts the
    grade: KERNEL_WARN with 'waits on C2 irqsoff ...' for a preempt-off of another CPU that starts at most 5 ms before
    and ends at most 30 ms after the irqsoff, KERNEL_BAD otherwise and for the irqsoff itself, (None, '') for context
    lines."""

    # (cpu, start, end, D ms, task) of the TRUSTY irqsoff
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
        """Near the tolerances: starting 3 ms before the irqsoff or ending 25.9 ms after it still waits on it; ending
        35.9 ms after it does not."""
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
        """Report continuations and wake lines are context, (None, ''); a short report with a space in the task is
        still a report (KERNEL_WARN)."""
        for line in ('[20552.231000] irqsoff_warn: C2 disabled IRQ at:', '[  219.500000] Wakeup pending. Abort CPU',
                     '[  220.400000] Resume caused by misconfigured IRQ 134 26100000.pcie wakeup'):
            self.assertEqual(stab.kernel_grade(line), (None, ''), line)
        self.assertEqual(stab.kernel_grade('[ 10.0] irqsoff_warn: C1 T:<5>a b D:43.000ms F:9.900000s')[0],
                         'KERNEL_WARN')


# ==== Fake device and run harness =====================================================================================

class FakeDev:
    """The device interface stab.Run uses, scripted: health() returns the given snapshots in order (and records the
    since and boot it was asked for), wait_alive() the given answers, su() a fixed output, sh() the boot id when asked
    for it; everything else succeeds at once."""
    serial = 'fake'

    def __init__(self, healths=(), alive=(), boot=BOOT, su_out=''):
        """healths: snapshots in order; None = a probe that timed out, 'rc1' = one that failed at once."""
        self.healths, self.alive, self.boot, self.su_out = list(healths), list(alive), boot, su_out
        # fail: the reason of the last failed probe, as Run reads it; args: (since, boot) of every health() call
        self.fail, self.args = None, []

    def health(self, marker='', since=None, boot=None):
        """The next scripted snapshot; None for a timeout (also once they run out) or a failure, with self.fail set."""
        self.args.append((since, boot))
        h = self.healths.pop(0) if self.healths else None
        self.fail = None if isinstance(h, dict) else 'timeout' if h is None else 'rc=1: su: not found'
        return h if isinstance(h, dict) else None

    def su(self, cmd, timeout=60):
        """Succeeds with the fixed output su_out."""
        return 0, self.su_out

    def sh(self, cmd, timeout=60):
        """The boot id for a command that reads boot_id, else no output."""
        return 0, self.boot + '\n' if 'boot_id' in cmd else ''

    def adb(self, *args, timeout=60):
        """Succeeds with no output."""
        return 0, ''

    def wait_alive(self, timeout):
        """The next scripted answer; True once they run out."""
        return self.alive.pop(0) if self.alive else True

    def wait_boot(self, timeout):
        """Booted at once."""
        return True


def H(boot=BOOT, kernel=(), **kv):
    """A health snapshot as stab.parse_health returns it: boot id, no suspend failures, dmesg_last 0, the kernel_bad
    section's lines `kernel`, and any other key=value (values as strings, as parsed)."""
    h = {'boot_id': boot, 'suspend_fail': '0', 'dmesg_last': '0', '_raw': 'x', '_sec': {'kernel_bad': list(kernel)}}
    h.update({k: str(v) for k, v in kv.items()})
    return h


class RunCase(unittest.TestCase):
    """Base of the tests that drive a stab.Run: a temporary run directory, stdout silenced, and the Runs made by
    run_of() closed at the end; events() reads back events.jsonl."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.runs = []

    def tearDown(self):
        for r in self.runs:
            r.ev.close()
            r.logf.close()
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def reset(self):
        """A fresh run directory within one test (for a loop over cases)."""
        self.tearDown()
        self.setUp()

    def run_of(self, *healths, phase='p', **kw):
        """A stab.Run in phase `phase` on a FakeDev that answers the given health snapshots (kw: FakeDev's)."""
        r = stab.Run(FakeDev(healths, **kw), self.tmp.name, {})
        r.phase = phase
        self.runs.append(r)
        return r

    def events(self, *kinds):
        """The events written so far, only those of the given kinds when any are given."""
        with open(os.path.join(self.tmp.name, 'events.jsonl')) as f:
            evs = [json.loads(l) for l in f]
        return [e for e in evs if not kinds or e['kind'] in kinds]


# ==== Grading inside snapshots ========================================================================================

class SnapshotGrading(RunCase):
    """Rules 1 and 3 inside Run.snapshot: histogram brought up to date, irqsoff intervals collected first.

    Fixture: a Run on a FakeDev whose snapshots carry TRUSTY, TAIL or YLOG in their kernel section, with a serial
    capture file for rule 1. Asserts the kinds of the KERNEL_BAD / KERNEL_WARN events written."""

    def test_waiter_before_its_irqsoff_in_one_snapshot(self):
        r = self.run_of(H(), H(kernel=[TAIL, TRUSTY]))
        r.snapshot('baseline')
        r.snapshot()
        kinds = {e['msg'][:60]: e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')}
        self.assertEqual(kinds, {TAIL[:60]: 'KERNEL_WARN', TRUSTY[:60]: 'KERNEL_BAD'})
        self.assertIn('[waits on C2 irqsoff 820 ms (kworker/u16:0)]', self.events('KERNEL_WARN')[0]['msg'])

    def test_irqsoff_kept_for_two_snapshots(self):
        """A waiter two snapshots after its irqsoff is still matched (WARN), three snapshots after it is not (BAD)."""
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


# ==== Rule 2: suspend aborts ==========================================================================================

# the wakeup_reason kv of a suspend aborted by the PCIe controller's wake interrupt: IRQ 127 under the hypervisor,
# 134 on stock
PCIE127 = ('[  9477.100000] Wakeup pending. Abort CPU freeze | '
           '[  9477.900000] Resume caused by misconfigured IRQ 127 26100000.pcie wakeup')
PCIE134 = ('[  219.500000] Wakeup pending. Abort CPU freeze | '
           '[  220.400000] Resume caused by misconfigured IRQ 134 26100000.pcie wakeup')


class SuspendAbort(RunCase):
    """Rule 2: a suspend aborted by a named wake interrupt is SUSPEND_ABORTED; anything else stays SUSPEND_FAIL.

    Guards Run.snapshot's suspend-failure check and stab.wake_info. Fixture (kind()): two snapshots, the second with
    one more suspend failure and the given wakeup_reason, failed device step counter and last_failed_dev. Asserts the
    single SUSPEND_ABORTED or SUSPEND_FAIL event and its message, and wake_info's (abort device, wake device)."""

    def kind(self, reason, last=100.0, dev='', rose=False, was=''):
        """rose: a device step counter rose (the device in last_failed_dev failed now); was: last_failed_dev before."""
        self.reset()
        r = self.run_of(H(dmesg_last=last, suspend_failed_suspend=2, suspend_last_failed_dev=was),
                        H(suspend_fail=1, suspend_failed_suspend=3 if rose else 2, suspend_last_failed_dev=dev,
                          wakeup_reason=reason))
        r.snapshot()
        r.snapshot('suspend1')
        evs = self.events('SUSPEND_ABORTED', 'SUSPEND_FAIL')
        self.assertEqual(len(evs), 1)
        return evs[0]['kind'], evs[0]['msg']

    def test_pcie_wake_under_hypervisor_and_stock(self):
        for reason in (PCIE127, PCIE134):
            k, msg = self.kind(reason)
            self.assertEqual(k, 'SUSPEND_ABORTED')
            self.assertIn('wake interrupt pending from 26100000.pcie', msg)

    def test_other_cases_stay_fail(self):
        """No reason, a resume line without an abort, an abort older than the previous snapshot, a device step that
        failed: SUSPEND_FAIL."""
        resume_only = '[  220.400000] Resume caused by IRQ 104, 64400000.spi:pmic@0:rtc@200'
        for reason, last, dev, rose in (('', 100.0, '', False), (resume_only, 100.0, '', False),
                                        (PCIE134, 300.0, '', False), (PCIE134, 100.0, 'mmc0', True)):
            self.assertEqual(self.kind(reason, last, dev, rose)[0], 'SUSPEND_FAIL', (reason, last, dev))

    def test_alarmtimer(self):  # the alarmtimer device failed its suspend step: an alarm was due
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
        r.snapshot()
        r.snapshot('suspend1')
        self.assertEqual([e['kind'] for e in self.events('SUSPEND_ABORTED', 'SUSPEND_FAIL')], ['SUSPEND_ABORTED'])
        self.assertIn('from 26100000.pcie', self.events('SUSPEND_ABORTED')[0]['msg'])


# ==== Rule 4: probe timeouts ==========================================================================================

class ProbeTimeout(RunCase):
    """Rule 4: no health answer but adb back within 60 s on the same boot is a probe timeout, not a lost device.

    Guards Run.snapshot's handling of a health probe that times out (None) or fails at once ('rc1'). Fixture: FakeDev
    snapshot scripts with the same or a new boot and wait_alive answers. Asserts the sequence of event kinds
    (PROBE_TIMEOUT, DEVICE_LOST, UNEXPECTED_REBOOT, DEVICE_GONE), the Abort, and the since / boot passed to the
    probe."""

    def test_same_boot_is_probe_timeout(self):
        r = self.run_of(H(), None, H())
        r.snapshot('baseline')
        self.assertEqual(r.snapshot('x')['boot_id'], BOOT)
        kinds = [e['kind'] for e in self.events() if e['kind'] != 'PHASE_START']
        self.assertEqual(kinds, ['INFO', 'PROBE_TIMEOUT'])
        self.assertRegex(self.events('PROBE_TIMEOUT')[0]['msg'], r'back after \d+ s, same boot')

    def test_new_boot_is_device_lost(self):
        r = self.run_of(H(), None, H(boot=BOOT2), H(boot=BOOT2), boot=BOOT2)
        r.snapshot('baseline')
        r.snapshot('x')
        kinds = [e['kind'] for e in self.events()]
        self.assertIn('DEVICE_LOST', kinds)
        self.assertIn('UNEXPECTED_REBOOT', kinds)
        self.assertNotIn('PROBE_TIMEOUT', kinds)

    def test_not_back_in_60_s_is_device_lost(self):
        r = self.run_of(H(), None, H(), H(), alive=[False, True])
        r.snapshot('baseline')
        r.snapshot('x')
        self.assertEqual([e['kind'] for e in self.events('DEVICE_LOST', 'PROBE_TIMEOUT')], ['DEVICE_LOST'])

    def gone(self, *healths, **kw):
        """Runs a baseline and one snapshot that must end in Abort; returns (the event kinds other than INFO, the
        last DEVICE_GONE message)."""
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
        r.snapshot('baseline')
        r.snapshot()
        self.assertEqual(r.dev.args, [(None, None), ('123.5', BOOT)])


# ==== Rule 5: phase attribution =======================================================================================

class Attribution(RunCase):
    """Rule 5: a kernel line belongs to the phase whose PHASE_START marker is the latest one before its start.

    Guards stab.attribute, Run.mark_phase (the marker's kernel time read back from the device) and the phase of the
    kernel events Run.snapshot writes. Fixture: MARKS, (boot, kernel time, phase) markers over two boots; FakeDev su
    output with or without the marker line. Asserts the phase returned, the markers recorded, the events' phase and
    their '(collected in <phase>)' suffix."""

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
        r.snapshot('baseline')
        r.snapshot()
        evs = self.events('KERNEL_BAD')
        self.assertEqual([e['phase'] for e in evs], ['6-el2_stress/tee', '6-el2_stress/pmu'])
        self.assertTrue(evs[0]['msg'].endswith('(collected in 6-el2_stress/pmu)'))
        self.assertNotIn('collected in', evs[1]['msg'])


class PhaseMarkers(RunCase):
    """Rule 5: every run phase and every el2_stress step writes its marker, and the outer phase is marked again.

    Guards Run.execute and Run.phase_el2_stress with mark_phase, setup, snapshot and the waits replaced by stubs
    that record or do nothing. Asserts the phases mark_phase was called in, the PHASE_START events and the per-step
    output file of a repeated step."""

    def test_execute_marks_every_phase(self):
        r = self.run_of()
        calls = []
        # mark_phase records the phase it is called in; setup and dismiss_anr_dialogs do nothing
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
        r.phase_el2_stress({'steps': ['ipi', 'timer', 'ipi'], 'seconds': 1})
        steps = ['6-el2_stress/ipi', '6-el2_stress/timer', '6-el2_stress/ipi-r2']
        self.assertEqual(calls, steps + ['6-el2_stress'])
        self.assertEqual([e['phase'] for e in self.events('PHASE_START')], steps)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, 'el2stress-ipi-r2.txt')))


# ==== M4: grouping ====================================================================================================

def trusty(i, phase_f=1000.0):
    """Report i of a series of 300 ms irqsoff reports one second apart from kernel time phase_f, on rotating CPUs
    and kworker names (one group once digits are replaced by N)."""
    f = phase_f + i
    return f'[{f + 0.3:.6f}] irqsoff_warn: C{i % 8} T:<{100 + i}>kworker/u16:{i % 5} D:300.000ms F:{f:.6f}s'


class Grouping(RunCase):
    """M4: per phase, the first 3 reports of a group are written; the rest are one event per snapshot with a count.

    Guards Run.snapshot's grouping, stab.write_summary's counting and compare.load's kernel findings. Fixture: series
    of trusty() reports in one or two snapshots, phase markers splitting them, SKIP events. Asserts the events'
    count and group fields, the summary.md lines (verdict, event table, warnings per kind) and compare.py's keys and
    counts."""

    def test_first_three_then_counted(self):
        r = self.run_of(H(), H(kernel=[trusty(i) for i in range(10)]), H(kernel=[trusty(i) for i in range(10, 15)]))
        r.snapshot('baseline')
        r.snapshot()
        r.snapshot()
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
        r.snapshot('baseline')
        r.snapshot()
        got = [(e['phase'], e.get('count', 1)) for e in self.events('KERNEL_BAD')]
        self.assertEqual(sorted(got), sorted([('a', 1)] * 3 + [('b', 1)] * 3 + [('a', 2), ('b', 2)]))

    def test_warnings_listed_per_kind(self):
        """summary.md lists warnings per kind, at most 20 each, the rest counted."""
        r = self.run_of()
        for i in range(25):
            r.event('SKIP', f'skip {i}')
        r.event('PROBE_TIMEOUT', 'late one')
        stab.write_summary(self.tmp.name)
        with open(os.path.join(self.tmp.name, 'summary.md')) as f:
            text = f.read()
        self.assertIn('### SKIP (25)', text)
        self.assertIn('- ... 5 more events', text)
        self.assertIn('### PROBE_TIMEOUT (1)', text)
        self.assertNotIn('skip 20', text)

    def test_compare_groups_irqsoff(self):
        """compare.py adds the counts of grouped events and of plain report lines under one key per type and task."""
        r = self.run_of(H(), H(kernel=[trusty(i) for i in range(10)]))
        r.snapshot('baseline')
        r.snapshot()
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


# ==== M2: device/health.sh ============================================================================================

SH = shutil.which('sh')
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'device', 'health.sh')
# a report from before the previous snapshot (kernel time 140)
OLD = '[  140.000000] preemptoff_warn: C1 T:<9>old D:120.000ms F:139.880000s'
# newer lines: 0 a preempt-off report, 1-2 its '... at:' record, 3 an irqsoff report, 4 its record, 5-6 a suspend
# abort and its resume line, 7 a line health.sh does not keep
NEW = ['[  150.000000] preemptoff_warn: C1 T:<9>a D:120.000ms F:149.880000s',
       '[  150.000100] preemptoff_warn: C1 disabled preempt at:', '[  150.000100] trace_preempt_off+0x74/0x7c',
       '[  200.000000] irqsoff_warn: C2 T:<9>kworker/u16:0 D:300.000ms F:199.700000s',
       '[  200.000200] irqsoff_warn: C2 enabled IRQ at:', '[  205.000000] Wakeup pending. Abort CPU freeze',
       '[  206.000000] Resume caused by misconfigured IRQ 127 26100000.pcie wakeup', '[  210.000000] binder: noise']


@unittest.skipUnless(SH, 'needs a POSIX sh')
class HealthScript(unittest.TestCase):
    """M2: device/health.sh's kernel section, run with sh on a synthetic kernel log (test hooks STAB_DMESG and
    STAB_BOOT_ID; the device-only commands in it fail harmlessly here).

    Asserts, through stab.parse_health of its output, the kernel_bad lines kept, kernel_new, dmesg_last, kernel_src
    (file, pipe or last200) and wakeup_reason, for the same boot, a new boot, no writable temporary directory, no awk
    and more lines than the 2000-line cap; and that its temporary files are removed."""

    def run_script(self, log, since='', boot='', tmpdir=None, path=None):
        """Runs health.sh with arguments ('', since, boot) on the kernel log lines `log`; returns its parsed output.
        tmpdir: its TMPDIR (default the test's own directory); path: its PATH."""
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
        """Another boot id, or none: the last 200 lines whatever since says, and no kernel_new."""
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
        """2100 new lines: kernel_new counts them all, the section keeps the newest 2000."""
        log = [f'[ {1000 + i}.000000] irqsoff_warn: C1 T:<9>w D:5.000ms F:{999 + i}.990000s' for i in range(2100)]
        h = self.run_script(log, since='1000.000000', boot=BOOT)
        self.assertEqual((h['kernel_new'], len(h['_sec']['kernel_bad'])), ('2100', 2000))
        self.assertEqual(h['_sec']['kernel_bad'][-1], log[-1])


# ==== Small fixes =====================================================================================================

class SmallFixes(RunCase):
    """Smaller rules of the health snapshot and the console histogram, one test each: HEALTH_DEGRADED once per boot,
    reports held until the capture has their bytes (and graded at a reboot, held only once), the WARN splat window
    across snapshots, KERNEL_LOG_CAPPED, ConsoleHist at kernel time 0 and with a long partial line, an unreadable
    capture, malformed numbers, and the grade of a watchdog resume line. Fixture: FakeDev snapshot scripts and
    capture files (capture()). Asserts events, held reports and histogram byte counts."""

    def test_degraded_probe_warned_once_per_boot(self):
        """A snapshot without a usable copy of the kernel log (kernel_src not file, or an empty dmesg_last) warns once
        per boot."""
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
        r.snapshot('baseline')
        r.snapshot()
        self.assertEqual(r.held, [YLOG])  # its report's own time is not in the capture yet
        r.expect_reboot = True
        r.snapshot()
        self.assertEqual([e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')], ['KERNEL_WARN'])

    def test_splat_window_across_snapshots(self):
        """A preempt-off inside a WARN splat whose end line comes a snapshot later is still graded as inside it."""
        warn = '[  500.000000] WARNING: CPU: 2 PID: 9 at drivers/x.c:10 f+0x1/0x2'
        end = '[  500.900000] ---[ end trace 0000000000000000 ]---'
        off = '[  500.600000] preemptoff_warn: C2 T:<9>x D:300.000ms F:500.300000s'
        r = self.run_of(H(), H(kernel=[warn]), H(kernel=[end, off]))
        r.snapshot('baseline')
        r.snapshot()
        r.snapshot()
        self.assertIn('[inside a WARN splat printout]', self.events('KERNEL_WARN')[-1]['msg'])

    def test_cap_hit_is_a_warning(self):
        r = self.run_of(H(), H(kernel_new=2100))
        r.snapshot('baseline')
        r.snapshot()
        self.assertEqual(len(self.events('KERNEL_LOG_CAPPED')), 1)

    def test_hist_reset_at_time_zero(self):  # a boot that died 60 s in: the next one is never 100 s "behind"
        h = hist_of(serial_lines(59.0, 60.0, 5000), b'21:00:00.000 [    0.000000][    T0] Booting Linux\n',
                    serial_lines(59.0, 60.0, 3000))
        self.assertLess(h.bytes_in(58.9, 60.1), 3100)

    def test_hist_long_partial_line(self):  # no line end for 70000 bytes: counted as a line, not kept waiting
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
        """Appends the chunks to the run's serial.txt; returns its path."""
        cap = os.path.join(self.tmp.name, 'serial.txt')
        with open(cap, 'ab') as f:
            for c in chunks:
                f.write(c)
        return cap

    def test_report_waits_for_its_console_bytes(self):
        r = self.run_of(H(), H(kernel=[YLOG]), H(), H())
        r.serial = self.capture(serial_lines(3895.0, 3895.6, 2000))
        r.snapshot('baseline')
        r.snapshot()
        self.assertEqual(self.events('KERNEL_BAD', 'KERNEL_WARN'), [])  # capture behind F + D: held
        self.capture(serial_lines(3895.6, 3895.86, 12000))
        r.snapshot()
        self.assertEqual([e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')], ['KERNEL_WARN'])

    def test_held_once_only(self):  # the capture never catches up: graded at the next snapshot without its bytes
        r = self.run_of(H(), H(kernel=[YLOG]), H())
        r.serial = self.capture(serial_lines(3895.0, 3895.6, 2000))
        r.snapshot('baseline')
        r.snapshot()
        r.snapshot()
        self.assertEqual([e['kind'] for e in self.events('KERNEL_BAD', 'KERNEL_WARN')], ['KERNEL_BAD'])


# ==== Rule 6: standby =================================================================================================

# the RTC's wake source name on T11206
RTC = '64400000.spi:pmic@0:rtc@200'


class Standby(RunCase):
    """Rule 6: standby runs for its hours, records each wake source and flags sleeps cut short.

    Guards stab.standby_summary, Run.suspend_once (time.sleep patched out) and Run.phase_standby (suspend_once and
    screen stubbed, time on a fake clock). Asserts the summary texts, the INFO line of a cycle, the number of cycles
    run and the STANDBY_SHORT events."""

    def test_summary_short(self):
        info, short = stab.standby_summary([(12, RTC), (9, RTC), (17, RTC), (11, '26100000.pcie')], 1500)
        self.assertIn(f'4 cycles of 1500 s requested; adb back after median 12 s, max 17 s; wake reasons {RTC} x3',
                      info)
        self.assertRegex(short, rf'^woke after a median 12 s of the requested 1500 s \({RTC}\): long standby not '
                                'measured')

    def test_summary_long_or_never(self):
        """A median of at least half the request is not short; adb never back reads 'never'."""
        self.assertIsNone(stab.standby_summary([(1512, RTC), (1530, RTC)], 1500)[1])
        info, short = stab.standby_summary([(None, 'unknown')], 1500)
        self.assertIsNone(short)
        self.assertIn('never', info)

    def test_wake_reason_in_cycle_line(self):
        rtc = f'[  120.000000] Resume caused by IRQ 104, {RTC}'
        r = self.run_of(H(dmesg_last=100), H(suspend_success=1, wakeup_reason=rtc), su_out='AUDIO 0 0')
        r.dev.wait_drop = lambda timeout: True
        with mock.patch.object(stab.time, 'sleep'):
            h, back, why = r.suspend_once(20, 1)
        self.assertEqual(why, RTC)
        self.assertRegex(self.events('INFO')[-1]['msg'], rf'^suspend 1: success \+1 fail \+0, adb back after \d+ s, '
                                                         rf'woke by {RTC}$')

    def test_first_resume_and_refused_suspend(self):
        """The first resume after the pre-suspend snapshot is ours (a later one can be an automatic suspend's); a
        suspend that never dropped adb has no sleep length."""
        sec = [f'[  120.000000] Resume caused by IRQ 104, {RTC}',
               '[  150.000000] Resume caused by misconfigured IRQ 127 26100000.pcie wakeup']
        r = self.run_of(H(dmesg_last=100), H(kernel=sec, suspend_success=1), su_out='AUDIO 0 0')
        r.dev.wait_drop = lambda timeout: False
        with mock.patch.object(stab.time, 'sleep'):
            h, back, why = r.suspend_once(20, 1)
        self.assertEqual((back, why), (None, RTC))
        self.assertIn('adb never dropped, woke by', self.events('INFO')[-1]['msg'])
        self.assertIn('median 12 s', stab.standby_summary([(12, RTC), (None, RTC), (12, RTC)], 1500)[0])

    def run_standby(self, back):
        """Runs phase_standby for 0.05 h (180 s) with every cycle taking back + 30 s of a fake clock (adb back after
        `back` s, then 30 s awake); returns the names of the cycles run."""
        clock = [1000.0]
        r = self.run_of()
        calls = []

        def once(s, i):
            """Stands in for Run.suspend_once: records the cycle, advances the clock, wakes by the RTC."""
            calls.append(i)
            clock[0] += back + 30
            return {}, back, RTC
        r.suspend_once, r.screen = once, lambda on: None
        with mock.patch.object(stab.time, 'time', lambda: clock[0]), mock.patch.object(stab.time, 'sleep'):
            r.phase_standby({'hours': 0.05, 'suspend_s': 1500, 'awake_s': 30})  # 180 s
        return calls

    def test_loops_for_the_hours(self):
        self.assertEqual(len(self.run_standby(12)), 5)  # 42 s a cycle: starts at 0, 42, 84, 126, 168 of 180 s
        self.assertEqual(len(self.events('STANDBY_SHORT')), 1)
        self.reset()
        self.assertEqual(self.run_standby(1510), ['standby1'])
        self.assertEqual(self.events('STANDBY_SHORT'), [])
        self.assertIn('1 cycles of 1500 s requested', self.events('INFO')[-1]['msg'])


if __name__ == '__main__':
    unittest.main()
