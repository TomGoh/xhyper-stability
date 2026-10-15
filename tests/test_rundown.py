#!/usr/bin/env python3
"""Tests of the battery rundown (phase battery_rundown in host/stab.py, host/rundown_report.py) on synthetic samples
and a scripted fake device.

  python3 -m unittest discover -s tests -v       (from the repository root)
"""
# Structure:
#   Fixtures                      boot ids, the sample header, synthetic samples (sample, discharge, analyse_text)
#   Report                        Report: rundown_report's analysis, summary, comparison and run directory
#   Live view                     capture_line, Live: stab.RundownLive on serial capture lines
#   Sampler                       Sampler: device/rundown.sh on a fake sysfs
#   Checks                        Checks: the phase's pure helpers (state checks, decoder, restore commands)
#   Fake tablet                   Tablet: a scripted device for the whole phase
#   Phase                         Phase: Run.phase_battery_rundown end to end on a Tablet
import contextlib, io, json, os, shlex, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

# the modules under test live in the repository's host/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'host'))
import rundown_report as rr  # noqa: E402
import stab  # noqa: E402

# ==== Fixtures ========================================================================================================

# two boot ids: before the unplug and after the shutdown
BOOT, BOOT2 = '0b0e1d2c-1111-4222-8333-944455556666', '7a7b7c7d-1111-4222-8333-944455556666'
E = 1760000000  # synthetic epoch
# the sampler's header line (two cpufreq policies)
HEADER = ('epoch,uptime_s,cap,status,online,volt_uV,ocv_uV,cur_uA,cur_avg_uA,counter_uAh,temp_dC,backlight,player,'
          'f0_kHz,f4_kHz,vsync_hz,playback')
# the video file's sha256 (what the fake tablet answers for sha256sum)
SHA = '3aa2d8946ec7cf3b50b7148fb3338ca421e6ec34987b20a4044845d674794e3e'


def sample(i, plugged=False, **over):
    """Sample i of a synthetic discharge: 60 s apart, 1 % every 6 samples from 100 %, voltage falling linearly from 4.3
    to 3.4 V over 600 samples at 650 mA, refresh rate and playback state every 10th sample."""
    r = {'epoch': E + 60 * i, 'uptime_s': f'{1000 + 60 * i:.2f}', 'cap': 100 if plugged else 100 - i // 6,
         'status': 'Full' if plugged else 'Discharging', 'online': 1 if plugged else 0,
         'volt_uV': 4300000 if plugged else round(4300000 - 900000 * i / 600), 'ocv_uV': '',
         'cur_uA': -20000 if plugged else -650000, 'cur_avg_uA': '',
         'counter_uAh': 8000000 - (0 if plugged else 13000 * i),
         'temp_dC': 290 + i % 50, 'backlight': 128, 'player': 1, 'f0_kHz': 1800000, 'f4_kHz': 2000000,
         'vsync_hz': '90.00' if i % 10 == 0 else '', 'playback': 'PLAYING(3)' if i % 10 == 0 else ''}
    r.update(over)
    return ','.join(str(r[k]) for k in HEADER.split(','))


def discharge(n=600, edit=None):
    """CSV text: three samples on USB power (before the unplug, earlier epochs), then samples 0..n on battery; edit:
    {sample index: {column: value}} or a function on the list of sample lines."""
    lines = [sample(i - 3, plugged=True) for i in range(3)]
    lines += [sample(i, **(edit.get(i, {}) if isinstance(edit, dict) else {})) for i in range(n + 1)]
    if callable(edit):
        lines = edit(lines)
    return HEADER + '\n' + '\n'.join(lines) + '\n'


