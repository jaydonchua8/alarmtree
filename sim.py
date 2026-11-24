"""Synthetic data hall telemetry with labelled faults, for bench.py.

Everything here is made up by a random generator. Nothing is measured.

make():      single-feed tree, UPS -> PDU -> rack, fixed 10 s polling.
make_dual(): A and B feeds. UPS -> PDU -> rack power strip on each side, and
             every rack hangs off one strip per side. Some PDUs have no meter,
             polls are jittered and sometimes dropped, a dead feed moves its
             load onto the other strip, and faults are allowed to overlap.

Every device is polled at its own phase, so a rack can report a power loss
before its PDU does.
"""
import random

POLL = 10


class Fault:
    def __init__(self, kind, root, start, end, onset, affected):
        self.kind, self.root, self.start, self.end = kind, root, start, end
        self.onset = onset          # first moment a threshold is truly crossed
        self.affected = affected    # devices downstream of root
        self.consequences = set()   # strips expected to go overcurrent from load transfer


def topology(n_ups=4, pdus_per_ups=8, racks_per_pdu=10):
    parents, kinds = {}, {}
    for u in range(n_ups):
        ups = "ups%d" % u
        kinds[ups] = "ups"
        for p in range(pdus_per_ups):
            pdu = "%s-pdu%d" % (ups, p)
            kinds[pdu] = "pdu"
            parents[pdu] = ups
            for r in range(racks_per_pdu):
                rack = "%s-r%d" % (pdu, r)
                kinds[rack] = "rack"
                parents[rack] = pdu
    return parents, kinds


