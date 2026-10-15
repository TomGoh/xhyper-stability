#!/usr/bin/env python3
"""compare.py LABEL=RUN_DIR ...: side-by-side table of stability runs (e.g. the hypervisor image against stock).

Per run: verdict, event counts by kind, kernel findings grouped by source (the 'at <file>:<line>' of WARNING splats,
the report type and task of long preempt-off and IRQ-off reports; aggregated events add their `count`), suspend
results, stress/memtest outcomes, cyclictest maximum latency per CPU (idle and loaded). Prints markdown; with
--out FILE also writes it there.
"""
# How it is run: python3 host/compare.py stock=<run dir> image=<run dir> ... [--out FILE]. Every argument that does
# not start with '--' (and is not the --out value) must be LABEL=RUN_DIR; the table has one column per run, in the
# order given. Read from each run directory: events.jsonl, summary.md (its first line holds the verdict) and
# cyclictest-idle.txt / cyclictest-stress.txt when present. Nothing is written into the run directories.
#
# Structure:
#   Reading a run         load: the counts and outcomes of one run directory
#   Table                 main: the markdown table, printed and written to --out FILE
import collections, json, os, re, sys

# ==== Reading a run ===================================================================================================

def load(d):
    """The comparable facts of run directory d: {'kinds': Counter of event kinds, 'kernel': Counter of kernel
    findings by source, 'suspend': Counter of suspend cycles (ok / other), 'stress': Counter of stress-ng outcomes,
    'memtest': [stressapptest and MEMTEST_FAIL messages], 'cyc_idle' / 'cyc_stress': [cyclictest Max per CPU in us],
    'verdict': the verdict of summary.md's first line ('?' without one)}. Counts add an aggregated event's `count`."""
    evs = [json.loads(l) for l in open(os.path.join(d, 'events.jsonl')) if l.strip()]
    r = {'kinds': collections.Counter()}
    # src: kernel finding key ('<kind> <source>') -> count
    src = collections.Counter()
    for e in evs:
        n = e.get('count', 1)  # an aggregated event stands for `count` findings
        r['kinds'][e['kind']] += n
        if e['kind'] in ('KERNEL_BAD', 'KERNEL_WARN'):
            # m: a WARNING splat, by its 'at <file>:<line>'
            m = re.search(r'WARNING: CPU: \d+ PID: \d+ at (\S+)', e['msg'])
            # preempt/IRQ-off reports by report type and task (digits as N): from `group` ('<preemptoff|irqsoff> <task>
            # <reason class>', runs since 2026-10-14), else parsed from the line
            g = e.get('group', '')
            p = re.search(r'(preemptoff|irqsoff)_warn: C\d+ T:<\d+>(.*?) D:\s*(\d+(?:\.\d+)?)\s*ms', e['msg'])
            # off: (report type, task, lasted 100 ms or more) or None when the event is not such a report; a group's
            # reason class is 'short' below 100 ms
            if g.count(' ') >= 2:  # the task name can hold spaces ('Signal Catcher')
                t, rest = g.split(' ', 1)
                comm, cls = rest.rsplit(' ', 1)
                off = (t, comm, cls != 'short')
            else:
                off = (p.group(1), re.sub(r'[0-9]+', 'N', p.group(2)), float(p.group(3)) >= 100) if p else None
            if m:
                src[f"{e['kind']} WARN {m.group(1)}"] += n
            elif off and off[2]:
                src[f"{e['kind']} {'preempt-off' if off[0] == 'preemptoff' else 'irq-off'} >=100ms {off[1]}"] += n
            elif not off:
                # any other kernel line: its text without the kernel time stamp, the '(collected in <phase>)' suffix
                # and a trailing '[<grading reason>]', cut to 64 characters
                msg = re.sub(r' \[[^\]]*\]$', '', re.sub(r' \(collected in [^)]*\)$', '', e['msg']))
                src[f"{e['kind']} {re.sub(r'^\[\s*[0-9.]+\]\s*', '', msg)[:64]}"] += n
            # (a preempt/IRQ-off report shorter than 100 ms is not listed)
    r['kernel'] = src
    # suspend cycles: INFO 'suspend <n>: success ...' lines; ok when the success counter rose and the fail one did not
    r['suspend'] = collections.Counter(
        'ok' if re.search(r'success \+1 fail \+0', e['msg']) else 'other'
        for e in evs if e['kind'] == 'INFO' and re.match(r'suspend \S+: success', e['msg']))
    # stress-ng outcomes: the text after the last ': ' of its INFO, STRESS_FAIL and SKIP events
    r['stress'] = collections.Counter(re.sub(r'.*: ', '', e['msg']) for e in evs
                                      if e['kind'] in ('INFO', 'STRESS_FAIL', 'SKIP')
                                      and e['msg'].startswith('stress-ng'))
    r['memtest'] = [e['msg'] for e in evs if e['msg'].startswith('stressapptest') or e['kind'] == 'MEMTEST_FAIL']
    for load_ in ('idle', 'stress'):
        p = os.path.join(d, f'cyclictest-{load_}.txt')
        r[f'cyc_{load_}'] = [int(x) for x in re.findall(r'Max:\s+(\d+)', open(p).read())] if os.path.exists(p) else []
    s = os.path.join(d, 'summary.md')
    r['verdict'] = open(s).readline().split(':')[-1].strip() if os.path.exists(s) else '?'
    return r


# ==== Table ===========================================================================================================

def main():
    """Prints the markdown table of the runs on the command line (and writes it to --out FILE)."""
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    out = sys.argv[sys.argv.index('--out') + 1] if '--out' in sys.argv else None
    if out in args:
        args.remove(out)
    # runs: [(label, load() of the run directory)]
    runs = [(a.split('=', 1)[0], load(a.split('=', 1)[1])) for a in args]
    L = ['| item | ' + ' | '.join(n for n, _ in runs) + ' |', '|---|' + '---|' * len(runs)]
    # row: appends the table row `name` whose cell for each run is f(that run's load() result)
    row = lambda name, f: L.append(f'| {name} | ' + ' | '.join(str(f(r)) for _, r in runs) + ' |')
    row('verdict', lambda r: r['verdict'])
    # event counts of every kind seen in any run, except the bookkeeping kinds
    for k in sorted({k for _, r in runs for k in r['kinds']} - {'INFO', 'PHASE_START', 'PHASE_END'}):
        row(f'events {k}', lambda r, k=k: r['kinds'].get(k, 0))
    # kernel findings by source, every source seen in any run
    for k in sorted({k for _, r in runs for k in r['kernel']}):
        row(k, lambda r, k=k: r['kernel'].get(k, 0))
    row('suspend cycles ok / other', lambda r: f"{r['suspend'].get('ok', 0)} / {r['suspend'].get('other', 0)}")
    row('stress-ng results', lambda r: ', '.join(f'{k} x{v}' for k, v in r['stress'].items()) or '-')
    row('memtest', lambda r: '; '.join(r['memtest'][-1:]) or '-')
    row('cyclictest idle max per CPU (us)', lambda r: ' '.join(map(str, r['cyc_idle'])) or '-')
    row('cyclictest loaded max per CPU (us)', lambda r: ' '.join(map(str, r['cyc_stress'])) or '-')
    text = '\n'.join(L) + '\n'
    print(text)
    if out:
        open(out, 'w').write(text)


if __name__ == '__main__':
    main()
