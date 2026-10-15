#!/usr/bin/env python3
"""rundown_report.py: results of a battery rundown (phase battery_rundown of host/stab.py, \
samples of device/rundown.sh).

  rundown_report.py RUN_DIR                    writes RUN_DIR/rundown-summary.md and rundown.json
  rundown_report.py CSV [--live LIVE_CSV] [--sample-s 60] [--brightness 128] [--refresh-hz 90] [--out FILE]
                                               a bare sample file: prints the summary (and writes it to FILE)
  rundown_report.py compare [LABEL=]A [LABEL=]B ...
                                               side-by-side table of run directories or sample files

The run starts at the first sample without external power (online 0, the USB unplug) and ends at the last sample (the
automatic shutdown). Power per sample is voltage x current (current_now is negative while discharging, as the kernel's
power supply interface defines it; a fuel gauge that reports discharge as positive, as ACPI batteries do, is detected
by the median current on battery and noted); energy is its integral over the device clock (trapezoids between samples,
also across a gap). Validity checks, each a named flag: backlight not the set
raw value, the player without a process, playback not PLAYING, display refresh rate not the set one, external power
back during the run, gaps between samples longer than 3 x the sample interval, the device restarting (uptime going
down), the last sample above 5 % (did not run down). Columns are taken by name from the header line, so the layout of
any version of the sampler works (`vlc` is the older name of `player`); a live file from the serial console carries a
host_time column first and may have no header line (then the device file's columns follow host_time).
"""
# The docstring above is also the --help text (main() prints it); the backslash at the end of its first line joins
# that line to the next one, so the printed text is one line there.
#
# How it is used: host/stab.py imports this module. Its battery_rundown phase calls report_dir() on the run directory
# once the device is back (or on the live view when the device does not come back) and uses DEFAULTS, FLAGS, hm() and
# f0() for its events and the summary.md line. By hand, it reports a run directory, a bare sample file, or compares
# several runs (see the usage above).
#
# Files: rundown-device.csv (the device's sample file pulled after the run), rundown-live.csv (the samples received
# over the serial console: host_time, then the sample), plan.json (the phase's parameters); written:
# rundown-summary.md and rundown.json.
#
# Structure:
#   Constants             DEFAULTS, COLUMNS_V1, ALIAS, LEVELS, FLAGS
#   Loading samples       load
#   Number formatting     fnum, f0, hm
#   Analysis              analyse, host_time, live_view
#   Summary               summary: the markdown of one result
#   Run directories       params_of, report_dir, result_of
#   Comparison            compare: the markdown table of several results
#   Command line          main, load_header
import csv, datetime, json, os, statistics, sys

# ==== Constants =======================================================================================================

# parameters of the battery_rundown phase (plans/battery-rundown.json) when a plan leaves them out
DEFAULTS = {'apk': 'vlc', 'media': 'bbb60', 'brightness': 128, 'refresh_hz': 90, 'volume': 3, 'airplane': True,
            'sample_s': 60, 'charge_timeout_h': 4, 'unplug_timeout_min': 30, 'return_timeout_h': 24,
            'activity': 'org.videolan.vlc.gui.video.VideoPlayerActivity'}
# the first sampler's layout (2026-10-14 prototype), for files without a header line
COLUMNS_V1 = ('epoch,uptime_s,cap,status,online,volt_uV,ocv_uV,cur_uA,cur_avg_uA,counter_uAh,temp_dC,backlight,vlc,'
              'f0_kHz,f4_kHz,f7_kHz,vsync_hz').split(',')
# older column names -> current ones
ALIAS = {'vlc': 'player'}
# capacity levels (%) of the timeline: the time from the start at which each was first reached
LEVELS = (90, 80, 70, 60, 50, 40, 30, 20, 10, 5)
# validity flags: name -> meaning (also the text of stab.py's RUNDOWN_INVALID events)
FLAGS = {
    'never_unplugged': 'no sample without external power: the rundown never started',
    'backlight': 'backlight raw value not the set one',
    'player_stopped': 'the player had no process',
    'not_playing': 'playback state not PLAYING',
    'vsync': 'display refresh rate not the set one',
    'power_back': 'external power back (online 1) during the run',
    'gap': 'gap between samples longer than 3 x the sample interval',
    'reboot': 'uptime went down: the device restarted during the run',
    'not_run_down': 'last sample above 5 %: the device did not run down',
}


