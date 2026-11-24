import os
import subprocess
import sys
import tempfile
import unittest

from alarmtree import Engine, Rule, NO_DATA
import bench
import sim

HOT = dict(name="inlet_hot", metric="inlet_c", op=">", limit=32.0)
LOWV = dict(name="voltage_low", metric="voltage", op="<", limit=180,
            upstream=True)
PARENTS = {"pdu": "ups", "r1": "pdu", "r2": "pdu"}


def feed(eng, rows):
    out = []
    for row in rows:
        out += eng.feed(*row)
    return out


class Thresholds(unittest.TestCase):
    def test_plain_threshold_pages_on_every_crossing(self):
        eng = Engine([Rule(**HOT)])
        rows = [(t * 10, "r1", "inlet_c", v)
                for t, v in enumerate([31.9, 32.1, 31.9, 32.1, 31.9, 32.2])]
        self.assertEqual(len(feed(eng, rows)), 3)

    def test_hysteresis_pages_once(self):
        eng = Engine([Rule(clear=30.0, **HOT)])
        rows = [(t * 10, "r1", "inlet_c", v)
                for t, v in enumerate([31.9, 32.1, 31.9, 32.1, 31.9, 32.2])]
        self.assertEqual(len(feed(eng, rows)), 1)
        feed(eng, [(60, "r1", "inlet_c", 29.5)])
        self.assertEqual(eng.active(), [])

    def test_on_delay_ignores_single_spike(self):
        eng = Engine([Rule(clear=30.0, on_delay=20, **HOT)])
        rows = [(0, "r1", "inlet_c", 24.0), (10, "r1", "inlet_c", 85.0),
                (20, "r1", "inlet_c", 24.0), (30, "r1", "inlet_c", 24.0)]
        self.assertEqual(feed(eng, rows), [])

    def test_on_delay_raises_after_sustained_violation(self):
        eng = Engine([Rule(clear=30.0, on_delay=20, **HOT)])
        pages = feed(eng, [(t, "r1", "inlet_c", 33.0) for t in (0, 10, 20, 30)])
        self.assertEqual([(p.ts, p.device, p.rule) for p in pages],
                         [(20, "r1", "inlet_hot")])

    def test_off_delay_keeps_alarm_through_brief_dip(self):
        eng = Engine([Rule(clear=30.0, off_delay=20, **HOT)])
        feed(eng, [(0, "r1", "inlet_c", 33.0), (10, "r1", "inlet_c", 29.0),
                   (20, "r1", "inlet_c", 33.0)])
        self.assertEqual(eng.active(), [("r1", "inlet_hot", None)])

    def test_low_side_rule(self):
        eng = Engine([Rule(clear=190, **LOWV)])
        pages = feed(eng, [(0, "pdu", "voltage", 208), (10, "pdu", "voltage", 0)])
        self.assertEqual(len(pages), 1)

    def test_bad_rule_rejected(self):
        with self.assertRaises(ValueError):
            Rule("x", "m", ">", 10, clear=11)
        with self.assertRaises(ValueError):
            Rule("x", "m", ">=", 10)


class NoData(unittest.TestCase):
    def test_silent_device_raises_and_recovers(self):
        eng = Engine([Rule(**HOT)], stale_after=30)
        feed(eng, [(0, "r1", "inlet_c", 24.0), (0, "r2", "inlet_c", 24.0)])
        pages = feed(eng, [(t, "r2", "inlet_c", 24.0) for t in range(10, 60, 10)])
        self.assertEqual([(p.device, p.rule) for p in pages], [("r1", NO_DATA)])
        feed(eng, [(60, "r1", "inlet_c", 24.0)])
        self.assertEqual(eng.active(), [])

    def test_never_seen_device_is_not_stale(self):
        eng = Engine([Rule(**HOT)], parents=PARENTS, stale_after=30)
        self.assertEqual(eng.tick(1000), [])


