"""Tests for dual feeds, inferred parents, transfer folding, adaptive
staleness, the derived hold and the Redfish adapter."""
import io
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from alarmtree import Engine, Rule, NO_DATA, INFERRED
import bench
import demo
import redfish
import sim

LOWV = Rule("voltage_low", "voltage", "<", 180, 190, upstream=True)
OVER = Rule("overcurrent", "amps", ">", 25.6, 24.0, transfer=True)
HOT = Rule("inlet_hot", "inlet_c", ">", 32.0, 30.0)
DUAL = {"pa": "ua", "pb": "ub", "sa": "pa", "sb": "pb", "rk": ["sa", "sb"]}


def feed(eng, rows):
    out = []
    for row in rows:
        out += eng.feed(*row)
    return out


def keys(pages):
    return [(p.device, p.rule) for p in pages]


def all_ok(t, skip=(), devices=("ua", "ub", "pa", "pb", "sa", "sb")):
    rows = [(t, d, "voltage", 208.0) for d in devices if d not in skip]
    if "rk" not in skip:
        rows.append((t, "rk", "inlet_c", 24.0))
    return rows


class Validation(unittest.TestCase):
    def test_rule_cannot_be_upstream_and_transfer(self):
        with self.assertRaises(ValueError):
            Rule("x", "amps", ">", 1, upstream=True, transfer=True)

    def test_negative_delay_rejected(self):
        with self.assertRaises(ValueError):
            Rule("x", "amps", ">", 1, on_delay=-1)

    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):
            Engine([LOWV], {"a": "b", "b": "c", "c": "a"})

    def test_self_parent_and_duplicate_feed_rejected(self):
        with self.assertRaises(ValueError):
            Engine([LOWV], {"a": "a"})
        with self.assertRaises(ValueError):
            Engine([LOWV], {"a": ["b", "b"]})

    def test_bad_hold_and_infer_frac_rejected(self):
        for kw in (dict(hold=-1), dict(hold="soon"), dict(infer_frac=0),
                   dict(infer_frac=1.5)):
            with self.assertRaises(ValueError):
                Engine([LOWV], {"a": "b"}, **kw)

    def test_diamond_topology_is_not_a_cycle(self):
        Engine([LOWV], {"rk": ["sa", "sb"], "sa": "ups", "sb": "ups"})

    def test_null_and_nan_readings_are_ignored(self):
        eng = Engine([LOWV])
        self.assertEqual(eng.feed(0, "a", "voltage", None), [])
        self.assertEqual(eng.feed(1, "a", "voltage", float("nan")), [])
        self.assertEqual(eng.active(), [])
        self.assertIsNone(eng.last_seen.get("a"))


class DualFeed(unittest.TestCase):
    def eng(self, **kw):
        return Engine([LOWV, OVER, HOT], DUAL, stale_after=30, hold=30, **kw)

    def test_rack_silent_with_one_feed_down_pages_itself(self):
        eng = self.eng()
        feed(eng, all_ok(0) + all_ok(10))
        # pa dies, sa follows, the rack goes quiet although sb is fine.
        pages = []
        for t in range(20, 140, 10):
            pages += feed(eng, [(t, "pa", "voltage", 0.0), (t, "sa", "voltage", 0.0)]
                          + all_ok(t, skip=("pa", "sa", "rk")))
        self.assertEqual(keys(pages), [("pa", "voltage_low"), ("rk", NO_DATA)])
        self.assertEqual(pages[0].folded, [("sa", "voltage_low")])

    def test_rack_silent_with_both_feeds_down_is_folded(self):
        eng = self.eng()
        feed(eng, all_ok(0) + all_ok(10))
        pages = []
        for t in range(20, 140, 10):
            pages += feed(eng, [(t, d, "voltage", 0.0) for d in ("pa", "sa", "pb", "sb")]
                          + all_ok(t, skip=("pa", "sa", "pb", "sb", "rk")))
        self.assertEqual(sorted(keys(pages)),
                         [("pa", "voltage_low"), ("pb", "voltage_low")])
        # Folded into the nearest explained device on its first feed.
        self.assertIn(("rk", NO_DATA, "sa"), eng.active())

    def test_rack_still_dark_after_one_feed_returns_pages(self):
        eng = self.eng()
        feed(eng, all_ok(0) + all_ok(10))
        for t in range(20, 100, 10):
            feed(eng, [(t, d, "voltage", 0.0) for d in ("pa", "sa", "pb", "sb")]
                 + all_ok(t, skip=("pa", "sa", "pb", "sb", "rk")))
        pages = []
        for t in range(100, 200, 10):   # side b comes back, the rack does not
            pages += feed(eng, [(t, d, "voltage", 0.0) for d in ("pa", "sa")]
                          + all_ok(t, skip=("pa", "sa", "rk")))
        self.assertEqual(keys(pages), [("rk", NO_DATA)])

    def test_page_lists_devices_left_on_one_feed(self):
        eng = self.eng()
        feed(eng, all_ok(0) + all_ok(10))
        pages = []
        for t in range(20, 80, 10):
            pages += feed(eng, [(t, "pa", "voltage", 0.0), (t, "sa", "voltage", 0.0)]
                          + all_ok(t, skip=("pa", "sa")))
        self.assertEqual(keys(pages), [("pa", "voltage_low")])
        self.assertEqual(pages[0].at_risk, ["rk"])
        self.assertEqual(eng.at_risk("pa"), ["rk"])
        self.assertEqual(eng.at_risk("ub"), [])

    def test_explained_flattens_through_intermediate_devices(self):
        eng = self.eng()
        feed(eng, all_ok(0) + all_ok(10))
        pages = []
        for t in (20, 30, 40, 50, 60):
            pages += feed(eng, [(t, d, "voltage", 0.0) for d in ("ua", "pa", "sa")]
                          + all_ok(t, skip=("ua", "pa", "sa")))
        self.assertEqual(keys(pages), [("ua", "voltage_low")])
        folded, cons = eng.explained(pages[0])
        self.assertEqual(folded, [("pa", "voltage_low"), ("sa", "voltage_low")])
        self.assertEqual(cons, [])