def analyse_text(text, **kw):
    """rundown_report.analyse of a sample file with this text (kw: analyse's settings)."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'r.csv')
        with open(p, 'w') as f:
            f.write(text)
        return rr.analyse(rr.load(p), **kw)


# ==== Report ==========================================================================================================

class Report(unittest.TestCase):
    """Guards host/rundown_report.py: analyse (duration, energy, power, charge, temperature, timeline, validity flags),
    summary, compare, load of the first sampler's layout and of a headerless live file, report_dir with the plan's
    parameters, result_of and main, and the rundown line stab.write_summary puts into summary.md. Fixture: discharge(),
    a clean 10 h synthetic discharge, edited per case. Asserts the result fields and the markdown lines."""

    def test_synthetic_discharge(self):
        """A clean discharge: no flags, the exact numbers, and the summary's lines."""
        res = analyse_text(discharge())
        self.assertEqual(res['flags'], {})
        self.assertEqual((res['duration_s'], res['start_cap'], res['end_cap']), (36000, 100, 0))
        self.assertAlmostEqual(res['energy_wh'], 0.65 * 3.85 * 10, places=3)  # linear voltage: mean 3.85 V for 10 h
        self.assertAlmostEqual(res['avg_w'], 0.65 * 3.85, places=3)
        self.assertAlmostEqual(res['median_w'], 0.65 * 3.85, places=3)
        self.assertAlmostEqual(res['charge_mah'], 7800)
        self.assertAlmostEqual(res['temp_max_c'], 33.9)
        tl = dict(res['timeline'])
        self.assertEqual((tl[90], tl[50], tl[10], tl[5]), (3600, 18000, 32400, 34200))
        text = rr.summary(res, 'r.csv')
        self.assertIn('- duration: **10 h 00 min** (36000 s, 601 samples, device clock)', text)
        self.assertIn('- energy: 25.02 Wh; power average 2.502 W', text)
        self.assertIn('| 90 % | 1 h 00 min |', text)
        self.assertIn('- validity: no flags', text)

    def test_each_flag(self):
        """Every flag raised alone, once, by one edit of the discharge; the details of vsync, gap and not_run_down."""
        cases = {
            'backlight': {100: {'backlight': 64}},
            'player_stopped': {200: {'player': 0}},
            'not_playing': {300: {'playback': 'PAUSED(2)'}},
            'vsync': {400: {'vsync_hz': '60.00'}},
            'power_back': {500: {'online': 1, 'status': 'Charging', 'cur_uA': 900000}},
            'gap': lambda ls: ls[:100] + ls[105:],  # samples 97-101 missing: 360 s from sample 96 to 102
            'reboot': {250: {'uptime_s': '12.00'}},
            'not_run_down': lambda ls: ls[:400],  # last sample 396, at 34 %
        }
        for name, edit in cases.items():
            res = analyse_text(discharge(edit=edit))
            self.assertEqual(set(res['flags']), {name}, name)
            self.assertEqual(res['flags'][name]['count'], 1, name)
        self.assertEqual(analyse_text(discharge(edit={400: {'vsync_hz': '60.00'}}))['flags']['vsync']['detail'],
                         '60.00 Hz')
        gap = analyse_text(discharge(edit=cases['gap']))['flags']['gap']
        self.assertEqual((gap['detail'], gap['first_s']), ('longest 360 s', 6120))
        self.assertEqual(analyse_text(discharge(edit=cases['not_run_down']))['flags']['not_run_down']['detail'],
                         'last sample 34 %')

    def test_positive_discharge_current(self):  # a fuel gauge (ACPI) that reports discharge as positive
        res = analyse_text(discharge(edit=lambda ls: [l.replace(',-650000,', ',650000,') for l in ls]))
        self.assertAlmostEqual(res['energy_wh'], 0.65 * 3.85 * 10, places=3)
        self.assertIn('positive', res['current_sign'])
        self.assertIn('- current: the fuel gauge reports discharge as a positive current', rr.summary(res))
        self.assertNotIn('current_sign', analyse_text(discharge()))

    def test_never_unplugged(self):
        res = analyse_text(HEADER + '\n' + '\n'.join(sample(i, plugged=True) for i in range(5)) + '\n')
        self.assertEqual(set(res['flags']), {'never_unplugged'})
        self.assertNotIn('duration_s', res)
        self.assertIn('FLAGS never_unplugged x5', rr.summary(res))

    def test_flag_thresholds(self):  # 3 x 60 s is not a gap yet; 0.5 Hz off is still the set rate; 5 % has run down
        res = analyse_text(discharge(n=570, edit=lambda ls: ls[:100] + ls[102:]))
        self.assertNotIn('gap', res['flags'])
        self.assertEqual(res['end_cap'], 5)
        self.assertNotIn('not_run_down', res['flags'])
        self.assertEqual(analyse_text(discharge(edit={10: {'vsync_hz': '89.60'}}))['flags'], {})

    def test_first_sampler_layout_and_live_file(self):
        """The 2026-10-14 sampler's columns (vlc, no playback) by header, and its live file (no header, host time
        first) after the device file's columns."""
        old = 'epoch,uptime_s,cap,status,online,volt_uV,ocv_uV,cur_uA,cur_avg_uA,counter_uAh,temp_dC,backlight,vlc,' \
              'f0_kHz,f4_kHz,f7_kHz,vsync_hz'
        # six samples, the first on USB power, the player gone in sample 3
        rows = [f'{E + 60 * i},{100 + 60 * i},{100 - i},Discharging,{0 if i else 1},4000000,4050000,-500000,-500000,'
                f'{8000000 - 9000 * i},300,128,{0 if i == 3 else 1},1,2,3,{"90.00" if i % 2 == 0 else ""}'
                for i in range(6)]
        with tempfile.TemporaryDirectory() as d:
            dev, live = os.path.join(d, 'dev.csv'), os.path.join(d, 'live.csv')
            with open(dev, 'w') as f:
                f.write(old + '\n' + '\n'.join(rows) + '\n')
            # the live file lacks sample 2 (lost on the serial console)
            with open(live, 'w') as f:
                f.write(''.join(f'2026-10-14 17:{30 + i}:0{i}.123,{r}\n' for i, r in enumerate(rows) if i != 2))
            res = rr.analyse(rr.load(dev), live=rr.load(live, ['host_time'] + old.split(',')))
            self.assertEqual(set(res['flags']), {'player_stopped', 'not_run_down'})
            self.assertEqual(res['duration_s'], 240)
            self.assertEqual(res['live'], {'samples': 5, 'missing': 1, 'start': '2026-10-14 17:31:01.123',
                                           'end': '2026-10-14 17:35:05.123', 'duration_s': 244.0})
            # without a device file: a headerless live file of the first layout by itself
            self.assertEqual(rr.analyse(rr.load(live))['duration_s'], 240)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rr.main([dev, '--live', live])
            self.assertIn('live view over the serial console: 5 samples, 1 of the run', out.getvalue())

    def test_compare(self):
        a = analyse_text(discharge())
        b = analyse_text(discharge(n=540, edit={100: {'backlight': 64}}))
        text = rr.compare([('stock', a), ('image', b)])
        self.assertIn('| item | stock | image |', text)
        self.assertIn('| duration | 10 h 00 min | 9 h 00 min (-10.0%) |', text)
        self.assertRegex(text, r'\| energy \(Wh\) \| 25\.02 \| 2\d\.\d\d \(-\d+\.\d%\) \|')
        self.assertIn('| validity flags | none | backlight x1, not_run_down x1 |', text)

    def test_report_dir_and_run_summary(self):
        """report_dir takes the brightness from the run's plan.json; summary.md gets the rundown line; result_of
        reads the run directory back."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'rundown-device.csv'), 'w') as f:
                f.write(discharge())
            with open(os.path.join(d, 'plan.json'), 'w') as f:
                json.dump({'phases': [{'type': 'battery_rundown', 'brightness': 64}]}, f)
            res = rr.report_dir(d)
            self.assertEqual(set(res['flags']), {'backlight'})  # the plan's value, not the default
            self.assertTrue(os.path.exists(os.path.join(d, 'rundown-summary.md')))
            open(os.path.join(d, 'events.jsonl'), 'w').close()
            with contextlib.redirect_stdout(io.StringIO()):
                stab.write_summary(d)
            with open(os.path.join(d, 'summary.md')) as f:
                self.assertIn('- battery rundown: rundown-summary.md (10 h 00 min on battery, 25.02 Wh, '
                              'average 2.502 W; validity: backlight x601)', f.read())
            self.assertEqual(rr.result_of(d)['duration_s'], 36000)


# ==== Live view =======================================================================================================

def capture_line(t, csv_line, ktime=5000.0):
    """A serial capture line carrying the sampler's kernel-log copy of csv_line, stamped with host time t."""
    return f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} [{ktime:12.6f}][T812@C3] " \
           f'RUNDOWN {csv_line}\n'


