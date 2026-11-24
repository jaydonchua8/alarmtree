"""Demo: one row of dual-fed racks, polled as Redfish resources.

  python demo.py

Synthetic. The JSON bodies below have the shape of DMTF Redfish Circuit and
Sensor resources, but no real device produced them. Takes about a second.

Topology (pdu-a has no meter, like a panel nobody put on the network):

  ups-a -> pdu-a -> rkN-a \\
                            rkN   (N = 1..4, each rack on both feeds)
  ups-b -> pdu-b -> rkN-b /

What happens:
  t=100  the breaker feeding pdu-a opens. Every a-side strip reads 0 V or
         stops answering. The b-side strips pick up the whole rack load and
         rk3-b goes past 80 % of its 32 A rating.
  t=160  rk2 develops a hotspot while it is running on one feed.
  t=300  strip rk1-b fails too, so rk1 has no power and its BMC goes quiet.
  t=420  pdu-a is re-energised.
"""
from alarmtree import Engine, Rule
import redfish

POLL = 5
RACKS = [1, 2, 3, 4]
LOAD = {1: 14.0, 2: 18.0, 3: 28.0, 4: 16.0}   # total amps per rack


def circuit(volts, amps):
    return {"@odata.type": "#Circuit.v1_7_0.Circuit", "CircuitType": "Branch",
            "RatedCurrentAmps": 32, "BreakerState": "Normal",
            "Voltage": {"Reading": volts}, "CurrentAmps": {"Reading": amps},
            "Status": {"State": "Enabled", "Health": "OK"}}


def mains(volts):
    return {"@odata.type": "#Sensor.v1_9_0.Sensor", "ReadingType": "Voltage",
            "ReadingUnits": "V", "Reading": volts,
            "Status": {"State": "Enabled", "Health": "OK"}}


def inlet(celsius):
    return {"@odata.type": "#Sensor.v1_9_0.Sensor", "ReadingType": "Temperature",
            "ReadingUnits": "Cel", "PhysicalContext": "Intake",
            "Reading": celsius,
            "Thresholds": {"UpperCaution": {"Reading": 30},
                           "UpperCritical": {"Reading": 32, "DwellTime": "PT10S",
                                             "HysteresisReading": -2}},
            "Status": {"State": "Enabled", "Health": "OK"}}


def poll(t):
    """Every (device, Redfish body) the poller would get back at time t."""
    a_dead = 100 <= t < 420
    out = [("ups-a", mains(208.0)), ("ups-b", mains(208.0)),
           ("pdu-b", mains(208.0))]
    for n in RACKS:
        b_dead = n == 1 and t >= 300
        a_up, b_up = not a_dead, not b_dead
        share_a = LOAD[n] / (2 if b_up else 1)
        share_b = LOAD[n] / (2 if a_up else 1)
        if a_up:
            out.append(("rk%d-a" % n, circuit(208, share_a)))
        elif n % 2:            # odd strips keep answering and read 0 V
            out.append(("rk%d-a" % n, circuit(0, 0)))
        if b_up:               # rk1-b just stops answering when it fails
            out.append(("rk%d-b" % n, circuit(208, share_b)))
        if a_up or b_up:       # the BMC needs one live feed
            temp = 24.0
            if n == 2 and t >= 160:
                temp = min(36.0, 24.0 + (t - 160) * 0.2)
            out.append(("rk%d" % n, inlet(temp)))
    return out


def topology():
    parents = {"pdu-a": "ups-a", "pdu-b": "ups-b"}
    for n in RACKS:
        parents["rk%d-a" % n] = "pdu-a"
        parents["rk%d-b" % n] = "pdu-b"
        parents["rk%d" % n] = ["rk%d-a" % n, "rk%d-b" % n]
    return parents


def build_rules():
    # Limits the devices publish themselves, plus one site rule for voltage,
    # because a Circuit resource does not carry a low-voltage threshold.
    found = [redfish.rules(body, circuit_delay=10) for _, body in poll(0)]
    site = [Rule("voltage_low", "voltage", "<", 180, 190, 10, 10, upstream=True)]
    return redfish.merge_rules(found + [site])


CHECKPOINTS = {290: "t=290, before the second failure",
               410: "t=410, before pdu-a comes back"}


def run(engine, until=520, at_checkpoint=None):
    for t in range(0, until, POLL):
        if at_checkpoint and t in CHECKPOINTS:
            at_checkpoint(t)
        for i, (device, body) in enumerate(poll(t)):
            ts = t + i * 0.01
            for sample in redfish.samples(ts, device, body):
                for page in engine.feed(*sample):
                    yield page
    for page in engine.tick(until):
        yield page


def describe(eng, page):
    folded, cons = eng.explained(page)
    lines = ["  %s %s (paged t=%.0f)" % (page.device, page.rule, page.ts)]
    if page.evidence:
        lines.append("    inferred from %d of %d children, the device itself "
                     "reports nothing" % page.evidence)
    if folded:
        lines.append("    explains: " + ", ".join("%s %s" % k for k in folded))
    risk = eng.at_risk(page.device)
    if risk:
        lines.append("    running on one feed: " + ", ".join(risk))
    if cons:
        lines.append("    overloaded by the transfer: " + ", ".join(
            "%s %s" % k for k in cons))
    return lines


def main():
    rules = build_rules()
    print("rules built from the Redfish bodies plus one site rule:")
    for r in sorted(rules, key=lambda r: r.name):
        print("  %-16s %-16s %s %-5g clear %-6.4g on-delay %2gs%s%s" % (
            r.name, r.metric, r.op, r.limit, r.clear, r.on_delay,
            "  upstream" if r.upstream else "", "  transfer" if r.transfer else ""))

    flat = Engine(rules, None, stale_after=15)
    n_flat = sum(1 for _ in run(flat))
    print()
    print("same rules with no topology: %d pages" % n_flat)
    print()
    print("with topology, dual feeds, inference and transfer folding:")

    eng = Engine(rules, topology(), stale_after=15, stale_polls=3, hold="auto",
                 infer_frac=0.5, infer_min=3)
    pages = []

    def checkpoint(t):
        print("open pages at %s:" % CHECKPOINTS[t])
        for p in pages:
            if (p.device, p.rule) in eng.alarms:
                print("\n".join(describe(eng, p)))

    for page in run(eng, at_checkpoint=checkpoint):
        pages.append(page)
        print("PAGE t=%-4.0f %s %s" % (page.ts, page.device, page.rule))
    print()
    print("%d pages instead of %d" % (len(pages), n_flat))
    return pages, n_flat


if __name__ == "__main__":
    main()