# ==== Loading samples =================================================================================================

def load(path, columns=None):
    """Rows (dicts by column name) of a sample file; rows whose field count does not match are dropped (a line cut
    short on the serial console). A file whose first line is not a header takes `columns`, by default the first
    sampler's layout, after a host_time column when its first field is not a number (a live file)."""
    with open(path, newline='', errors='replace') as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    if not lines:
        return []
    head = lines[0].split(',')
    if head[0] in ('epoch', 'host_time'):
        lines = lines[1:]
    elif columns:
        head = columns
    else:
        head = COLUMNS_V1 if head[0].isdigit() else ['host_time'] + COLUMNS_V1
    head = [ALIAS.get(h, h) for h in head]
    rows = []
    for vals in csv.reader(lines):
        if len(vals) == len(head):
            rows.append(dict(zip(head, vals)))
    return rows


# ==== Number formatting ===============================================================================================

def fnum(r, k):
    """Column k of row r as a float; None when it is missing, empty or not a number."""
    try:
        return float(r.get(k, ''))
    except (TypeError, ValueError):
        return None


def f0(x, unit=''):
    """x without decimals followed by unit; '-' for None."""
    return '-' if x is None else f'{x:.0f}{unit}'


def hm(s):
    """Seconds as 'H h MM min'; '-' for None."""
    if s is None:
        return '-'
    s = int(round(s))
    return f'{s // 3600} h {s % 3600 // 60:02d} min'


# ==== Analysis ========================================================================================================

