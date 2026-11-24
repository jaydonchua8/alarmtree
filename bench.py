"""Benchmarks on labelled synthetic telemetry (see sim.py, nothing is measured).

  python bench.py            full run (a few minutes), writes bench_results.json
  python bench.py --quick    one tuning seed, one held-out seed, small grid

Part 1 is the original single-feed comparison of three configurations.
Part 2 uses the dual-feed generator. Engine settings are picked on the tuning
seeds, then run unchanged on held-out data made with different seeds AND
different generator settings (poll interval, topology size, share of
unmetered PDUs, drop rate, jitter).
"""
import json
import sys
import time

from alarmtree import Engine, Rule
import sim

GRACE = 90  # seconds after a fault ends in which a page still belongs to it

TUNE_SEEDS = [1, 2, 3]
DEV_SEED = 101   # held-out generator settings, used while debugging
HELD_SEEDS = [201, 202, 203]  # never inspected page by page
TUNE_GEN = {}  # make_dual defaults: 10 s poll, 25 % unmetered PDUs
HELD_GEN = dict(poll=15, n_ups=4, pdus_per_ups=3, racks_per_pdu=12,
                unmetered=0.4, p_silent=0.5, p_drop=0.02, jitter=2.0,
                n_trip=5, n_strip=6, n_hot=10, n_over=5, n_marginal=5,
                n_glitch=40, n_cluster=2)


def rules(debounce, transfer=False):
    d = 20 if debounce else 0

    def c(v):
        return v if debounce else None
    return [
        Rule("ups_output_low", "output_v", "<", 430, c(450), d, d, upstream=True),
        Rule("voltage_low", "voltage", "<", 180, c(190), d, d, upstream=True),
        Rule("overcurrent", "amps", ">", 25.6, c(24.0), d, d, transfer=transfer),
        Rule("inlet_hot", "inlet_c", ">", 32.0, c(30.0), d, d),
    ]


def first_parent(parents):
    """What version 1 understood: one parent per device."""
    return {c: (p if isinstance(p, str) else p[0]) for c, p in parents.items()}


def build(mode, parents, hold=30, infer_frac=None, stale_polls=None):
    if mode == "naive":
        return Engine(rules(False), None, stale_after=30)
    if mode == "debounce":
        return Engine(rules(True), None, stale_after=30)
    if mode == "tree":   # version 1 behaviour
        return Engine(rules(True), first_parent(parents), stale_after=30, hold=hold)
    return Engine(rules(True, transfer=True), parents, stale_after=30, hold=hold,
                  infer_frac=infer_frac, stale_polls=stale_polls)


def run(samples, parents, mode, **kw):
    """Returns (pages, seconds)."""
    eng, dt = run_engine(samples, parents, mode, **kw)
    return eng.pages, dt


def run_engine(samples, parents, mode, **kw):
    eng = build(mode, parents, **kw)
    feed = eng.feed
    t0 = time.perf_counter()
    for ts, d, m, v in samples:
        feed(ts, d, m, v)
    eng.tick(samples[-1][0])  # faults end 300 s before the data does
    return eng, time.perf_counter() - t0


def score(pages, faults, updates=()):
    """Bucket every page. root: first page on the device at the root of a
    fault. repeat: another page for that fault. symptom: a device downstream
    of a fault. consequence: a strip overloaded by load transfer from a
    fault. false: matches nothing."""
    by_root, by_aff, by_con = {}, {}, {}
    for f in faults:
        by_root.setdefault(f.root, []).append(f)
        for d in f.affected:
            by_aff.setdefault(d, []).append(f)
        for d in f.consequences:
            by_con.setdefault(d, []).append(f)

    def inside(f, ts):
        return f.start <= ts <= f.end + GRACE
    first = {}
    out = {"pages": len(pages), "root": 0, "repeat": 0, "symptom": 0,
           "consequence": 0, "false": 0}
    for p in sorted(pages, key=lambda p: p.ts):
        hit = [f for f in by_root.get(p.device, ()) if inside(f, p.ts)]
        if hit:
            if hit[0] not in first:
                first[hit[0]] = p
                out["root"] += 1
            else:
                out["repeat"] += 1
        elif any(inside(f, p.ts) for f in by_aff.get(p.device, ())):
            out["symptom"] += 1
        elif any(inside(f, p.ts) for f in by_con.get(p.device, ())):
            out["consequence"] += 1
        else:
            out["false"] += 1
    expected = {(d, id(f)) for d, fs in by_con.items() for f in fs}
    attached = set()
    for ts, device, _rule, _cause in updates:
        for f in by_con.get(device, ()):
            if inside(f, ts):
                attached.add((device, id(f)))
    out["transfer_expected"] = len(expected)
    out["transfer_attached"] = len(attached)
    out["delays"] = sorted(first[f].ts - f.onset for f in first)
    out["power_delays"] = sorted(
        first[f].ts - f.onset for f in first
        if f.kind in ("pdu_trip", "ups_fail", "double_loss", "strip_fail"))
    out["faults"] = len(faults)
    out["detected"] = len(first)
    out["missed_kinds"] = sorted(f.kind for f in faults if f not in first)
    return out


