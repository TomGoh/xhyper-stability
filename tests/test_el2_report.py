#!/usr/bin/env python3
"""Tests of the per-step EL2 report (host/el2_report.py) and the step names of a repeated el2_stress plan.

  python3 -m unittest discover -s host -p 'test_*.py' -v
"""
import csv, json, os, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'host'))
import el2_report  # noqa: E402
import stab  # noqa: E402

E = 1760000000  # synthetic epoch


class StepNames(unittest.TestCase):
    def test_repeated_steps_get_round_suffix(self):
        self.assertEqual(stab.step_names(['ipi', 'timer', 'ipi', 'tee', 'ipi', 'timer']),
                         ['ipi', 'timer', 'ipi-r2', 'tee', 'ipi-r3', 'timer-r2'])

    def test_single_round_unchanged(self):
        self.assertEqual(stab.step_names(['ipi', 'pmu', 'all']), ['ipi', 'pmu', 'all'])


class Innermost(unittest.TestCase):
    PHS = [(E, E + 1000, '6-el2_stress'), (E + 10, E + 100, '6-el2_stress/ipi'),
           (E + 110, E + 200, '6-el2_stress/ipi-r2')]

    def test_step_wins_over_outer_phase(self):
        self.assertEqual(el2_report.phase_of(self.PHS, E + 50), '6-el2_stress/ipi')
        self.assertEqual(el2_report.phase_of(self.PHS, E + 150), '6-el2_stress/ipi-r2')
        self.assertEqual(el2_report.phase_of(self.PHS, E + 105), '6-el2_stress')
        self.assertEqual(el2_report.phase_of(self.PHS, E + 5000), '-')

    def test_same_start_second(self):  # event times have whole seconds: a step can start in its phase's second
        self.assertEqual(el2_report.phase_of([(E, E + 1000, 'b'), (E, E + 100, 'a')], E + 50), 'a')


class Report(unittest.TestCase):
    def test_samples_land_in_their_step(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'el2'))
            t = lambda ep: time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(ep))
            evs = [(E, '6-el2_stress', 'PHASE_START'), (E + 10, '6-el2_stress/ipi', 'PHASE_START'),
                   (E + 100, '6-el2_stress/ipi', 'PHASE_END'), (E + 110, '6-el2_stress/ipi-r2', 'PHASE_START'),
                   (E + 200, '6-el2_stress/ipi-r2', 'PHASE_END'), (E + 1000, '6-el2_stress', 'PHASE_END')]
            with open(os.path.join(d, 'events.jsonl'), 'w') as f:
                for ep, ph, kind in evs:
                    f.write(json.dumps({'t': t(ep), 'phase': ph, 'kind': kind, 'msg': ''}) + '\n')
            with open(os.path.join(d, 'el2', 'b00710ad-0000-4000-8000-000000000000.log'), 'w') as f:
                for i, ep in enumerate([E + 20, E + 21, E + 22, E + 150, E + 151, E + 152, E + 153]):
                    f.write(f'T {100 + i}.0 {ep} 2000000 2000000 2000000\n')
                    f.write(''.join(f'{c} {(i + 1) * 100000000} {(100 + i) * 10 ** 9}\n' for c in range(8)))
            el2_report.report(d)
            with open(os.path.join(d, 'el2-per-second.csv')) as f:
                phases = {r['phase'] for r in csv.DictReader(f)}
            self.assertEqual(phases, {'6-el2_stress/ipi', '6-el2_stress/ipi-r2'})
            with open(os.path.join(d, 'el2-summary.md')) as f:
                rows = {l.split(' | ')[0][2:]: l for l in f if l.startswith('| 6-el2_stress')}
            self.assertIn('5.0% / 5.0% / 5.0%', rows['6-el2_stress/ipi'])
            self.assertIn('5.0% / 5.0% / 5.0%', rows['6-el2_stress/ipi-r2'])
            self.assertNotIn('%', rows['6-el2_stress'])


if __name__ == '__main__':
    unittest.main()