def analyse(rows, sample_s=60, brightness=128, refresh_hz=90, live=None):
    """Result dict of one rundown (see the module doc); live: rows of the live file (host_time + sample), or None."""
    res = {'samples': len(rows), 'sample_s': sample_s, 'brightness': brightness, 'refresh_hz': refresh_hz, 'flags': {}}
    rows = [r for r in rows if fnum(r, 'epoch') is not None]

    # the run: from the first sample without external power (the unplug) to the last sample
    start = next((i for i, r in enumerate(rows) if r.get('online') == '0'), None)
    if start is None:
        res['flags']['never_unplugged'] = {'count': len(rows), 'first_s': None, 'cap': None, 'detail': ''}
        return res
    run = rows[start:]
    t0, t1 = fnum(run[0], 'epoch'), fnum(run[-1], 'epoch')
    # rel: a sample's time in seconds from the start of the run (device clock)
    rel = lambda r: fnum(r, 'epoch') - t0
    res.update(start_epoch=t0, end_epoch=t1, duration_s=t1 - t0, run_samples=len(run),
               start_cap=fnum(run[0], 'cap'), end_cap=fnum(run[-1], 'cap'),
               start_volt=fnum(run[0], 'volt_uV'), end_volt=fnum(run[-1], 'volt_uV'))

    # capacity timeline: the first sample at or below each level (a sample without a capacity counts as 101 %)
    res['timeline'] = []
    for lvl in LEVELS:
        hit = next((r for r in run if (fnum(r, 'cap') if fnum(r, 'cap') is not None else 101) <= lvl), None)
        res['timeline'].append((lvl, rel(hit) if hit else None))

    # power and energy of the samples on battery; sign: -1 by the kernel's convention (discharge is negative),
    # 1 for a fuel gauge whose median current on battery is positive
    pw = []  # (epoch, watts) of the samples on battery
    cur = [fnum(r, 'cur_uA') for r in run if r.get('online') == '0' and fnum(r, 'cur_uA') is not None]
    sign = -1
    if cur and statistics.median(cur) > 0:
        sign, res['current_sign'] = 1, 'the fuel gauge reports discharge as a positive current: taken as discharge'
    for r in run:
        v, i = fnum(r, 'volt_uV'), fnum(r, 'cur_uA')
        if r.get('online') == '0' and v is not None and i is not None:
            pw.append((fnum(r, 'epoch'), v * sign * i / 1e12))
    if pw:
        res['avg_w'] = statistics.fmean(p for _, p in pw)
        res['median_w'] = statistics.median(p for _, p in pw)
        # trapezoids between consecutive samples on battery, in Wh
        res['energy_wh'] = sum((a[1] + b[1]) / 2 * (b[0] - a[0]) for a, b in zip(pw, pw[1:])) / 3600
    # charge counter (uAh) at the start minus at the end, in mAh
    c0, c1 = fnum(run[0], 'counter_uAh'), fnum(run[-1], 'counter_uAh')
    res['charge_mah'] = (c0 - c1) / 1000 if c0 is not None and c1 is not None else None
    temps = [fnum(r, 'temp_dC') for r in run if fnum(r, 'temp_dC') is not None]
    res['temp_max_c'] = max(temps) / 10 if temps else None

    # validity flags (FLAGS)
    def flag(name, hits, detail=''):
        """Records flag `name` when hits (the offending samples) is not empty: their count, the first one's time from
        the start and capacity, and detail."""
        if hits:
            res['flags'][name] = {'count': len(hits), 'first_s': rel(hits[0]), 'cap': fnum(hits[0], 'cap'),
                                  'detail': detail}
    flag('backlight', [r for r in run if r.get('backlight', '') != '' and fnum(r, 'backlight') != brightness])
    flag('player_stopped', [r for r in run if r.get('player') == '0'])
    flag('not_playing', [r for r in run if r.get('playback') and not r['playback'].startswith('PLAYING')])
    # vsync: refresh rates more than 0.5 Hz off the set one; the detail lists the rates seen
    flag('vsync', [r for r in run if fnum(r, 'vsync_hz') is not None and abs(fnum(r, 'vsync_hz') - refresh_hz) > 0.5],
         ', '.join(sorted({r['vsync_hz'] for r in run if fnum(r, 'vsync_hz') is not None
                           and abs(fnum(r, 'vsync_hz') - refresh_hz) > 0.5})) + ' Hz')
    flag('power_back', [r for r in run if r.get('online') == '1'])
    # gaps: (the sample after the gap, seconds since the sample before it)
    gaps = [(b, fnum(b, 'epoch') - fnum(a, 'epoch')) for a, b in zip(run, run[1:])
            if fnum(b, 'epoch') - fnum(a, 'epoch') > 3 * sample_s]
    flag('gap', [b for b, _ in gaps], f'longest {max((g for _, g in gaps), default=0):.0f} s')
    # reboot: a sample whose uptime is below the previous sample's
    flag('reboot', [b for a, b in zip(run, run[1:]) if None not in (fnum(a, 'uptime_s'), fnum(b, 'uptime_s'))
                    and fnum(b, 'uptime_s') < fnum(a, 'uptime_s')])
    if res['end_cap'] is not None and res['end_cap'] > 5:
        flag('not_run_down', [run[-1]], f"last sample {res['end_cap']:.0f} %")
    if live is not None:
        res['live'] = live_view(live, {r.get('epoch') for r in run})
    return res


def host_time(r):
    """The host_time column of a live-file row ('YYYY-MM-DD HH:MM:SS.fff') as an epoch; None when it has none."""
    try:
        return datetime.datetime.strptime(r.get('host_time', ''), '%Y-%m-%d %H:%M:%S.%f').timestamp()
    except ValueError:
        return None


def live_view(live, run_epochs):
    """Samples received over the serial console: count, how many of the run's device samples are missing there, and the
    run's start and end by the host clock (first unplugged sample, last sample received)."""
    got = [r for r in live if host_time(r) is not None]
    first = next((r for r in got if r.get('online') == '0'), None)
    out = {'samples': len(got), 'missing': len(run_epochs - {r.get('epoch') for r in got})}
    if first:
        out.update(start=first['host_time'], end=got[-1]['host_time'],
                   duration_s=host_time(got[-1]) - host_time(first))
    return out


# ==== Summary =========================================================================================================