class Transfer(unittest.TestCase):
    def eng(self):
        return Engine([LOWV, OVER], DUAL, stale_after=30, hold=30)

    def rows(self, t, sa_v, sb_amps):
        return [(t, "ua", "voltage", 208.0), (t, "ub", "voltage", 208.0),
                (t, "pa", "voltage", 208.0), (t, "pb", "voltage", 208.0),
                (t, "sa", "voltage", sa_v), (t, "sb", "voltage", 208.0),
                (t, "sb", "amps", sb_amps), (t, "rk", "voltage", 208.0)]

    def test_overload_on_surviving_feed_is_attached_not_paged(self):
        eng = self.eng()
        pages = feed(eng, self.rows(0, 208.0, 14.0))
        for t in range(10, 100, 10):
            pages += feed(eng, self.rows(t, 0.0, 28.0))
        self.assertEqual(keys(pages), [("sa", "voltage_low")])
        self.assertEqual(pages[0].consequences, [("sb", "overcurrent")])
        self.assertEqual([(u[1], u[2], u[3]) for u in eng.updates],
                         [("sb", "overcurrent", "sa")])

    def test_overload_with_healthy_partner_pages_after_hold(self):
        eng = self.eng()
        pages = feed(eng, self.rows(0, 208.0, 14.0))
        for t in range(10, 100, 10):
            pages += feed(eng, self.rows(t, 208.0, 28.0))
        self.assertEqual(keys(pages), [("sb", "overcurrent")])
        self.assertEqual(pages[0].raised_ts, 10)
        self.assertEqual(pages[0].ts, 40)
        self.assertEqual(eng.updates, [])

    def test_overload_that_outlives_the_lost_feed_pages(self):
        eng = self.eng()
        feed(eng, self.rows(0, 208.0, 14.0))
        for t in range(10, 100, 10):
            feed(eng, self.rows(t, 0.0, 28.0))
        pages = []
        for t in range(100, 200, 10):  # sa is back, sb is still over
            pages += feed(eng, self.rows(t, 208.0, 28.0))
        self.assertEqual(keys(pages), [("sb", "overcurrent")])

    def test_transfer_rule_without_topology_just_pages(self):
        eng = Engine([OVER], None)
        self.assertEqual(keys(feed(eng, [(0, "sb", "amps", 28.0)])),
                         [("sb", "overcurrent")])