class Folding(unittest.TestCase):
    def engine(self, hold=30, stale_after=None):
        return Engine([Rule(clear=190, **LOWV), Rule(clear=30.0, **HOT)],
                      PARENTS, stale_after=stale_after, hold=hold)

    def warm(self, eng):
        feed(eng, [(0, d, "voltage", 208) for d in ("ups", "pdu", "r1", "r2")])

    def test_child_reporting_first_is_folded_into_parent(self):
        eng = self.engine()
        self.warm(eng)
        pages = feed(eng, [(10, "r1", "voltage", 0), (12, "ups", "voltage", 208),
                           (14, "pdu", "voltage", 0), (16, "r2", "voltage", 0)])
        self.assertEqual(pages, [])  # everything with a parent is still held
        pages = eng.tick(50)
        self.assertEqual([(p.device, p.rule) for p in pages],
                         [("pdu", "voltage_low")])
        self.assertEqual(sorted(pages[0].folded),
                         [("r1", "voltage_low"), ("r2", "voltage_low")])

    def test_without_hold_the_early_child_pages(self):
        eng = self.engine(hold=0)
        self.warm(eng)
        pages = feed(eng, [(10, "r1", "voltage", 0), (14, "pdu", "voltage", 0),
                           (16, "r2", "voltage", 0)])
        self.assertEqual([p.device for p in pages], ["r1", "pdu"])

    def test_silent_children_are_folded_too(self):
        eng = self.engine(stale_after=30)
        self.warm(eng)
        feed(eng, [(10, "pdu", "voltage", 0)])
        pages = []
        for t in range(20, 120, 10):
            pages += feed(eng, [(t, "ups", "voltage", 208), (t, "pdu", "voltage", 0)])
        self.assertEqual([p.device for p in pages], ["pdu"])
        self.assertIn(("r1", NO_DATA, "pdu"), eng.active())

    def test_fold_walks_past_healthy_parent(self):
        eng = self.engine()
        self.warm(eng)
        feed(eng, [(10, "ups", "voltage", 0), (11, "r1", "voltage", 0),
                   (12, "pdu", "voltage", 208)])
        eng.tick(60)
        self.assertEqual([p.device for p in eng.pages], ["ups"])
        self.assertEqual(eng.active(), [("r1", "voltage_low", "ups"),
                                        ("ups", "voltage_low", None)])

    def test_silent_parent_chain(self):
        eng = self.engine(stale_after=30)
        self.warm(eng)
        # ups reads 0 V, pdu goes silent, r1 reads 0 V, r2 goes silent
        feed(eng, [(t, d, "voltage", 0) for t in range(10, 120, 10)
                   for d in ("ups", "r1")])
        self.assertEqual([(p.device, p.rule) for p in eng.pages],
                         [("ups", "voltage_low")])
        self.assertEqual(eng.active(), [
            ("pdu", NO_DATA, "ups"), ("r1", "voltage_low", "pdu"),
            ("r2", NO_DATA, "pdu"), ("ups", "voltage_low", None)])

    def test_dead_then_silent_device_pages_once(self):
        eng = self.engine(stale_after=30)
        self.warm(eng)
        feed(eng, [(10, "pdu", "voltage", 0)])
        feed(eng, [(t, d, "voltage", 208) for t in range(20, 200, 10)
                   for d in ("ups", "r1", "r2")])
        self.assertEqual([(p.device, p.rule) for p in eng.pages],
                         [("pdu", "voltage_low")])
        self.assertEqual(eng.pages[0].folded, [("pdu", NO_DATA)])

    def test_sample_arriving_late_is_not_reported_stale(self):
        eng = Engine([Rule(**HOT)], stale_after=30)
        pages = feed(eng, [(0, "r1", "inlet_c", 24.0), (100, "r1", "inlet_c", 24.0)])
        self.assertEqual(pages, [])

    def test_temperature_alarm_is_never_folded(self):
        eng = self.engine()
        self.warm(eng)
        pages = feed(eng, [(10, "pdu", "voltage", 0), (50, "pdu", "voltage", 0),
                           (51, "r1", "inlet_c", 35.0)])
        self.assertEqual([(p.device, p.rule) for p in pages],
                         [("pdu", "voltage_low"), ("r1", "inlet_hot")])
        self.assertEqual(pages[1].ts, 51)  # and it is not held either

    def test_lone_child_pages_after_hold(self):
        eng = self.engine()
        self.warm(eng)
        self.assertEqual(feed(eng, [(10, "r1", "voltage", 0)]), [])
        pages = feed(eng, [(t, d, "voltage", 0 if d == "r1" else 208)
                           for t in (20, 30, 40, 50) for d in ("ups", "pdu", "r1", "r2")])
        self.assertEqual([(p.ts, p.device) for p in pages], [(40, "r1")])
        self.assertEqual(pages[0].raised_ts, 10)

    def test_child_cleared_during_hold_never_pages(self):
        eng = self.engine()
        self.warm(eng)
        feed(eng, [(10, "r1", "voltage", 0), (20, "r1", "voltage", 208)])
        self.assertEqual(eng.tick(29), [])
        self.assertEqual([p for p in eng.pages], [])

    def test_child_still_down_after_parent_recovers_pages_itself(self):
        eng = self.engine()
        self.warm(eng)
        feed(eng, [(10, "pdu", "voltage", 0), (11, "r1", "voltage", 0),
                   (12, "r2", "voltage", 0)])
        eng.tick(50)
        self.assertEqual([p.device for p in eng.pages], ["pdu"])
        feed(eng, [(100, "ups", "voltage", 208), (100, "pdu", "voltage", 208),
                   (101, "r1", "voltage", 208), (102, "r2", "voltage", 0)])
        self.assertEqual(eng.active(), [("r2", "voltage_low", None)])
        feed(eng, [(t, d, "voltage", 0 if d == "r2" else 208)
                   for t in (110, 120, 130, 140) for d in ("ups", "pdu", "r1", "r2")])
        self.assertEqual([(p.device, p.ts) for p in eng.pages],
                         [("pdu", 50), ("r2", 130)])