def summary(res, source=''):
    """Markdown summary of an analyse() result."""
    fl = res['flags']
    L = ['# Battery rundown', '', f"- source: {source or '-'} ({res['samples']} samples)",
         f"- set: backlight {res['brightness']:g}, refresh rate {res['refresh_hz']:g} Hz, a sample every "
         f"{res['sample_s']:g} s"]
    if 'start_epoch' in res:
        # ts: an epoch as local date and time; v: microvolts as volts
        ts = lambda e: datetime.datetime.fromtimestamp(e).strftime('%Y-%m-%d %H:%M:%S')
        v = lambda x: f'{x / 1e6:.3f} V' if x is not None else '-'
        L += [f"- start (first sample on battery): {ts(res['start_epoch'])}, {f0(res['start_cap'], ' %')}, "
              f"{v(res['start_volt'])}",
              f"- end (last sample): {ts(res['end_epoch'])}, {f0(res['end_cap'], ' %')}, {v(res['end_volt'])}",
              f"- duration: **{hm(res['duration_s'])}** ({res['duration_s']:.0f} s, {res['run_samples']} samples, "
              'device clock)']
        if 'energy_wh' in res:
            L.append(f"- energy: {res['energy_wh']:.2f} Wh; power average {res['avg_w']:.3f} W, median "
                     f"{res['median_w']:.3f} W" + (f"; charge counter {res['charge_mah']:.0f} mAh"
                                                   if res.get('charge_mah') is not None else ''))
        if res.get('temp_max_c') is not None:
            L.append(f"- battery temperature max {res['temp_max_c']:.1f} C")
        if res.get('current_sign'):
            L.append(f"- current: {res['current_sign']}")
    lv = res.get('live')
    if lv:
        L.append(f"- live view over the serial console: {lv['samples']} samples, {lv['missing']} of the run's samples "
                 'missing there' + (f"; start {lv['start']}, last {lv['end']} (host clock, {hm(lv['duration_s'])})"
                                    if 'start' in lv else ''))
    L.append('- validity: '
             + ('no flags' if not fl else 'FLAGS ' + ', '.join(f"{k} x{v['count']}" for k, v in fl.items())))
    if res.get('timeline'):
        L += ['', '## Capacity timeline', '', '| capacity | time from start |', '|---|---|']
        L += [f'| {lvl} % | {hm(s)} |' for lvl, s in res['timeline']]
    if fl:
        L += ['', '## Validity flags', '',
              '| flag | samples | first (from start, capacity) | meaning |', '|---|---|---|---|']
        for k, v in fl.items():
            first = f"{hm(v['first_s'])}, {f0(v['cap'], ' %')}" if v['first_s'] is not None else '-'
            L.append(f"| {k} | {v['count']} | {first} | {FLAGS[k]}{'; ' + v['detail'] if v['detail'] else ''} |")
    return '\n'.join(L) + '\n'


# ==== Run directories =================================================================================================

def params_of(run_dir):
    """The battery_rundown phase's parameters from the run's plan.json, over DEFAULTS."""
    p = dict(DEFAULTS)
    try:
        with open(os.path.join(run_dir, 'plan.json')) as f:
            plan = json.load(f)
        p.update(next((ph for ph in plan.get('phases', []) if ph.get('type') == 'battery_rundown'), {}))
    except (OSError, ValueError):
        pass
    return p


def report_dir(run_dir):
    """Analyses RUN_DIR/rundown-device.csv (else the live file), writes rundown-summary.md and rundown.json."""
    p = params_of(run_dir)
    dev, live = os.path.join(run_dir, 'rundown-device.csv'), os.path.join(run_dir, 'rundown-live.csv')
    src = dev if os.path.exists(dev) else live
    rows = load(src) if os.path.exists(src) else []
    # lrows: the live file as the live view next to the device file (None when there is none or it is the source)
    lrows = load(live) if os.path.exists(live) and src != live else None
    res = analyse(rows, p['sample_s'], p['brightness'], p['refresh_hz'], lrows)
    res['source'] = os.path.basename(src)
    with open(os.path.join(run_dir, 'rundown-summary.md'), 'w') as f:
        f.write(summary(res, res['source']))
    with open(os.path.join(run_dir, 'rundown.json'), 'w') as f:
        json.dump(res, f, indent=1)
    return res