class Live(unittest.TestCase):
    """Guards stab.RundownLive, the live view of the sampler's RUNDOWN lines in a serial capture, and stab.stamp_time.
    Fixture: capture lines of 71 samples (one on USB power, then on battery) with another kernel line before them and
    a sample line cut short after them, fed in two reads that split a line. Asserts the counts of samples and bad
    lines, the INFO messages (first sample on battery, every 10 %, probably shut down after 5 min without a sample by
    this process's clock, samples again), the live file's header and host time column, and stamps before midnight."""

    def test_serial_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'live.csv')
            lv = stab.RundownLive(path, HEADER.split(','), 60)
            t = time.mktime((2026, 10, 14, 17, 30, 0, 0, 0, -1))
            chunk = ''.join(capture_line(t + 60 * i, sample(i, plugged=i < 0)) for i in range(-1, 70)).encode()
            chunk = b'17:29:00.000 [ 4999.000000][T1@C0] other kernel line\n' + chunk
            chunk += b'17:40:00.000 [ 5999.0][T812@C3] RUNDOWN 1760000000,10,9\n'  # cut short on the line: dropped
            msgs = lv.feed(chunk[:1000], t + 4200) + lv.feed(chunk[1000:], t + 4200)  # a line split between reads
            self.assertEqual((lv.n, lv.bad), (71, 1))
            self.assertTrue(msgs[0].startswith('rundown: first sample on battery at 17:30:00 (host clock): 100 %, '
                                               '4.300 V'))
            self.assertEqual(len(msgs), 2)
            self.assertRegex(msgs[1], r'^rundown: 90 % after 1\.00 h on battery, 4\.\d{3} V, 2\.\d\d W, 3\d\.\d C$')
            with open(path) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0], 'host_time,' + HEADER)
            self.assertEqual(lines[2].split(',')[0], time.strftime('%Y-%m-%d 17:30:00.000', time.localtime(t)))
            self.assertEqual(len(lines), 72)
            # quiet: by the time this process saw the last sample, not a file time
            self.assertEqual(lv.feed(b'', t + 4200 + 299), [])
            os.utime(path, (0, 0))
            off = lv.feed(b'', t + 4200 + 300)
            self.assertEqual(len(off), 1)
            self.assertIn('no sample for 5 min (last 89 %, 71 samples received): the device has probably shut down',
                          off[0])
            self.assertEqual(lv.feed(b'', t + 9000), [])  # once
            again = lv.feed(capture_line(t + 9000, sample(70)).encode(), t + 9000)
            self.assertTrue(again[0].startswith('rundown: samples again after a pause'))

    def test_stamp_before_midnight(self):
        now = time.mktime((2026, 10, 15, 0, 0, 30, 0, 0, -1))
        self.assertEqual(stab.stamp_time('23:59:50.500', now), now - 39.5)
        self.assertEqual(stab.stamp_time('00:00:10.000', now), now - 20)


# ==== Sampler =========================================================================================================

SH = shutil.which('dash') or shutil.which('sh')
SAMPLER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'device', 'rundown.sh')
# dumpsys media_session output: another app's session first, then the player's
MEDIA_DUMP = """  Sessions Stack - have 2 sessions:
    com.example.music/Music (userId=0)
      package=com.example.music
      state=PlaybackState {state=PAUSED(2), position=0}
    org.videolan.vlc/VLC/MediaSessionCompat (userId=0)
      package=org.videolan.vlc
      state=PlaybackState {state=PLAYING(3), position=12000}
"""