def descendants(parents, root):
    kids = {}
    for c, p in parents.items():
        kids.setdefault(p, []).append(c)
    out, stack = [], [root]
    while stack:
        for c in kids.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def make(seed, hours=2.0, n_trip=6, n_ups_fail=1, n_hot=12, n_over=8,
         n_marginal=8, n_glitch=60):
    """Returns (samples, faults, parents). samples are time ordered tuples
    (ts, device, metric, value)."""
    rng = random.Random(seed)
    parents, kinds = topology()
    devices = sorted(kinds)
    racks = [d for d in devices if kinds[d] == "rack"]
    pdus = [d for d in devices if kinds[d] == "pdu"]
    upss = [d for d in devices if kinds[d] == "ups"]
    end_t = int(hours * 3600)
    faults = []
    eff = {}        # device -> list of (start, end, effect, arg)
    busy = set()    # devices already inside a fault, to keep labels unambiguous

    def window(lo, hi):
        dur = rng.randint(lo, hi)
        start = rng.randint(300, end_t - dur - 300)
        return start, start + dur

    def power_loss(kind, root):
        start, end = window(300, 900)
        aff = descendants(parents, root)
        busy.update(aff)
        busy.add(root)
        # In 30% of losses the root's own meter goes silent instead of reading 0 V.
        eff.setdefault(root, []).append(
            (start, end, "silent" if rng.random() < 0.3 else "dead", None))
        for d in aff:
            mode = "dead" if kinds[d] == "pdu" or rng.random() < 0.6 else "silent"
            eff.setdefault(d, []).append((start, end, mode, None))
        faults.append(Fault(kind, root, start, end, start, set(aff)))

    for root in rng.sample(upss, n_ups_fail):
        power_loss("ups_fail", root)
    free = [p for p in pdus if p not in busy]
    for root in rng.sample(free, n_trip):
        power_loss("pdu_trip", root)
    free = [r for r in racks if r not in busy]
    picks = rng.sample(free, n_hot + n_over + n_marginal)
    for rack in picks[:n_hot]:
        start, end = window(600, 1200)
        peak = rng.uniform(34.0, 38.0)
        eff.setdefault(rack, []).append((start, end, "hot", peak))
        faults.append(Fault("hotspot", rack, start, end, None, set()))
    for rack in picks[n_hot:n_hot + n_over]:
        start, end = window(300, 900)
        eff.setdefault(rack, []).append((start, end, "over", rng.uniform(27.0, 30.0)))
        faults.append(Fault("overload", rack, start, end, start, set()))
    for rack in picks[n_hot + n_over:]:
        # Inlet temperature sitting right on the limit, so noise crosses it repeatedly.
        start, end = window(900, 1500)
        eff.setdefault(rack, []).append((start, end, "marginal", 32.1))
        faults.append(Fault("marginal", rack, start, end, start, set()))
    used = set(picks) | busy
    for rack in rng.sample([r for r in racks if r not in used], n_glitch):
        # One bad reading. Not a fault, nobody should be paged.
        t = rng.randint(300, end_t - 300)
        eff.setdefault(rack, []).append(
            (t, t + POLL, "glitch", rng.choice(["inlet_c", "voltage"])))

    base = {d: (rng.uniform(10.0, 20.0), rng.uniform(21.0, 26.0)) for d in racks}
    order = sorted((rng.uniform(0, POLL - 0.01), d) for d in devices)
    samples = []
    gauss = rng.gauss
    for k in range(end_t // POLL):
        for phase, d in order:
            ts = k * POLL + phase
            kind = kinds[d]
            mode, arg, f0, f1 = None, None, 0, 0
            for (s, e, m, a) in eff.get(d, ()):
                if s <= ts < e:
                    mode, arg, f0, f1 = m, a, s, e
                    break
            if mode == "silent":
                continue
            if kind == "ups":
                samples.append((ts, d, "output_v", 0.0 if mode == "dead" else gauss(480, 3)))
                continue
            volt = 0.0 if mode == "dead" else gauss(208, 1.5)
            if kind == "pdu":
                samples.append((ts, d, "voltage", volt))
                continue
            amps, temp = base[d]
            amps = 0.0 if mode == "dead" else gauss(amps, 0.4)
            temp = gauss(temp, 0.25)
            if mode == "hot":
                ramp = 300.0
                frac = min(1.0, (ts - f0) / ramp, (f1 - ts) / ramp)
                temp += (arg - base[d][1]) * max(0.0, frac)
            elif mode == "over":
                amps = gauss(arg, 0.4)
            elif mode == "marginal":
                temp = gauss(arg, 0.25)
            elif mode == "glitch":
                if arg == "inlet_c":
                    temp = 85.0
                else:
                    volt = 0.0
            samples.append((ts, d, "voltage", volt))
            samples.append((ts, d, "amps", amps))
            samples.append((ts, d, "inlet_c", temp))
    for f in faults:
        if f.kind == "hotspot":
            # Noise-free moment the ramp passes 32 C.
            peak = [a for (s, e, m, a) in eff[f.root] if m == "hot"][0]
            b = base[f.root][1]
            f.onset = f.start + 300.0 * (32.0 - b) / (peak - b)
    return samples, faults, parents


# ---------------------------------------------------------------------------
# Dual-feed generator
# ---------------------------------------------------------------------------
AMP_LIMIT = 25.6   # 80 % of a 32 A breaker


def dual_topology(n_ups=3, pdus_per_ups=4, racks_per_pdu=8):
    """parents maps a strip to its PDU, a PDU to its UPS and a rack to the
    list [strip on side a, strip on side b]."""
    parents, kinds = {}, {}
    for u in range(n_ups):
        for side in "ab":
            ups = "ups-%s%d" % (side, u)
            kinds[ups] = "ups"
            for p in range(pdus_per_ups):
                pdu = "pdu-%s%d-%d" % (side, u, p)
                kinds[pdu] = "pdu"
                parents[pdu] = ups
        for p in range(pdus_per_ups):
            for r in range(racks_per_pdu):
                rack = "rk-%d-%d-%02d" % (u, p, r)
                kinds[rack] = "rack"
                feeds = []
                for side in "ab":
                    strip = "%s-%s" % (rack, side)
                    kinds[strip] = "strip"
                    parents[strip] = "pdu-%s%d-%d" % (side, u, p)
                    feeds.append(strip)
                parents[rack] = feeds
    return parents, kinds


def _other(name):
    """Same device on the other side (pdu-a0-1 <-> pdu-b0-1, strips too)."""
    if name.startswith(("pdu-", "ups-")):
        return name[:4] + ("b" if name[4] == "a" else "a") + name[5:]
    return name[:-1] + ("b" if name[-1] == "a" else "a")


def make_dual(seed, hours=2.0, poll=10, n_ups=3, pdus_per_ups=4,
              racks_per_pdu=8, unmetered=0.25, p_silent=0.3, p_drop=0.005,
              jitter=0.5, n_ups_fail=1, n_trip=4, n_double=1, n_strip=4,
              n_hot=8, n_over=4, n_marginal=4, n_glitch=30, n_cluster=1):
    """Returns (samples, faults, parents, info). samples are time ordered
    (ts, device, metric, value). info has "kinds" and "unmetered"."""
    rng = random.Random(seed)
    parents, kinds = dual_topology(n_ups, pdus_per_ups, racks_per_pdu)
    devices = sorted(kinds)
    racks = [d for d in devices if kinds[d] == "rack"]
    pdus = [d for d in devices if kinds[d] == "pdu"]
    upss = [d for d in devices if kinds[d] == "ups"]
    strips_of = {}
    for d in devices:
        if kinds[d] == "strip":
            strips_of.setdefault(parents[d], []).append(d)
    pdus_of = {}
    for p in pdus:
        pdus_of.setdefault(parents[p], []).append(p)
    no_meter = set(rng.sample(pdus, int(round(unmetered * len(pdus)))))
    end_t = int(hours * 3600)

    # Total rack load in amps, split evenly over both strips. Kept away from
    # the limit so that "overloaded after transfer" is not a coin flip.
    load = {}
    for r in racks:
        v = rng.uniform(12.0, 30.0)
        while AMP_LIMIT - 1.5 < v < AMP_LIMIT + 1.5:
            v = rng.uniform(12.0, 30.0)
        load[r] = v
    inlet = {r: rng.uniform(21.0, 26.0) for r in racks}

    faults = []
    loss = {}    # device -> [(start, end, mode)], mode "dead" or "silent"
    eff = {}     # rack or strip -> [(start, end, effect, arg)]
    busy = set() # PDUs (both sides of a pair) already used by a power fault

    def window(lo, hi):
        dur = rng.randint(lo, hi)
        start = rng.randint(300, end_t - dur - 300)
        return start, start + dur

    def lose(kind, root, start, end):
        if kinds[root] == "ups":
            below_pdus = list(pdus_of[root])
        elif kinds[root] == "pdu":
            below_pdus = [root]
        else:
            below_pdus = []
        strips = [s for p in below_pdus for s in strips_of[p]]
        aff = set(strips) | (set(below_pdus) - {root})
        if kinds[root] == "strip":
            strips = [root]
        mode = "silent" if rng.random() < p_silent else "dead"
        loss.setdefault(root, []).append((start, end, mode))
        for d in below_pdus:
            if d != root:
                loss.setdefault(d, []).append((start, end, "dead"))
        for s_ in strips:
            if s_ != root:
                loss.setdefault(s_, []).append(
                    (start, end, "dead" if rng.random() < 0.6 else "silent"))
        f = Fault(kind, root, start, end, start, aff)
        for s_ in strips:
            if load[s_[:-2]] > AMP_LIMIT:
                f.consequences.add(_other(s_))
        faults.append(f)
        return f, strips

    def take_pair(p):
        busy.add(p)
        busy.add(_other(p))

    for root in rng.sample(upss, n_ups_fail):
        for p in pdus_of[root]:
            take_pair(p)
        lose("ups_fail", root, *window(300, 900))
    for _ in range(n_double):
        first = rng.choice([p for p in pdus if p not in busy])
        take_pair(first)
        s1, e1 = window(500, 900)
        s2 = s1 + rng.randint(60, 200)
        e2 = s2 + rng.randint(300, 600)
        f1, st1 = lose("double_loss", first, s1, e1)
        f2, st2 = lose("double_loss", _other(first), s2, e2)
        if e2 - e1 < 60:
            # Side a is dead for (nearly) all of the second loss, so nothing
            # is left on it to overload.
            f2.consequences.clear()
        dark = {s_[:-2] for s_ in st1}
        f1.affected |= dark
        f2.affected |= dark
    for _ in range(n_trip):
        root = rng.choice([p for p in pdus if p not in busy])
        take_pair(root)
        lose("pdu_trip", root, *window(300, 900))
    dark_racks = {d for f in faults if f.kind == "double_loss"
                  for d in f.affected if kinds[d] == "rack"}
    quiet = [r for r in racks if parents[parents[r][0]] not in busy]
    # A cluster: three strips on one PDU side fail within a minute for their
    # own reasons while the PDU is fine. Three faults, three pages wanted.
    # This is what an eager "the parent must be down" inference gets wrong.
    cluster = []
    for _ in range(n_cluster):
        pdu = rng.choice(sorted({parents[parents[r][0]] for r in quiet
                                 if r not in cluster}))
        side = rng.choice("ab")
        start, end = window(400, 900)
        members = rng.sample([r for r in quiet if parents[parents[r][0]] == pdu], 3)
        for rack in members:
            cluster.append(rack)
            lose("strip_fail", "%s-%s" % (rack, side), start + rng.randint(0, 60), end)
    quiet = [r for r in quiet if r not in cluster]
    picks = rng.sample(quiet, n_strip + n_over)
    for rack in picks[:n_strip]:
        lose("strip_fail", "%s-%s" % (rack, rng.choice("ab")), *window(300, 900))
    for rack in picks[n_strip:]:
        strip = "%s-%s" % (rack, rng.choice("ab"))
        start, end = window(300, 900)
        eff.setdefault(strip, []).append((start, end, "over", rng.uniform(27.0, 30.0)))
        faults.append(Fault("overload", strip, start, end, start, set()))
    # Thermal faults may sit on top of a single-feed power loss on purpose.
    rest = [r for r in racks if r not in dark_racks and r not in picks
            and r not in cluster]
    tpicks = rng.sample(rest, n_hot + n_marginal)
    for rack in tpicks[:n_hot]:
        start, end = window(600, 1200)
        peak = rng.uniform(34.0, 38.0)
        eff.setdefault(rack, []).append((start, end, "hot", peak))
        f = Fault("hotspot", rack, start, end, None, set())
        f.onset = start + 300.0 * (32.0 - inlet[rack]) / (peak - inlet[rack])
        faults.append(f)
    for rack in tpicks[n_hot:]:
        start, end = window(900, 1500)
        eff.setdefault(rack, []).append((start, end, "marginal", 32.1))
        faults.append(Fault("marginal", rack, start, end, start, set()))
    used = set(picks) | set(tpicks) | dark_racks | set(cluster)
    for rack in rng.sample([r for r in racks if r not in used],
                           min(n_glitch, len(racks) - len(used))):
        t = rng.randint(300, end_t - 300)
        if rng.random() < 0.5:
            eff.setdefault(rack, []).append((t, t + poll, "glitch", None))
        else:
            eff.setdefault("%s-%s" % (rack, rng.choice("ab")), []).append(
                (t, t + poll, "glitch", None))

    def lost(d, ts):
        for (s, e, m) in loss.get(d, ()):
            if s <= ts < e:
                return m
        return None

    def effect(d, ts):
        for (s, e, m, a) in eff.get(d, ()):
            if s <= ts < e:
                return m, a, s, e
        return None, None, 0, 0

    order = sorted((rng.uniform(0, poll - 0.01), d) for d in devices
                   if d not in no_meter)
    samples = []
    gauss, rnd, uni = rng.gauss, rng.random, rng.uniform
    for k in range(end_t // poll):
        for phase, d in order:
            if rnd() < p_drop:
                continue
            t0 = k * poll + phase          # when the device was read
            ts = t0 + uni(0, jitter)       # when the poller stamped it
            kind = kinds[d]
            if kind == "rack":
                a, b = parents[d]
                if lost(a, t0) and lost(b, t0):
                    continue               # no power on either feed
                mode, arg, f0, f1 = effect(d, t0)
                temp = gauss(inlet[d], 0.25)
                if mode == "hot":
                    frac = min(1.0, (t0 - f0) / 300.0, (f1 - t0) / 300.0)
                    temp += (arg - inlet[d]) * max(0.0, frac)
                elif mode == "marginal":
                    temp = gauss(arg, 0.25)
                elif mode == "glitch":
                    temp = 85.0
                samples.append((ts, d, "inlet_c", temp))
                continue
            m = lost(d, t0)
            if m == "silent":
                continue
            if kind == "ups":
                samples.append((ts, d, "output_v", 0.0 if m else gauss(480, 3)))
            elif kind == "pdu":
                samples.append((ts, d, "voltage", 0.0 if m else gauss(208, 1.5)))
            else:
                rack = d[:-2]
                mode, arg, _, _ = effect(d, t0)
                if m:
                    volt, amps = 0.0, 0.0
                else:
                    volt = 0.0 if mode == "glitch" else gauss(208, 1.5)
                    share = load[rack] if lost(_other(d), t0) else load[rack] / 2
                    amps = gauss(arg if mode == "over" else share, 0.4)
                samples.append((ts, d, "voltage", volt))
                samples.append((ts, d, "amps", amps))
    samples.sort(key=lambda s_: s_[0])
    return samples, faults, parents, {"kinds": kinds, "unmetered": sorted(no_meter)}