class Inference(unittest.TestCase):
    PARENTS = {"panel": "ups", "r1": "panel", "r2": "panel", "r3": "panel",
               "r4": "panel"}

    def eng(self, parents=None, **kw):
        kw.setdefault("infer_frac", 0.5)
        return Engine([LOWV], parents or self.PARENTS, stale_after=30, hold=30, **kw)

    def run_rows(self, eng, dead, t0=10, t1=100, ups=208.0, racks=("r1", "r2", "r3", "r4")):
        pages = feed(eng, [(0, "ups", "voltage", 208.0)]
                     + [(0, r, "voltage", 208.0) for r in racks])
        for t in range(t0, t1, 10):
            pages += feed(eng, [(t, "ups", "voltage", ups)] + [
                (t, r, "voltage", 0.0 if r in dead else 208.0) for r in racks])
        return pages

    def test_unmetered_parent_is_inferred_from_children(self):
        eng = self.eng()
        pages = self.run_rows(eng, dead=("r1", "r2", "r3"))
        self.assertEqual(keys(pages), [("panel", INFERRED)])
        self.assertEqual(pages[0].evidence, (3, 4))
        self.assertEqual(sorted(pages[0].folded),
                         [("r%d" % i, "voltage_low") for i in (1, 2, 3)])

    def test_too_few_children_page_individually(self):
        eng = self.eng()
        pages = self.run_rows(eng, dead=("r1", "r2"))
        self.assertEqual(sorted(keys(pages)),
                         [("r1", "voltage_low"), ("r2", "voltage_low")])

    def test_inference_off_by_default(self):
        eng = self.eng(infer_frac=None)
        pages = self.run_rows(eng, dead=("r1", "r2", "r3", "r4"))
        self.assertEqual(len(pages), 4)

    def test_no_inference_when_a_real_alarm_further_up_explains_it(self):
        eng = self.eng()
        pages = self.run_rows(eng, dead=("r1", "r2", "r3", "r4"), ups=0.0)
        self.assertEqual(keys(pages), [("ups", "voltage_low")])
        self.assertNotIn(("panel", INFERRED), eng.alarms)
        self.assertIn(("r1", "voltage_low", "ups"), eng.active())

    def test_inferred_alarm_clears_and_stuck_child_pages_itself(self):
        eng = self.eng()
        self.run_rows(eng, dead=("r1", "r2", "r3", "r4"))
        pages = []
        for t in range(100, 200, 10):   # everything but r4 recovers
            pages += feed(eng, [(t, "ups", "voltage", 208.0)] + [
                (t, r, "voltage", 0.0 if r == "r4" else 208.0)
                for r in ("r1", "r2", "r3", "r4")])
        self.assertEqual(keys(pages), [("r4", "voltage_low")])
        self.assertNotIn(("panel", INFERRED), eng.alarms)

    def test_two_unmetered_levels_give_one_page(self):
        parents = {"p1": "bus", "p2": "bus", "p3": "bus"}
        racks = []
        for p in ("p1", "p2", "p3"):
            for i in range(3):
                parents["%s-r%d" % (p, i)] = p
                racks.append("%s-r%d" % (p, i))
        eng = Engine([LOWV], parents, stale_after=30, hold=30, infer_frac=0.5)
        pages = feed(eng, [(0, r, "voltage", 208.0) for r in racks])
        for t in range(10, 150, 10):
            pages += feed(eng, [(t + i * 0.1, r, "voltage", 0.0)
                                for i, r in enumerate(racks)])
        self.assertEqual(keys(pages), [("bus", INFERRED)])
        self.assertEqual(pages[0].evidence, (3, 3))

    def test_never_seen_leaf_does_not_count_against_inference(self):
        parents = dict(self.PARENTS, ghost1="panel", ghost2="panel", ghost3="panel",
                       ghost4="panel")
        eng = self.eng(parents)
        pages = self.run_rows(eng, dead=("r1", "r2", "r3"))
        self.assertEqual(keys(pages), [("panel", INFERRED)])
        self.assertEqual(pages[0].evidence, (3, 4))