@unittest.skipUnless(SH and shutil.which('awk') and shutil.which('sed'), 'needs sh, awk and sed')
class Sampler(unittest.TestCase):
    """device/rundown.sh on a fake sysfs (paths rewritten), dumpsys and pidof stubbed: header, a sample line with the
    player's own media session state (another app's session comes first in the dump), stop by its pid file.

    Asserts the header line, the first sample line, its copy in the (fake) kernel log, and exit status 0 once the
    pid file is removed."""

    def test_sample_line(self):
        with tempfile.TemporaryDirectory() as d:
            # the fake sysfs and procfs under d, plus bin/ for the stubs and out/ for the sampler's files
            ps = os.path.join(d, 'sys/class/power_supply/battery')
            for sub in (ps, os.path.join(d, 'sys/class/backlight/bl0'), os.path.join(d, 'bin'),
                        os.path.join(d, 'out'),
                        os.path.join(d, 'sys/devices/system/cpu/cpufreq/policy0'), os.path.join(d, 'proc')):
                os.makedirs(sub)
            for k, v in (('capacity', 87), ('status', 'Discharging'), ('online', 0), ('voltage_now', 4100000),
                         ('current_now', -600000), ('charge_counter', 7000000), ('temp', 301)):
                with open(os.path.join(ps, k), 'w') as f:
                    f.write(f'{v}\n')
            for rel, v in (('sys/class/backlight/bl0/actual_brightness', 128), ('proc/uptime', '1234.56 999'),
                           ('sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq', 1800000)):
                with open(os.path.join(d, rel), 'w') as f:
                    f.write(f'{v}\n')
            with open(os.path.join(d, 'media.txt'), 'w') as f:
                f.write(MEDIA_DUMP)
            # stubs: dumpsys answers SurfaceFlinger and media_session, pidof finds the player
            for name, body in (('dumpsys',
                                f'case "$1" in SurfaceFlinger) echo "  vsyncRate=90.00 x";; media_session) '
                                f'cat {shlex.quote(os.path.join(d, "media.txt"))};; esac'),
                               ('pidof', 'echo 4242')):
                with open(os.path.join(d, 'bin', name), 'w') as f:
                    f.write(f'#!/bin/sh\n{body}\n')
                os.chmod(os.path.join(d, 'bin', name), 0o755)
            # the sampler with its absolute paths moved under d
            with open(SAMPLER) as f:
                text = (f.read().replace('/sys/', f'{d}/sys/').replace('/proc/uptime', f'{d}/proc/uptime')
                        .replace('/dev/kmsg', f'{d}/out/kmsg'))
            with open(os.path.join(d, 'rundown.sh'), 'w') as f:
                f.write(text)
            out = os.path.join(d, 'out', 'r.csv')
            p = subprocess.Popen([SH, os.path.join(d, 'rundown.sh'), out, '1'],
                                 stdout=subprocess.DEVNULL,
                                 env=dict(os.environ,
                                          PATH=os.path.join(d, 'bin') + os.pathsep + os.environ['PATH']))

            def lines():
                """Lines written to the sample file so far."""
                if not os.path.exists(out):
                    return 0
                with open(out) as f:
                    return f.read().count('\n')
            # wait up to 10 s for the header and one sample, then stop the sampler by its pid file
            end = time.time() + 10
            while time.time() < end and lines() < 2:
                time.sleep(0.05)
            os.remove(os.path.join(d, 'out', 'rundown.pid'))
            self.assertEqual(p.wait(timeout=10), 0)
            with open(out) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0], 'epoch,uptime_s,cap,status,online,volt_uV,ocv_uV,cur_uA,cur_avg_uA,'
                                       'counter_uAh,temp_dC,backlight,player,f0_kHz,vsync_hz,playback')
            self.assertRegex(lines[1], r'^\d+,1234\.56,87,Discharging,0,4100000,,-600000,,7000000,301,128,1,'
                                       r'1800000,90\.00,PLAYING\(3\)$')
            with open(os.path.join(d, 'out', 'kmsg')) as f:
                self.assertTrue(f.read().startswith('RUNDOWN ' + lines[1]))


# ==== Checks ==========================================================================================================

class Checks(unittest.TestCase):
    """Guards the pure helpers of the battery_rundown phase in host/stab.py: rundown_checks (the started rundown's
    state against the plan), hw_decoder, rundown_restore_cmds, same_setting, restore_shell and parse_volume.
    Fixture: GOOD, a state that passes every check, changed one field at a time. Asserts the reasons returned (one per
    changed field, naming it), the decoder found per process, and the restore command lists."""

    # the plan's parameters, the player's package, a state that passes every check
    P = {**rr.DEFAULTS}
    PKG = 'org.videolan.vlc'
    GOOD = {'playback': 'PLAYING', 'focus': 'mCurrentFocus=Window{1 u0 org.videolan.vlc/x.VideoPlayerActivity}',
            'pid': '4242', 'hw': (True, 'c2.vendor.avc.decoder'), 'backlight': '128', 'vsync': ['90.00'] * 3,
            'airplane': '1', 'wifi': 'Wifi is disabled', 'bluetooth': '0', 'volume': 3, 'sampler': True}

    def test_good_state(self):
        self.assertEqual(stab.rundown_checks(self.GOOD, self.P, self.PKG), [])

    def test_each_check(self):
        """One changed field gives one reason that names it; radios may stay on when the plan has airplane False."""
        for k, v, want in (('playback', 'PAUSED', 'one-time preparation'), ('focus', 'mCurrentFocus=launcher', 'focus'),
                           ('hw', (False, ''), 'hw-video-codec'), ('backlight', '255', 'backlight'),
                           ('vsync', ['90.00', '60.00', '90.00'], 'refresh rate'), ('vsync', ['', '', ''], 'refresh'),
                           ('airplane', '0', 'airplane'), ('wifi', 'Wifi is enabled', 'Wi-Fi'),
                           ('bluetooth', '1', 'Bluetooth'), ('volume', 7, 'volume'), ('sampler', False, 'sampler')):
            bad = stab.rundown_checks({**self.GOOD, k: v}, self.P, self.PKG)
            self.assertEqual(len(bad), 1, (k, v, bad))
            self.assertIn(want, bad[0])
        self.assertEqual(stab.rundown_checks({**self.GOOD, 'airplane': '0', 'wifi': 'on', 'bluetooth': '1'},
                                             {**self.P, 'airplane': False}, self.PKG), [])

    def test_hw_decoder(self):
        dump = ('ResourceManagerService:\n  Processes:\n    Pid: 1111\n      Client:\n'
                '        Name: c2.vendor.avc.decoder\n'
                '        Resources:\n          hw-video-codec/video-codec:1\n    Pid: 4242\n      Client:\n'
                '        Name: c2.android.avc.decoder\n        Resources:\n          sw-video-codec/video-codec:1\n')
        self.assertEqual(stab.hw_decoder(dump, '1111'), (True, 'c2.vendor.avc.decoder'))
        self.assertEqual(stab.hw_decoder(dump, '4242'), (False, 'c2.android.avc.decoder'))
        self.assertEqual(stab.hw_decoder(dump, '9'), (False, ''))
        self.assertEqual(stab.hw_decoder('Name: x\n hw-video-codec', '9')[0], True)  # no Pid sections: the whole dump

    def test_restore_commands(self):
        """The commands that bring back the settings read before the run, in order; radios by their on/off state;
        the hand-run command; the volume parsed back."""
        cmds = stab.rundown_restore_cmds({'global airplane_mode_on': '0', 'global wifi_on': '1',
                                          'global bluetooth_on': '0',
                                          'system peak_refresh_rate': 'null', 'system screen_brightness': '77',
                                          'system user_rotation': '', 'volume': 7, 'match_content': 1})
        self.assertEqual(cmds, ['cmd connectivity airplane-mode disable', 'sleep 2',
                                'cmd wifi set-wifi-enabled enabled',
                                'cmd bluetooth_manager disable', 'settings put system screen_brightness 77',
                                'settings delete system peak_refresh_rate',
                                'cmd media_session volume --stream 3 --set 7',
                                'cmd display set-match-content-frame-rate-pref 1'])
        # a device in airplane mode before the run: wifi_on 3 and bluetooth_on 2 are off (turned off by airplane mode),
        # wifi_on 2 is on in airplane mode
        air = {'global airplane_mode_on': '1', 'global wifi_on': '3', 'global bluetooth_on': '2'}
        self.assertEqual(stab.rundown_restore_cmds(air)[:4], ['cmd connectivity airplane-mode enable', 'sleep 2',
                                                              'cmd wifi set-wifi-enabled disabled',
                                                              'cmd bluetooth_manager disable'])
        self.assertIn('cmd wifi set-wifi-enabled enabled', stab.rundown_restore_cmds({**air, 'global wifi_on': '2'}))
        self.assertTrue(stab.same_setting('global wifi_on', '1', '2')
                        and not stab.same_setting('global wifi_on', '1', '3'))
        orig = {'system screen_brightness': "12'3", 'volume': 7}
        self.assertEqual(shlex.split(stab.restore_shell('S1', orig)),
                         ['adb', '-s', 'S1', 'shell', '; '.join(stab.rundown_restore_cmds(orig))])
        self.assertEqual(stab.parse_volume('[V] will control stream=3 (STREAM_MUSIC)\n'
                                           '[V] volume is 3 in range [0..15]'), 3)


