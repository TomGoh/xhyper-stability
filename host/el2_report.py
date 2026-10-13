#!/usr/bin/env python3
"""el2_report.py: read the EL2 parallel log (device/el2log.sh) of a stability run.

  el2_report.py <run dir>      writes <run dir>/el2-summary.md and el2-per-second.csv

Each <boot_id>.log under <run dir>/el2/ is a series of blocks: `T <boottime> <epoch> <cur kHz per policy...>` followed
by `<cpu> <el2 cycles> <timestamp ns>` lines. From consecutive blocks of one boot the EL2 share of every CPU is
  (cycles delta) / (timestamp delta in s x the CPU's current frequency in Hz),
an estimate: the frequency is the one read at the block, the cycle counter counts EL2 cycles at whatever frequency ran.
Anomalies: a CPU's count going down (the monitor lost accumulated cycles; one event per block, listing the CPUs; on
T11206 on 2026-10-13 every forced system suspend restarted CPUs 1-7 from about zero, CPU 0 kept its count), a CPU
missing from a block (the driver could not sample it), and a CPU whose EL2 share stays above HIGH for SUSTAIN
consecutive samples. Shares are computed only between samples whose count did not go down.
"""
import collections, csv, datetime, glob, json, os, re, sys

HIGH, SUSTAIN = 0.5, 5


def policy_map(run_dir):
    """cpu -> index of its policy in the T line, from identity written by stab.py (el2-policies.txt); T11206 default."""
    p = os.path.join(run_dir, 'el2-policies.txt')
    if os.path.exists(p):
        m = {}
        for i, line in enumerate(open(p).read().split()):
            a, _, b = line.partition(':')
            for part in b.split(','):
                lo, _, hi = part.partition('-')
                for c in range(int(lo), int(hi or lo) + 1):
                    m[c] = i
        return m
    return {c: 0 if c < 4 else 1 if c < 7 else 2 for c in range(8)}