class Staleness(unittest.TestCase):
    def test_slow_poller_is_not_stale_with_stale_polls(self):
        fixed = Engine([LOWV], None, stale_after=30)
        adapt = Engine([LOWV], None, stale_after=30, stale_polls=3)
        rows = []
        for t in range(0, 600, 10):
            rows.append((t, "fast", "voltage", 208.0))
            if t % 60 == 0:
                rows.append((t, "slow", "voltage", 208.0))
        self.assertGreater(len(feed(fixed, rows)), 5)
        self.assertEqual(feed(adapt, rows), [])

    def test_slow_poller_is_still_caught_when_it_stops(self):
        eng = Engine([LOWV], None, stale_after=30, stale_polls=3)
        rows = [(t, "fast", "voltage", 208.0) for t in range(0, 900, 10)]
        rows += [(t, "slow", "voltage", 208.0) for t in range(0, 301, 60)]
        pages = feed(eng, sorted(rows))
        self.assertEqual(keys(pages), [("slow", NO_DATA)])
        self.assertGreater(pages[0].ts, 300 + 180)
        self.assertLess(pages[0].ts, 300 + 200)

    def test_no_estimate_yet_means_patience(self):
        eng = Engine([LOWV], None, stale_after=30, stale_polls=3)
        rows = [(0, "slow", "voltage", 208.0)]
        rows += [(t, "fast", "voltage", 208.0) for t in range(0, 80, 10)]
        self.assertEqual(feed(eng, sorted(rows)), [])
        self.assertEqual(keys(feed(eng, [(t, "fast", "voltage", 208.0)
                                         for t in range(80, 120, 10)])),
                         [("slow", NO_DATA)])

    def test_same_poll_metrics_do_not_shrink_the_interval(self):
        eng = Engine([LOWV, OVER], None, stale_after=30, stale_polls=3)
        for t in range(0, 100, 20):
            eng.feed(t, "s", "voltage", 208.0)
            eng.feed(t + 0.2, "s", "amps", 10.0)
        self.assertAlmostEqual(eng.interval["s"], 20, delta=0.5)


class AutoHold(unittest.TestCase):
    PARENTS = {"pdu": "ups", "r1": "pdu", "r2": "pdu"}

    def scenario(self, hold, poll):
        """The PDU's meter goes silent and its racks read 0 V."""
        on = Rule("voltage_low", "voltage", "<", 180, 190, 20, 20, upstream=True)
        eng = Engine([on], self.PARENTS, stale_after=30, stale_polls=3, hold=hold)
        rows = []
        for k in range(40):
            t = k * poll
            rows.append((t + 3, "ups", "voltage", 208.0))
            if t < 5 * poll:
                rows.append((t + 4, "pdu", "voltage", 208.0))
            for i, r in enumerate(("r1", "r2")):
                rows.append((t + i, r, "voltage", 208.0 if t < 5 * poll else 0.0))
        return keys(feed(eng, sorted(rows)))

    def test_fixed_hold_tuned_for_fast_polls_leaks_on_slow_polls(self):
        self.assertEqual(self.scenario(30, 10), [("pdu", NO_DATA)])
        leaked = self.scenario(30, 30)
        self.assertIn(("r1", "voltage_low"), leaked)

    def test_auto_hold_follows_the_poll_interval(self):
        self.assertEqual(self.scenario("auto", 10), [("pdu", NO_DATA)])
        self.assertEqual(self.scenario("auto", 30), [("pdu", NO_DATA)])

    def test_auto_hold_releases_a_lone_child(self):
        on = Rule("voltage_low", "voltage", "<", 180, 190, 20, 20, upstream=True)
        eng = Engine([on], self.PARENTS, stale_after=30, stale_polls=3, hold="auto")
        rows = []
        for t in range(0, 300, 10):
            rows += [(t, "ups", "voltage", 208.0), (t + 1, "pdu", "voltage", 208.0),
                     (t + 2, "r1", "voltage", 0.0 if t >= 50 else 208.0),
                     (t + 3, "r2", "voltage", 208.0)]
        pages = feed(eng, rows)
        self.assertEqual(keys(pages), [("r1", "voltage_low")])
        self.assertLess(pages[0].ts - 52, 60)


def sensor(reading, **thresholds):
    return {"@odata.type": "#Sensor.v1_9_0.Sensor", "ReadingType": "Temperature",
            "Reading": reading, "Thresholds": thresholds,
            "Status": {"State": "Enabled"}}