def result_of(path, sample_s=60, brightness=128, refresh_hz=90):
    """The result of a run directory (its rundown.json, else report_dir(), which writes the files) or of a sample
    file (analysed with the given settings, nothing written)."""
    if os.path.isdir(path):
        j = os.path.join(path, 'rundown.json')
        if os.path.exists(j):
            with open(j) as f:
                return json.load(f)
        return report_dir(path)
    return analyse(load(path), sample_s, brightness, refresh_hz)


# ==== Comparison ======================================================================================================

def compare(results):
    """Markdown table of [(label, result)]: duration, energy, power, flags; numbers with the difference to the first."""
    def cell(r, k, fmt, base):
        """Value k of result r formatted with fmt, and its relative difference to the first result's (base) value
        when that is non-zero and r is not the first result; '-' when r has no value."""
        v = r.get(k)
        if v is None:
            return '-'
        b = base.get(k)
        return fmt(v) + (f' ({(v - b) / b:+.1%})' if b and r is not base else '')
    base = results[0][1]
    L = ['| item | ' + ' | '.join(n for n, _ in results) + ' |', '|---|' + '---|' * len(results)]
    # (row name, result key, format)
    rows = (('duration', 'duration_s', hm), ('energy (Wh)', 'energy_wh', lambda v: f'{v:.2f}'),
            ('average power (W)', 'avg_w', lambda v: f'{v:.3f}'),
            ('median power (W)', 'median_w', lambda v: f'{v:.3f}'),
            ('charge counter (mAh)', 'charge_mah', lambda v: f'{v:.0f}'),
            ('start capacity (%)', 'start_cap', lambda v: f'{v:.0f}'),
            ('last capacity (%)', 'end_cap', lambda v: f'{v:.0f}'),
            ('battery temperature max (C)', 'temp_max_c', lambda v: f'{v:.1f}'))
    for name, k, fmt in rows:
        L.append(f'| {name} | ' + ' | '.join(cell(r, k, fmt, base) for _, r in results) + ' |')
    L.append('| validity flags | ' + ' | '.join(', '.join(f"{k} x{v['count']}" for k, v in r['flags'].items()) or 'none'
                                                for _, r in results) + ' |')
    return '\n'.join(L) + '\n'


# ==== Command line ====================================================================================================

def main(argv):
    """The command line (see the module doc): prints the summary or the comparison, also to --out FILE; returns 0."""
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    # options take one value each (kept as given); everything else is a positional argument
    opt = {'--sample-s': 60, '--brightness': 128, '--refresh-hz': 90, '--live': None, '--out': None}
    args, i = [], 0
    while i < len(argv):
        if argv[i] in opt:
            opt[argv[i]] = argv[i + 1]
            i += 2
        else:
            args.append(argv[i])
            i += 1
    num = {k: float(opt[k]) for k in ('--sample-s', '--brightness', '--refresh-hz')}
    if args[0] == 'compare':
        # [LABEL=]PATH: the label defaults to the path's last component
        res = [(a.split('=', 1)[0] if '=' in a else os.path.basename(a.rstrip('/')),
                result_of(a.split('=', 1)[-1], num['--sample-s'], num['--brightness'], num['--refresh-hz']))
               for a in args[1:]]
        text = compare(res)
    elif os.path.isdir(args[0]):
        report_dir(args[0])
        with open(os.path.join(args[0], 'rundown-summary.md')) as f:
            text = f.read()
    else:
        # a bare sample file; a live file without a header line is read as host_time plus the columns of the
        # device file's header line (when the device file has none, load() falls back to the first layout)
        head = load_header(args[0])
        live = load(opt['--live'], ['host_time'] + head if head else None) if opt['--live'] else None
        res = analyse(load(args[0]), num['--sample-s'], num['--brightness'], num['--refresh-hz'], live)
        text = summary(res, os.path.basename(args[0]))
    print(text, end='')
    if opt['--out']:
        with open(opt['--out'], 'w') as f:
            f.write(text)
    return 0


def load_header(path):
    """The column names of a device sample file's header line, None when it has none."""
    with open(path, errors='replace') as f:
        head = f.readline().strip().split(',')
    return head if head[0] == 'epoch' else None


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