# ==== Fake tablet =====================================================================================================

class Tablet:
    """A scripted device for the phase: settings, radios, volume, sampler, player, a boot id; away (off adb) from the
    unplug until `back_after` adb polls, then on a new boot. Commands joined with '; ' are run part by part."""
    serial = 'fake'
    PKG = 'org.videolan.vlc'

    def __init__(self, tmp, playing=True, back_after=4, capture=None, new_boot=True, csv=None):
        """playing: whether playback starts when the player starts; back_after: adb polls until the device is back;
        capture: a serial capture file that gets 10 sampler lines per poll while away; new_boot: back on a new boot
        (False: USB plugged in again before the shutdown); csv: the device's sample file (default discharge())."""
        # settings and their values before the run (restored() compares with these)
        self.settings = {'system screen_brightness_mode': '1', 'system screen_brightness': '77',
                         'system peak_refresh_rate': 'null', 'system min_refresh_rate': '60.0',
                         'system accelerometer_rotation': '1', 'system user_rotation': '1',
                         'global airplane_mode_on': '0', 'global wifi_on': '1', 'global bluetooth_on': '1'}
        # volume index and the match-content-frame-rate preference
        self.volume, self.match = 7, 1
        self.original = (dict(self.settings), 7, 1)
        # player, sampler: running or not; path: the sample file the sampler writes
        self.playing, self.player, self.sampler, self.path = playing, False, False, None
        # away: off adb since the unplug; polls: adb polls while away
        self.boot, self.away, self.polls, self.back_after = BOOT, False, 0, back_after
        # cmds: every shell command sent; drop: called by wait_drop, False = adb did not drop (still connected)
        self.capture, self.fail, self.cmds, self.drop = capture, None, [], lambda: True
        self.new_boot, self.csv = new_boot, csv if csv is not None else discharge()  # csv: the device's sample file
        self.hiccups, self.cat_hook = 0, None  # adb drops that are not the unplug; called when the file is read
        # the answer to the battery query: capacity and status
        self.bat = 'BAT 100 Full'

    def health(self, marker='', since=None, boot=None):
        """A minimal snapshot of the current boot; a probe while the device is away fails the test."""
        assert not self.away, 'health probe while the device is away'
        return {'boot_id': self.boot, 'suspend_fail': '0', 'dmesg_last': '10', '_raw': 'x', '_sec': {}}

    def alive(self):
        """One adb poll: while away, counts it, appends sampler lines to the capture and comes back (on a new boot
        unless new_boot is False) at poll back_after; returns whether the device answers."""
        if self.away:
            self.polls += 1
            if self.capture:  # the sampler's lines reach the serial capture while the device is away
                with open(self.capture, 'a') as f:
                    for i in range(self.polls * 10 - 10, self.polls * 10):
                        f.write(capture_line(time.time() - 30 + i % 10 * 3, sample(i)))
            if self.polls >= self.back_after:
                self.away = False
                if self.new_boot:  # USB came back after the shutdown: a new boot, nothing of the run is running
                    self.boot, self.player, self.sampler = BOOT2, False, False
        return not self.away

    def wait_drop(self, timeout):
        """Whether adb dropped (drop() decides; it may raise); a drop is the unplug unless a hiccup is left."""
        if not self.drop():
            return False
        if self.hiccups:  # adb did not answer once, the device stays connected
            self.hiccups -= 1
            return True
        self.away = True
        return True

    def wait_alive(self, timeout):
        """Whether the device is on adb now."""
        return not self.away

    def wait_boot(self, timeout):
        """Booted at once."""
        return True

    def adb(self, *args, timeout=60):
        """Succeeds with no output."""
        return 0, ''

    def su(self, cmd, timeout=60):
        """As sh()."""
        return self.sh(cmd, timeout)

    def sh(self, cmd, timeout=60):
        """Records cmd and runs its '; ' parts with one(); a command while the device is away fails the test."""
        assert not self.away, f'adb command while the device is away: {cmd}'
        self.cmds.append(cmd)
        return 0, '\n'.join(o for o in (self.one(c.strip()) for c in cmd.split('; ')) if o)

    def one(self, c):
        """The output of one shell command c, as the phase's commands expect it; commands that change a setting,
        a radio, the volume, the sampler or the player update the fake state; anything else gives no output."""
        s, w = self.settings, c.split()
        if c.startswith('settings get '):
            return s.get(c[13:], 'null')
        if c.startswith('settings put '):
            s[f'{w[2]} {w[3]}'] = w[4]
        elif c.startswith('settings delete '):
            s[f'{w[2]} {w[3]}'] = 'null'
        elif c.startswith('cmd connectivity airplane-mode '):
            s['global airplane_mode_on'] = '1' if w[-1] == 'enable' else '0'
        elif c.startswith('cmd wifi set-wifi-enabled '):
            s['global wifi_on'] = '1' if w[-1] == 'enabled' else '0'
        elif c.startswith('cmd wifi status'):
            return 'Wifi is ' + ('enabled' if s['global wifi_on'] == '1' else 'disabled')
        elif c.startswith('cmd bluetooth_manager '):
            s['global bluetooth_on'] = '1' if w[-1] == 'enable' else '0'
        elif c.startswith('cmd media_session volume'):
            if '--set' in w:
                self.volume = int(w[-1])
            return f'[V] will control stream=3 (STREAM_MUSIC)\n[V] volume is {self.volume} in range [0..15]'
        elif c.startswith('cmd display set-match-content-frame-rate-pref'):
            self.match = int(w[-1])
        elif c.startswith('cmd display get-match-content-frame-rate-pref'):
            return f'Match content frame rate type: {self.match}'
        elif c.startswith('sha256sum /sdcard/Movies/'):
            return f'{SHA}  {w[1]}'
        elif c.startswith('pm path'):
            return 'package:/data/app/vlc/base.apk'
        elif c.startswith('cat /proc/cmdline'):
            return 'console=ttyS1,921600n8 loglevel=8'
        elif c.startswith('dumpsys package'):
            return 'versionCode=13070106\nversionName=3.7.1'
        elif c.startswith('echo "BAT'):
            return self.bat
        elif 'rundown.sh' in c:
            # the sampler starts; its sample file is the third word after nohup
            self.sampler, self.path = True, w[w.index('nohup') + 3]
        elif 'rundown.pid' in c and 'rm -f' in c:
            self.sampler = False
        elif c.startswith('head -1'):
            return HEADER if self.sampler else ''
        elif c.startswith('kill -0'):
            return 'SAMPLER_UP' if self.sampler else ''
        elif c.startswith('am force-stop'):
            self.player = False
        elif c.startswith('am start'):
            self.player = True
            return 'Status: ok'
        elif c == 'dumpsys media_session':
            st = 'PLAYING(3)' if self.playing and self.player else 'PAUSED(2)'
            return f'  package={self.PKG}\n  state=PlaybackState {{state={st}, position=0'
        elif c.startswith('dumpsys window'):
            return f'mCurrentFocus=Window{{1 u0 {self.PKG}/x.VideoPlayerActivity}}' if self.player else ''
        elif c.startswith('pidof'):
            return '4242' if self.player else ''
        elif c == 'dumpsys media.resource_manager':
            return '    Pid: 4242\n      Client:\n        Name: c2.vendor.avc.decoder\n        hw-video-codec/x:1'
        elif 'actual_brightness' in c:
            return s['system screen_brightness']
        elif c.startswith('dumpsys SurfaceFlinger'):
            # 90 Hz only with the minimum refresh rate at 90 and match-content off
            return 'vsyncRate=' + ('90.00' if s['system min_refresh_rate'] == '90.0' and self.match == 0 else '60.00')
        elif c.startswith('getprop sys.boot_completed'):
            return '1'
        elif c.startswith('getprop sys.boot.reason.last'):
            return 'shutdown,battery'
        elif c.startswith('getprop persist.sys.boot.reason.history'):
            return 'shutdown,battery,1760036000'
        elif self.path and c == f'cat {self.path}':
            if self.cat_hook:
                self.cat_hook()
            return self.csv
        return ''

    def restored(self):
        """Whether settings, volume and match-content preference are back to their values before the run."""
        return (self.settings, self.volume, self.match) == self.original