class Redfish(unittest.TestCase):
    def test_sensor_sample_and_rule(self):
        body = sensor(31.5, UpperCritical={"Reading": 32, "DwellTime": "PT20S",
                                           "HysteresisReading": -2},
                      UpperCaution={"Reading": 30})
        self.assertEqual(redfish.samples(7, "rk", body), [(7, "rk", "temp_c", 31.5)])
        (r,) = redfish.rules(body)
        self.assertEqual((r.metric, r.op, r.limit, r.clear, r.on_delay),
                         ("temp_c", ">", 32.0, 30.0, 20.0))

    def test_positive_hysteresis_offset_still_clears_on_the_safe_side(self):
        (r,) = redfish.rules(sensor(20, UpperCritical={"Reading": 32,
                                                       "HysteresisReading": 2}))
        self.assertEqual(r.clear, 30.0)
        (r,) = redfish.rules(sensor(20, LowerCritical={"Reading": 5,
                                                       "HysteresisReading": -1}))
        self.assertEqual((r.op, r.clear), ("<", 6.0))

    def test_null_reading_and_offline_sensor_give_no_sample(self):
        self.assertEqual(redfish.samples(0, "rk", sensor(None)), [])
        body = sensor(0.0)
        body["Status"]["State"] = "UnavailableOffline"
        self.assertEqual(redfish.samples(0, "rk", body), [])
        self.assertEqual(redfish.samples(0, "rk", sensor(True)), [])
        self.assertEqual(redfish.samples(0, "rk", sensor("31.5")), [])

    def test_offline_sensor_turns_into_no_data_not_a_zero_reading(self):
        low = sensor(0.0, LowerCritical={"Reading": 5})
        eng = Engine(redfish.rules(low), None, stale_after=30)
        pages = []
        for t in range(0, 100, 10):
            body = sensor(22.0 if t < 30 else None)
            for s in redfish.samples(t, "rk", body):
                pages += eng.feed(*s)
            pages += eng.feed(t, "other", "temp_c", 22.0)
        self.assertEqual(keys(pages), [("rk", NO_DATA)])

    def test_circuit(self):
        body = {"@odata.type": "#Circuit.v1_7_0.Circuit", "RatedCurrentAmps": 32,
                "BreakerState": "Tripped", "Voltage": {"Reading": 0},
                "CurrentAmps": {"Reading": None}, "PowerWatts": None}
        self.assertEqual(redfish.samples(1, "s", body),
                         [(1, "s", "voltage", 0.0), (1, "s", "breaker_tripped", 1.0)])
        rs = {r.name: r for r in redfish.rules(body, circuit_delay=10)}
        self.assertAlmostEqual(rs["overcurrent"].limit, 25.6)
        self.assertTrue(rs["overcurrent"].transfer)
        self.assertEqual(rs["overcurrent"].on_delay, 10)
        self.assertTrue(rs["breaker_tripped"].upstream)
        with self.assertRaises(redfish.RedfishError):
            redfish.rules(body, derate=0)

    def test_legacy_thermal_and_power(self):
        thermal = {"@odata.type": "#Thermal.v1_7_1.Thermal", "Temperatures": [
            {"MemberId": "0", "Name": "Inlet Temp", "ReadingCelsius": 23,
             "UpperThresholdCritical": 42, "Status": {"State": "Enabled"}},
            {"MemberId": "1", "Name": "CPU2", "ReadingCelsius": None,
             "Status": {"State": "Absent"}}]}
        self.assertEqual(redfish.samples(0, "bmc", thermal),
                         [(0, "bmc", "temp_c:0", 23.0)])
        self.assertEqual([(r.name, r.limit) for r in redfish.rules(thermal)],
                         [("temp_c_0_high", 42.0)])
        power = {"@odata.type": "#Power.v1_7_1.Power", "Voltages": [
            {"MemberId": "12V", "ReadingVolts": 12.1, "LowerThresholdCritical": 10.8}]}
        self.assertEqual(redfish.samples(0, "bmc", power),
                         [(0, "bmc", "voltage:12v", 12.1)])
        (r,) = redfish.rules(power)
        self.assertTrue(r.upstream)
        (r,) = redfish.rules(power, upstream_low_voltage=False)
        self.assertFalse(r.upstream)

    def test_malformed_resources_are_rejected(self):
        for bad in ([], "x", {}, {"@odata.type": "Sensor"},
                    {"@odata.type": "#Chassis.v1_0_0.Chassis"}):
            with self.assertRaises(redfish.RedfishError):
                redfish.samples(0, "d", bad)
        with self.assertRaises(redfish.RedfishError):
            redfish.rules({"@odata.type": "#Chassis.v1_0_0.Chassis"})

    def test_durations(self):
        self.assertEqual(redfish.duration_s("PT30S"), 30)
        self.assertEqual(redfish.duration_s("PT1M30.5S"), 90.5)
        self.assertEqual(redfish.duration_s("P1DT1H"), 90000)
        self.assertEqual(redfish.duration_s(None), 0)
        for bad in ("30", "PT", "P", "PT30", 30, "PT-5S"):
            with self.assertRaises(redfish.RedfishError):
                redfish.duration_s(bad)

    def test_merge_rules_refuses_disagreeing_devices(self):
        a = redfish.rules(sensor(1, UpperCritical={"Reading": 32}))
        b = redfish.rules(sensor(1, UpperCritical={"Reading": 35}))
        self.assertEqual(len(redfish.merge_rules([a, a])), 1)
        with self.assertRaises(redfish.RedfishError):
            redfish.merge_rules([a, b])