class Bench(unittest.TestCase):
    def test_sim_is_deterministic(self):
        a = sim.make(7, 0.75)
        b = sim.make(7, 0.75)
        self.assertEqual(a[0], b[0])
        self.assertEqual(len(a[1]), 35)

    def test_scoring_buckets(self):
        f = sim.Fault("pdu_trip", "pdu", 100, 200, 100, {"r1"})

        class P:
            def __init__(self, ts, device):
                self.ts, self.device = ts, device
        s = bench.score([P(120, "pdu"), P(130, "pdu"), P(125, "r1"),
                         P(900, "r1"), P(120, "zz")], [f])
        self.assertEqual((s["root"], s["repeat"], s["symptom"], s["false"]),
                         (1, 1, 1, 2))
        self.assertEqual(s["delays"], [20])

    def test_tree_config_pages_less_and_misses_nothing(self):
        samples, faults, parents = sim.make(11, 0.75)
        naive = bench.score(bench.run(samples, parents, "naive")[0], faults)
        tree = bench.score(bench.run(samples, parents, "tree")[0], faults)
        self.assertEqual(tree["detected"], len(faults))
        self.assertEqual(naive["detected"], len(faults))
        self.assertLess(tree["pages"] * 5, naive["pages"])
        self.assertEqual(tree["false"], 0)


class Cli(unittest.TestCase):
    def test_cli_runs_example(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as tmp:
            csv = os.path.join(tmp, "s.csv")
            with open(csv, "w") as f:
                f.write("ts,device,metric,value\n")
                for t in range(0, 100, 10):
                    v = 0 if t >= 30 else 208
                    for d in ("ups", "pdu", "r1", "r2"):
                        f.write("%d,%s,voltage,%s\n" % (t, d, 208 if d == "ups" else v))
            r = subprocess.run([sys.executable, os.path.join(here, "alarmtree.py"),
                                os.path.join(here, "example_config.json"), csv],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("pdu voltage_low folded=2", lines[0])


if __name__ == "__main__":
    unittest.main()