def blocks(path, start=0):
    """Yields (offset_after, boottime, epoch, freqs_khz, {cpu: (cycles, ts_ns)}) for complete blocks from byte offset."""
    with open(path, errors='replace') as f:
        f.seek(start)
        cur, pos = None, start
        for line in iter(f.readline, ''):
            if line.startswith('T '):
                if cur:
                    yield (pos,) + cur
                parts = line.split()
                try:
                    cur = (float(parts[1]), int(parts[2]), [int(x) for x in parts[3:]], {})
                except (ValueError, IndexError):
                    cur = None
            elif cur is not None:
                m = re.match(r'^(\d+) (\d+) (\d+)$', line.strip())
                if m:
                    cur[3][int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
            pos = f.tell()
        # the last block may still be growing: not yielded


class Scanner:
    """Incremental anomaly scan over the logs of a run (used live by stab.py at each checkpoint)."""

    def __init__(self, run_dir):
        self.run_dir, self.pmap = run_dir, policy_map(run_dir)
        self.offsets, self.last, self.high = {}, {}, collections.Counter()
        self.ncpu = {}

    def scan(self):
        out = []
        for path in sorted(glob.glob(os.path.join(self.run_dir, 'el2', '*.log'))):
            boot = os.path.basename(path)[:-4]
            for off, bt, ep, fr, cpus in blocks(path, self.offsets.get(path, 0)):
                self.offsets[path] = off
                n = self.ncpu.setdefault(boot, len(cpus))
                missing = [c for c in range(n) if c not in cpus]
                if missing:
                    out.append(('EL2_MISSING', ep, f'boot {boot[:8]} t={bt:.0f}s: no sample for CPU {missing}'))
                down = []
                for c, (cy, ts) in cpus.items():
                    k = (boot, c)
                    if k in self.last:
                        pcy, pts = self.last[k]
                        if cy < pcy:
                            down.append(f'{c} ({pcy} -> {cy})')
                        elif ts > pts and fr:
                            f = fr[min(self.pmap.get(c, 0), len(fr) - 1)] * 1000
                            share = (cy - pcy) / ((ts - pts) / 1e9 * f) if f else 0
                            self.high[k] = self.high[k] + 1 if share > HIGH else 0
                            if self.high[k] == SUSTAIN:
                                out.append(('EL2_HIGH', ep, f'boot {boot[:8]} t={bt:.0f}s CPU {c}: EL2 share above '
                                            f'{HIGH:.0%} for {SUSTAIN} samples (now {share:.0%})'))
                    self.last[k] = (cy, ts)
                if down:  # one event per reset; on T11206 every system suspend resets the non-boot CPUs (2026-10-13)
                    out.append(('EL2_COUNT_DOWN', ep, f'boot {boot[:8]} t={bt:.0f}s: count restarted on CPU '
                                + ', '.join(down)))
        return out


def phases(run_dir):
    """[(start_epoch, end_epoch, name)] from events.jsonl."""
    evs = [json.loads(l) for l in open(os.path.join(run_dir, 'events.jsonl')) if l.strip()]
    ep = lambda e: datetime.datetime.strptime(e['t'], '%Y-%m-%dT%H:%M:%S%z').timestamp()
    out, start = [], {}
    for e in evs:
        if e['kind'] == 'PHASE_START':
            start[e['phase']] = ep(e)
        elif e['kind'] in ('PHASE_END', 'ABORT') and e['phase'] in start:
            out.append((start.pop(e['phase']), ep(e), e['phase']))
    for ph, s in start.items():
        out.append((s, float('inf'), ph))
    return sorted(out)


def report(run_dir):
    pmap = policy_map(run_dir)
    phs = phases(run_dir) if os.path.exists(os.path.join(run_dir, 'events.jsonl')) else []
    acc = collections.defaultdict(lambda: collections.defaultdict(list))  # phase -> cpu -> [share]
    rows = []
    sc = Scanner(run_dir)
    anomalies = sc.scan()
    for path in sorted(glob.glob(os.path.join(run_dir, 'el2', '*.log'))):
        boot, last = os.path.basename(path)[:-4], {}
        for _, bt, ep, fr, cpus in blocks(path):
            ph = next((n for s, e, n in phs if s <= ep <= e), '-')
            for c, (cy, ts) in sorted(cpus.items()):
                if c in last and ts > last[c][1] and cy >= last[c][0] and fr:
                    f = fr[min(pmap.get(c, 0), len(fr) - 1)] * 1000
                    share = (cy - last[c][0]) / ((ts - last[c][1]) / 1e9 * f) if f else 0
                    acc[ph][c].append(share)
                    rows.append((boot[:8], ep, f'{bt:.1f}', ph, c, cy - last[c][0], f, f'{share:.4f}'))
                last[c] = (cy, ts)
    with open(os.path.join(run_dir, 'el2-per-second.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['boot', 'epoch', 'boottime', 'phase', 'cpu', 'el2_cycles', 'freq_hz', 'el2_share'])
        w.writerows(rows)
    L = ['# EL2 parallel log', '', f'{len(rows)} per-CPU samples; anomalies {len(anomalies)}.', '',
         'EL2 share per phase and CPU (mean / 99th percentile / max, estimated as cycles over time x current frequency):',
         '', '| phase | ' + ' | '.join(f'CPU{c}' for c in range(8)) + ' |', '|---|' + '---|' * 8]
    for ph in [n for _, _, n in phs] + (['-'] if '-' in acc else []):
        cells = []
        for c in range(8):
            v = sorted(acc[ph].get(c, []))
            cells.append(f'{sum(v) / len(v):.1%} / {v[int(len(v) * .99) - 1 if len(v) > 1 else 0]:.1%} / {v[-1]:.1%}'
                         if v else '-')
        L.append(f'| {ph} | ' + ' | '.join(cells) + ' |')
    L += ['', '## Anomalies', ''] + [f'- {datetime.datetime.fromtimestamp(ep):%F %T} {k}: {m}' for k, ep, m in
                                    anomalies[:300]]
    open(os.path.join(run_dir, 'el2-summary.md'), 'w').write('\n'.join(L) + '\n')
    return len(anomalies)


if __name__ == '__main__':
    n = report(sys.argv[1])
    print(f'{sys.argv[1]}/el2-summary.md: {n} anomalies')