# ==== Phase ===========================================================================================================

class Phase(unittest.TestCase):
    """Guards Run.phase_battery_rundown end to end on a Tablet: the full run, aborts and interrupts before the unplug,
    while away and after the return, USB back early, a restart before the battery ran down, a failing live view, a
    battery that does not get full and a USB cable that is not pulled. time.time and time.sleep run on a fake clock;
    STAB_APK_DIR and STAB_MEDIA_DIR are empty. Asserts the events (INFO messages, UNEXPECTED_REBOOT,
    RUNDOWN_INVALID, RESTORE_FAIL, SERIAL_STALE), the Abort messages, the files in the run directory, and that the
    tablet's settings are restored and its sampler and player stopped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        # the fake clock: time.sleep advances it
        self.clock = [time.mktime((2026, 10, 14, 17, 0, 0, 0, 0, -1))]
        self.patches = [mock.patch.object(stab.time, 'time', lambda: self.clock[0]),
                        mock.patch.object(stab.time, 'sleep', lambda s: self.clock.__setitem__(0, self.clock[0] + s)),
                        mock.patch.dict(os.environ, {'STAB_APK_DIR': '', 'STAB_MEDIA_DIR': ''})]
        for p in self.patches:
            p.start()
        self.run_ = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if self.run_:
            self.run_.ev.close()
            self.run_.logf.close()
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def start(self, dev, serial=None):
        """A stab.Run on dev in phase 1-battery_rundown, serial capture `serial`, after its baseline snapshot."""
        r = self.run_ = stab.Run(dev, self.tmp.name, {})
        r.serial = serial
        r.phase = '1-battery_rundown'
        r.snapshot('baseline')
        return r

    def events(self, *kinds):
        """The events written so far, only those of the given kinds when any are given."""
        with open(os.path.join(self.tmp.name, 'events.jsonl')) as f:
            return [e for e in map(json.loads, f) if not kinds or e['kind'] in kinds]

    def test_full_run(self):
        """Unplug, 12 polls away with the live view on the capture, back on a new boot: no lost-device, reboot,
        validity or stale-capture events, the expected INFO lines, settings restored, every file written, the run's
        settings applied once."""
        cap = os.path.join(self.tmp.name, 'serial.txt')
        open(cap, 'w').close()
        dev = Tablet(self.tmp.name, capture=cap, back_after=12)
        r = self.start(dev, cap)
        r.phase_battery_rundown({})
        kinds = {e['kind'] for e in self.events()}
        self.assertFalse(kinds & {'DEVICE_LOST', 'DEVICE_GONE', 'UNEXPECTED_REBOOT', 'PROBE_TIMEOUT',
                                  'RUNDOWN_INVALID', 'SERIAL_STALE'}, kinds)
        msgs = [e['msg'] for e in self.events('INFO')]
        self.assertTrue(any(m.startswith('UNPLUG USB NOW') for m in msgs))
        self.assertTrue(any(m.startswith('new boot after the rundown, as expected; boot reason (last, history): '
                                         'shutdown,battery') for m in msgs), msgs)
        self.assertTrue(any(m.startswith('rundown: 90 % after 1.00 h on battery') for m in msgs), msgs)
        self.assertTrue(any(m.startswith('rundown: 10 h 00 min on battery, 100 % to 0 %, 25.02 Wh') for m in msgs),
                        msgs)
        self.assertTrue(dev.restored())
        self.assertFalse(r.expect_reboot)
        self.assertEqual(r.last['boot_id'], BOOT2)
        for f in ('rundown-device.csv', 'rundown-summary.md', 'rundown.json', 'rundown-settings.json',
                  'rundown-start-state.json'):
            self.assertTrue(os.path.exists(os.path.join(self.tmp.name, f)), f)
        with open(os.path.join(self.tmp.name, 'rundown-live.csv')) as f:
            self.assertEqual(len(f.read().splitlines()), 1 + 120)  # 10 samples per adb poll, 12 polls
        with open(os.path.join(self.tmp.name, 'rundown-settings.json')) as f:
            self.assertEqual(json.load(f)['original']['system screen_brightness'], '77')
        with open(os.path.join(self.tmp.name, 'identity.txt')) as f:
            self.assertIn('versionName=3.7.1', f.read())
        # the run's settings were applied before the unplug
        applied = [c for c in dev.cmds if 'set-match-content-frame-rate-pref 0' in c]
        self.assertEqual(len(applied), 1)

    def test_abort_restores(self):  # playback never starts: an abort before the unplug, everything put back
        dev = Tablet(self.tmp.name, playing=False)
        r = self.start(dev)
        with self.assertRaises(stab.Abort) as cm:
            r.phase_battery_rundown({})
        self.assertIn('playback state PAUSED, not PLAYING: the player needs its one-time preparation',
                      str(cm.exception))
        self.assertTrue(dev.restored())
        self.assertFalse(dev.sampler or dev.player)
        self.assertEqual(self.events('INFO')[-1]['msg'], 'settings restored')

    def test_interrupt_before_unplug_restores(self):
        dev = Tablet(self.tmp.name)

        def ctrl_c():
            """Ctrl-C while the phase waits for the unplug."""
            raise KeyboardInterrupt()
        dev.drop = ctrl_c
        r = self.start(dev)
        with self.assertRaises(KeyboardInterrupt):
            r.phase_battery_rundown({})
        self.assertTrue(dev.restored())
        self.assertFalse(dev.sampler or dev.player)

    def test_not_back_in_time(self):  # the device never comes back: an abort, the report from the live view
        cap = os.path.join(self.tmp.name, 'serial.txt')
        open(cap, 'w').close()
        dev = Tablet(self.tmp.name, capture=cap, back_after=10 ** 6)
        r = self.start(dev, cap)
        with self.assertRaises(stab.Abort) as cm:
            r.phase_battery_rundown({'return_timeout_h': 0.5})
        self.assertIn('device not back 0.5 h after the unplug', str(cm.exception))
        with open(os.path.join(self.tmp.name, 'rundown.json')) as f:
            res = json.load(f)
        # 10 samples per adb poll: the unplug confirmation, then 60 polls 30 s apart while away
        self.assertEqual((res['source'], res['run_samples']), ('rundown-live.csv', 610))
        self.assertTrue(self.events('INFO')[-1]['msg'].startswith('rundown-summary.md written from the live view'))
        self.assertFalse(dev.restored())  # nothing can be restored on a device that is away
        fail = self.events('RESTORE_FAIL')
        self.assertEqual(len(fail), 1)
        self.assertIn(f'samples in {dev.path} on the device', fail[0]['msg'])
        self.assertIn("restore the settings with: adb -s fake shell 'cmd connectivity airplane-mode disable; ",
                      fail[0]['msg'])
        r.serial_scan()  # the capture stopped growing: the device is off, not a stale capture
        r.serial_scan()
        self.assertEqual(self.events('SERIAL_STALE'), [])

    def test_usb_back_before_the_shutdown(self):
        """The operator plugs USB in early: back on the same boot with sampler and player still running; both stopped,
        settings restored, no unexpected reboot; the report flags power back and not run down."""
        csv = discharge(n=360, edit={360: {'online': 1, 'status': 'Charging', 'cur_uA': 900000}})
        dev = Tablet(self.tmp.name, back_after=3, new_boot=False, csv=csv)
        r = self.start(dev)
        r.phase_battery_rundown({})
        self.assertTrue(dev.restored())
        self.assertFalse(dev.sampler or dev.player)
        self.assertEqual(self.events('UNEXPECTED_REBOOT'), [])
        self.assertTrue(any(e['msg'].startswith('same boot as before the unplug') for e in self.events('INFO')))
        self.assertEqual({e['msg'].split(':')[0] for e in self.events('RUNDOWN_INVALID')},
                         {'power_back', 'not_run_down'})
        # settings back before the first snapshot after the return
        snap = next(i for i, c in enumerate(dev.cmds) if c == 'getprop sys.boot.reason.last; '
                    'getprop persist.sys.boot.reason.history | head -3')
        self.assertTrue(any('airplane-mode disable' in c for c in dev.cmds[:snap]))

    def test_interrupt_after_the_return_restores(self):  # Ctrl-C or SIGTERM while the samples are pulled
        dev = Tablet(self.tmp.name, back_after=2)

        def ctrl_c():
            """Ctrl-C while the sample file is read."""
            raise KeyboardInterrupt()
        dev.cat_hook = ctrl_c
        r = self.start(dev)
        with self.assertRaises(KeyboardInterrupt):
            r.phase_battery_rundown({})
        self.assertTrue(dev.restored())
        self.assertFalse(dev.sampler or dev.player)
        self.assertEqual(self.events('RESTORE_FAIL'), [])

    def test_adb_gone_for_a_moment_is_not_the_unplug(self):
        dev = Tablet(self.tmp.name, back_after=3)
        dev.hiccups = 1
        r = self.start(dev)
        r.phase_battery_rundown({})
        msgs = [e['msg'] for e in self.events('INFO')]
        self.assertIn('adb did not answer for a moment but the device is back: not the unplug, still waiting', msgs)
        self.assertTrue(any(m.startswith('rundown: 10 h 00 min on battery') for m in msgs), msgs)
        self.assertTrue(dev.restored())

    def test_restart_before_the_battery_ran_down(self):
        """A new boot although the samples show no run on battery (a restart before the unplug) or a last sample
        above 5 % (a restart on battery): an unexpected reboot after all."""
        for csv, why in ((HEADER + '\n' + '\n'.join(sample(i, plugged=True) for i in range(-30, 0)) + '\n',
                          'never on battery'), (discharge(n=360), 'last sample 40 %')):
            with self.subTest(why):
                self.tearDown()
                self.setUp()
                dev = Tablet(self.tmp.name, back_after=2, csv=csv)
                r = self.start(dev)
                r.phase_battery_rundown({})
                ev = self.events('UNEXPECTED_REBOOT')
                self.assertEqual(len(ev), 1)
                self.assertIn('history=', ev[0]['msg'])
                self.assertTrue(any(e['msg'] == f'the device restarted before its battery ran down ({why}): an '
                                    'unexpected reboot' for e in self.events('INFO')))
                self.assertTrue(dev.restored())

    def test_live_view_failure_does_not_end_the_run(self):
        cap = os.path.join(self.tmp.name, 'serial.txt')
        open(cap, 'w').close()
        dev = Tablet(self.tmp.name, capture=cap, back_after=4)
        r = self.start(dev, cap)
        with mock.patch.object(stab.RundownLive, 'feed', side_effect=OSError('capture gone')):
            r.phase_battery_rundown({})
        self.assertTrue(any(e['msg'].startswith("rundown: live view stopped (OSError('capture gone'))")
                            for e in self.events('INFO')))
        self.assertTrue(dev.restored())
        with open(os.path.join(self.tmp.name, 'rundown.json')) as f:
            self.assertEqual(json.load(f)['source'], 'rundown-device.csv')

    def test_not_full_in_time_changes_nothing(self):
        dev = Tablet(self.tmp.name)
        dev.bat = 'BAT 97 Charging'
        r = self.start(dev)
        with self.assertRaises(stab.Abort) as cm:
            r.phase_battery_rundown({'charge_timeout_h': 0.1})
        self.assertIn('battery not full after 0.1 h on USB (97 %, Charging)', str(cm.exception))
        self.assertFalse([c for c in dev.cmds if c.startswith(('settings put', 'cmd connectivity'))])
        self.assertEqual(self.events('RESTORE_FAIL'), [])

    def test_not_unplugged_in_time(self):
        dev = Tablet(self.tmp.name)
        dev.drop = lambda: False
        r = self.start(dev)
        with self.assertRaises(stab.Abort) as cm:
            r.phase_battery_rundown({'unplug_timeout_min': 5})
        self.assertIn('USB still connected 5 min after the request', str(cm.exception))
        self.assertTrue(dev.restored())


if __name__ == '__main__':
    unittest.main()
