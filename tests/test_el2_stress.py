#!/usr/bin/env python3
"""Tests of the EL2 stress phase's set-up (phase_el2_stress in host/stab.py), without a device.

  python3 -m unittest discover -s tests -v       (from the repository root)
"""
# Structure:
#   glmark2 install       Glmark2Install: which step lists install glmark2 first
import os, sys, types, unittest

# the module under test lives in the repository's host/ directory
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'host'))
import stab  # noqa: E402


# ==== glmark2 install =================================================================================================

class Glmark2Install(unittest.TestCase):
    """Guards which step lists make phase_el2_stress install glmark2 first: gpu and all run it, and the default list
    ends with all. Fixture: a stand-in for Run whose dev answers every command with nothing and whose event() stops
    the phase at its first PHASE_START. Asserts the ensure_apks calls made before that."""

    class Stop(Exception):
        """Raised by the stand-in's event(): the phase got as far as its first step."""

    def installs(self, plan):
        """The ensure_apks calls phase_el2_stress makes for `plan` before its first step starts."""
        calls = []

        def stop(*args, **kwargs):
            raise self.Stop()

        dev = types.SimpleNamespace(sh=lambda cmd, timeout=60: (0, ''))
        run = types.SimpleNamespace(dev=dev, phase='6-el2_stress', ensure_apks=calls.append, event=stop)
        with self.assertRaises(self.Stop):
            stab.Run.phase_el2_stress(run, plan)
        return calls

    def test_default_list(self):
        """No `steps` in the plan: the default list ends with all, which runs the gpu part."""
        self.assertEqual(self.installs({}), [['glmark2']])

    def test_listed_steps(self):
        self.assertEqual(self.installs({'steps': ['gpu']}), [['glmark2']])
        self.assertEqual(self.installs({'steps': ['ipi', 'all']}), [['glmark2']])
        self.assertEqual(self.installs({'steps': ['ipi', 'tee']}), [])


if __name__ == '__main__':
    unittest.main()