def pct(xs, q):
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")


def merge(rows):
    tot = {}
    for r in rows:
        for k, v in r.items():
            tot[k] = tot.get(k, type(v)()) + v
    d = sorted(tot.pop("delays"))
    tot["power_median_delay_s"] = round(pct(sorted(tot.pop("power_delays")), 0.5), 1)
    tot["median_delay_s"] = round(pct(d, 0.5), 1)
    tot["p95_delay_s"] = round(pct(d, 0.95), 1)
    tot["pages_per_fault"] = round(tot["pages"] / tot["faults"], 2)
    tot["extra_pages"] = (tot["repeat"] + tot["symptom"] + tot["consequence"]
                          + tot["false"])
    tot["missed_kinds"] = sorted(tot["missed_kinds"])
    return tot


HDR = ("config", "pages", "detected", "symptom", "conseq", "repeat", "false",
       "median s", "p95 s", "power median s")


def table(rows):
    print("%-26s %6s %9s %8s %7s %7s %6s %9s %7s %15s" % HDR)
    for name, r in rows:
        print("%-26s %6d %5d/%-3d %8d %7d %7d %6d %9.1f %7.1f %15.1f" % (
            name, r["pages"], r["detected"], r["faults"], r["symptom"],
            r["consequence"], r["repeat"], r["false"], r["median_delay_s"],
            r["p95_delay_s"], r["power_median_delay_s"]))


def part1(quick):
    seeds = [1] if quick else [1, 2, 3]
    hours = 1.0 if quick else 2.0
    holds = [0, 5, 10, 20, 30, 45]
    res = {m: [] for m in ("naive", "debounce", "tree")}
    sweep = {h: [] for h in holds}
    n_samples, secs = 0, 0.0
    for seed in seeds:
        samples, faults, parents = sim.make(seed, hours)
        for mode in res:
            pages, dt = run(samples, parents, mode)
            res[mode].append(score(pages, faults))
            if mode == "tree":
                n_samples += len(samples)
                secs += dt
        for h in holds:
            pages, _ = run(samples, parents, "tree", hold=h)
            sweep[h].append(score(pages, faults))
    res = {m: merge(r) for m, r in res.items()}
    sweep = {h: merge(r) for h, r in sweep.items()}
    n_dev = len(set(parents) | set(parents.values()))
    print("PART 1: single feed. %d seeds x %.0f h, %d devices, %d samples, "
          "%d labelled faults" % (len(seeds), hours, n_dev, n_samples,
                                  res["tree"]["faults"]))
    table(res.items())
    print()
    print("hold sweep (tree config)")
    print("%6s %6s %8s %15s" % ("hold s", "pages", "symptom", "power median s"))
    for h, r in sweep.items():
        print("%6d %6d %8d %15.1f" % (h, r["pages"], r["symptom"],
                                      r["power_median_delay_s"]))
    rate = n_samples / secs
    print()
    print("tree config throughput: %.0f samples/s on one core" % rate)
    return {"configs": res, "hold_sweep": sweep, "devices": n_dev,
            "samples": n_samples, "samples_per_s": round(rate)}


def dual_set(seeds, gen):
    data, n_samples, n_dev = [], 0, 0
    for seed in seeds:
        samples, faults, parents, info = sim.make_dual(seed, **gen)
        data.append((samples, faults, parents))
        n_samples += len(samples)
        n_dev = len(info["kinds"])
    return data, n_samples, n_dev