class DualBench(unittest.TestCase):
    def test_make_dual_is_deterministic_and_dual_fed(self):
        a = sim.make_dual(5, hours=1.0)
        b = sim.make_dual(5, hours=1.0)
        self.assertEqual(a[0], b[0])
        self.assertNotEqual(a[0], sim.make_dual(6, hours=1.0)[0])
        parents, info = a[2], a[3]
        racks = [d for d, k in info["kinds"].items() if k == "rack"]
        self.assertTrue(all(len(parents[r]) == 2 for r in racks))
        emitted = {s[1] for s in a[0]}
        self.assertTrue(info["unmetered"])
        self.assertFalse(emitted & set(info["unmetered"]))
        ts = [s[0] for s in a[0]]
        self.assertEqual(ts, sorted(ts))

    def test_scoring_counts_consequences_and_attachments(self):
        f = sim.Fault("pdu_trip", "pa", 100, 400, 100, {"sa"})
        f.consequences = {"sb"}

        class P:
            def __init__(self, ts, device):
                self.ts, self.device = ts, device
        s = bench.score([P(120, "pa"), P(130, "sa"), P(140, "sb"), P(150, "zz"),
                         P(900, "sb")], [f], [(150, "sb", "overcurrent", "pa")])
        self.assertEqual((s["root"], s["symptom"], s["consequence"], s["false"]),
                         (1, 1, 1, 2))
        self.assertEqual((s["transfer_expected"], s["transfer_attached"]), (1, 1))

    def test_v2_beats_v1_on_dual_feed_data_and_misses_nothing(self):
        samples, faults, parents, _ = sim.make_dual(8, hours=1.0)
        v1 = bench.merge([bench.score(bench.run(samples, parents, "tree")[0], faults)])
        eng, _ = bench.run_engine(samples, parents, "v2", hold="auto",
                                  infer_frac=0.5, stale_polls=3)
        v2 = bench.merge([bench.score(eng.pages, faults, eng.updates)])
        self.assertEqual(v2["detected"], v2["faults"])
        self.assertLess(v1["detected"], v1["faults"])  # a trip on an unmetered PDU
        self.assertGreater(v1["extra_pages"], 20)
        self.assertLess(v2["extra_pages"], v1["extra_pages"] / 3)
        self.assertGreater(v2["transfer_attached"], 0)


class DemoAndCli(unittest.TestCase):
    def test_demo_pages(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pages, n_flat = demo.main()
        self.assertEqual(keys(pages), [("pdu-a", INFERRED), ("rk2", "temp_c_high"),
                                       ("rk1-b", NO_DATA)])
        self.assertEqual(n_flat, 8)
        self.assertIn("overloaded by the transfer: rk3-b overcurrent", buf.getvalue())

    def test_cli_accepts_dual_feed_config(self):
        here = os.path.dirname(os.path.abspath(__file__))
        cfg = {"stale_after": 30, "stale_polls": 3, "hold": "auto", "infer_frac": 0.5,
               "parents": DUAL,
               "rules": [{"name": "voltage_low", "metric": "voltage", "op": "<",
                          "limit": 180, "clear": 190, "upstream": True}]}
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "c.json"), "w") as f:
                json.dump(cfg, f)
            with open(os.path.join(tmp, "s.csv"), "w") as f:
                f.write("ts,device,metric,value\n")
                for t in range(0, 120, 10):
                    for d in ("ua", "ub", "pa", "pb", "sa", "sb", "rk"):
                        dead = t >= 30 and d in ("pa", "sa")
                        f.write("%d,%s,voltage,%s\n" % (t, d, 0 if dead else 208))
            r = subprocess.run([sys.executable, os.path.join(here, "alarmtree.py"),
                                os.path.join(tmp, "c.json"), os.path.join(tmp, "s.csv")],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stdout)
        self.assertIn("pa voltage_low folded=1", lines[0])


if __name__ == "__main__":
    unittest.main()