def evaluate(data, mode, **kw):
    rows = []
    for samples, faults, parents in data:
        eng, _ = run_engine(samples, parents, mode, **kw)
        rows.append(score(eng.pages, faults, eng.updates))
    return merge(rows)


def objective(r):
    """Lower is better: miss nothing, then few extra pages, then be quick."""
    return (r["faults"] - r["detected"], r["extra_pages"])


def label(kw):
    return "hold=%s infer=%s polls=%s" % (kw["hold"], kw["infer_frac"],
                                          kw["stale_polls"])


def part2(quick):
    t_seeds = TUNE_SEEDS[:1] if quick else TUNE_SEEDS
    h_seeds = HELD_SEEDS[:1] if quick else HELD_SEEDS
    holds = [30, "auto"] if quick else [20, 30, 45, 60, "auto"]
    fracs = [0.5] if quick else [0.3, 0.5, 0.8]
    grid = [dict(hold=h, infer_frac=f, stale_polls=p)
            for h in holds for f in fracs for p in (None, 3)]
    tune, n_t, dev_t = dual_set(t_seeds, TUNE_GEN)
    held, n_h, dev_h = dual_set(h_seeds, HELD_GEN)
    tuned = [(kw, evaluate(tune, "v2", **kw)) for kw in grid]
    # Ties go to the candidate with fewer site-specific constants (adaptive
    # staleness, derived hold), then to the quicker one, then to the default
    # infer_frac of 0.5.
    tuned.sort(key=lambda x: objective(x[1]) + (
        x[0]["stale_polls"] is None, x[0]["hold"] != "auto",
        x[1]["power_median_delay_s"], x[0]["infer_frac"] != 0.5))
    best = tuned[0][0]
    out = {"grid": [{"config": label(kw), "pages": r["pages"],
                     "detected": r["detected"], "faults": r["faults"],
                     "extra_pages": r["extra_pages"],
                     "power_median_delay_s": r["power_median_delay_s"]}
                    for kw, r in tuned], "picked": label(best)}
    print()
    print("PART 2: dual feed. settings picked on the tuning set from %d "
          "candidates: %s" % (len(grid), label(best)))
    print("grid on the tuning set, best first (extra = pages that are not "
          "the first page for a fault)")
    for kw, r in tuned[:6] + tuned[-2:]:
        print("  %-34s detected %d/%d  extra %3d  power median %5.1f s" % (
            label(kw), r["detected"], r["faults"], r["extra_pages"],
            r["power_median_delay_s"]))
    ablate = [("no dual-feed rule", dict(best), True),
              ("no inference", dict(best, infer_frac=None), False),
              ("fixed stale_after", dict(best, stale_polls=None), False)]
    for name, data, n, dev, seeds in (("tuning", tune, n_t, dev_t, t_seeds),
                                      ("held-out", held, n_h, dev_h, h_seeds)):
        rows = [("naive threshold", evaluate(data, "naive")),
                ("hysteresis + on-delay", evaluate(data, "debounce")),
                ("v1 tree, hold 30", evaluate(data, "tree")),
                ("v2 picked", evaluate(data, "v2", **best))]
        for aname, kw, single in ablate:
            if single:
                d2 = [(s, f, first_parent(p)) for s, f, p in data]
                rows.append(("v2 minus: " + aname, evaluate(d2, "v2", **kw)))
            elif kw != best:
                rows.append(("v2 minus: " + aname, evaluate(data, "v2", **kw)))
        print()
        print("%s set: seeds %s, %d devices, %d samples, %d labelled faults"
              % (name, seeds, dev, n, rows[0][1]["faults"]))
        table(rows)
        v2 = rows[3][1]
        print("v2 picked: %d of %d transfer overloads attached to the lost "
              "feed's page; missed: %s" % (v2["transfer_attached"],
                                           v2["transfer_expected"],
                                           v2["missed_kinds"] or "none"))
        out[name] = {"seeds": seeds, "devices": dev, "samples": n,
                     "rows": dict(rows)}
    out["held_out_generator"] = HELD_GEN
    return out


def main():
    quick = "--quick" in sys.argv
    res = {"note": "synthetic data from sim.py"}
    res["single_feed"] = part1(quick)
    res["dual_feed"] = part2(quick)
    if not quick:
        with open("bench_results.json", "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
