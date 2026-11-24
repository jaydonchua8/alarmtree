"""alarmtree: threshold alarms for device telemetry that page once per cause.

Feed it (timestamp, device, metric, value) samples in time order. It applies
threshold rules with hysteresis and an on-delay, raises a no_data alarm for
devices that stop reporting, and uses the power topology (child -> parents) to
fold alarms that an upstream device already explains into that device's page.

A device may have more than one parent (A and B feeds). It is only explained
when every feed is explained. A parent with no alarm of its own (for example
an unmetered panel) can be inferred from its children. An overcurrent alarm on
the surviving feed of a dual-fed load is attached to the lost feed's page.

Standard library only. See README.md.
"""
import heapq
import json
import math
import sys

NO_DATA = "no_data"
INFERRED = "inferred_upstream_loss"
AUTO = "auto"


class Rule:
    """One threshold on one metric.

    op        ">" alarms when value > limit, "<" when value < limit
    clear     value the metric must get back past before the alarm clears
              (defaults to limit, which means no hysteresis)
    on_delay  seconds the condition must hold before the alarm is raised
    off_delay seconds the clear condition must hold before it clears
    upstream  True if losing the parent device would cause this alarm
              (voltage loss, not inlet temperature). Only these are folded.
    transfer  True if losing the redundant partner feed would cause this
              alarm (overcurrent on the feed that picked up the load).
    """

    def __init__(self, name, metric, op, limit, clear=None, on_delay=0,
                 off_delay=0, upstream=False, transfer=False):
        if op not in (">", "<"):
            raise ValueError("op must be '>' or '<'")
        self.name, self.metric, self.op, self.limit = name, metric, op, limit
        self.clear = limit if clear is None else clear
        if (op == ">" and self.clear > limit) or (op == "<" and self.clear < limit):
            raise ValueError("clear must be on the safe side of limit")
        if upstream and transfer:
            raise ValueError("a rule is upstream or transfer, not both")
        if on_delay < 0 or off_delay < 0:
            raise ValueError("delays must not be negative")
        self.on_delay, self.off_delay, self.upstream = on_delay, off_delay, upstream
        self.transfer = transfer

    def violated(self, v):
        return v > self.limit if self.op == ">" else v < self.limit

    def cleared(self, v):
        return v <= self.clear if self.op == ">" else v >= self.clear


class Page:
    """A notification a person would receive."""

    def __init__(self, ts, device, rule, value, raised_ts):
        self.ts, self.device, self.rule = ts, device, rule
        self.value, self.raised_ts = value, raised_ts
        self.folded = []  # (device, rule) alarms explained by this page
        self.consequences = []  # (device, rule) transfer alarms it caused
        self.at_risk = []  # dual-fed devices left on one feed when it was sent
        self.evidence = None  # (children in alarm, children) for inferred pages

    def __repr__(self):
        return "Page(t=%s %s %s folded=%d)" % (self.ts, self.device, self.rule,
                                               len(self.folded))


class _Alarm:
    __slots__ = ("device", "rule", "upstream", "transfer", "status", "since",
                 "raised_ts", "value", "clear_since", "held", "folded_into",
                 "causes", "page", "children", "consequences", "evidence")

    def __init__(self, device, rule, upstream, since, value, transfer=False):
        self.device, self.rule, self.upstream = device, rule, upstream
        self.transfer = transfer
        self.causes = ()          # every device whose alarm this one hangs on
        self.consequences = []    # (device, rule) transfer alarms attached here
        self.evidence = None
        self.status = "pending"   # pending -> active -> (removed)
        self.since, self.value = since, value
        self.raised_ts = None
        self.clear_since = None
        self.held = False         # waiting out the hold window
        self.folded_into = None   # ancestor device that explains it
        self.page = None
        self.children = []        # (device, rule) alarms folded into this one


class Engine:
    def __init__(self, rules, parents=None, stale_after=None, hold=0,
                 infer_frac=None, infer_min=3, stale_polls=None):
        """parents: dict child -> parent, or child -> list of parents for a
        device with redundant feeds. Pass None to disable folding.
        stale_after: seconds without a sample before no_data is raised.
        stale_polls: if set, a device is stale after this many of its own
        observed poll intervals (never sooner than stale_after).
        hold: seconds an alarm that an upstream device could explain waits
        before paging, so that device has time to raise first. "auto" derives
        the wait per alarm from observed poll intervals.
        infer_frac: if set, a parent with no alarm of its own gets an
        inferred alarm once this fraction (and at least infer_min) of its
        single-parent children are in an upstream-type alarm."""
        if hold != AUTO and (not isinstance(hold, (int, float)) or hold < 0):
            raise ValueError("hold must be a number >= 0 or 'auto'")
        if infer_frac is not None and not 0 < infer_frac <= 1:
            raise ValueError("infer_frac must be in (0, 1]")
        self.by_metric = {}
        self.max_on = self.max_off = 0
        for r in rules:
            self.by_metric.setdefault(r.metric, []).append(r)
            self.max_off = max(self.max_off, r.off_delay)
            if r.upstream:
                self.max_on = max(self.max_on, r.on_delay)
        self.fold = parents is not None
        self.parents = {}      # child -> tuple of parents
        self.children = {}     # parent -> list of children
        for child, ps in (parents or {}).items():
            ps = (ps,) if isinstance(ps, str) else tuple(ps)
            if not ps:
                continue
            if child in ps or len(set(ps)) != len(ps):
                raise ValueError("bad parents for %r" % (child,))
            self.parents[child] = ps
            for p in ps:
                self.children.setdefault(p, []).append(child)
        self._check_acyclic()
        self.stale_after = stale_after
        self.stale_polls = stale_polls
        self.hold = hold
        self.holding = hold == AUTO or hold > 0
        self.adaptive = stale_polls is not None or hold == AUTO
        self.infer_frac, self.infer_min = infer_frac, infer_min
        self.alarms = {}       # (device, rule name) -> _Alarm
        self.by_device = {}    # device -> set of rule names with active upstream alarms
        self.folded = {}       # cause device -> [_Alarm]
        self.last_seen = {}
        self.gaps = {}         # device -> last few gaps between polls
        self.interval = {}     # device -> median of those gaps
        self.stale_thr = {}    # device -> seconds, only when stale_polls is set
        self.heap = []         # (release_ts, seq, _Alarm)
        self.seq = 0
        self.next_tick = None
        self.pages = []
        self.updates = []      # (ts, device, rule, cause device) for transfer alarms
        for child, ps in self.parents.items():
            self.last_seen.setdefault(child, None)
            for p in ps:
                self.last_seen.setdefault(p, None)

    def _check_acyclic(self):
        state = {}
        for start in self.parents:
            stack = [(start, iter(self.parents.get(start, ())))]
            if state.get(start) == 2:
                continue
            state[start] = 1
            while stack:
                node, it = stack[-1]
                for p in it:
                    if state.get(p) == 1:
                        raise ValueError("topology has a cycle through %r" % (p,))
                    if state.get(p) is None:
                        state[p] = 1
                        stack.append((p, iter(self.parents.get(p, ()))))
                        break
                else:
                    state[node] = 2
                    stack.pop()

    # -- public ---------------------------------------------------------
    def feed(self, ts, device, metric, value):
        """Process one sample. Returns the pages it caused (maybe none)."""
        if value is None or value != value:
            return []  # a null or NaN reading is not a sample
        out = []
        if self.next_tick is None:
            self.next_tick = ts
        if self.adaptive:
            prev = self.last_seen.get(device)
            if prev is not None and ts - prev > 0.5:  # same poll if closer
                g = self.gaps.setdefault(device, [])
                g.append(ts - prev)
                if len(g) > 5:
                    del g[0]
                est = sorted(g)[len(g) // 2]
                self.interval[device] = est
                if self.stale_polls is not None:
                    self.stale_thr[device] = max(self.stale_after or 0,
                                                 self.stale_polls * est)
        self.last_seen[device] = ts
        if ts >= self.next_tick:
            self.tick(ts, out)
        a = self.alarms.get((device, NO_DATA))
        if a is not None:
            self._clear(a, ts, out)
        for rule in self.by_metric.get(metric, ()):
            self._apply(rule, ts, device, value, out)
        self.pages.extend(out)
        return out

    def tick(self, now, out=None):
        """Advance time without a sample: stale checks and hold releases."""
        own = out is None
        if own:
            out = []
        self.next_tick = now + 1
        if self.stale_after is not None or self.stale_polls is not None:
            thr_of, default = self.stale_thr.get, self.stale_after
            if self.stale_polls is not None and default is not None:
                # No interval estimate yet (fewer than two polls seen): be
                # patient instead of assuming the device polls fast.
                default = default * self.stale_polls
            for device, seen in self.last_seen.items():
                if seen is None:
                    continue
                thr = thr_of(device, default)
                if thr is None:
                    continue
                if now - seen > thr and (device, NO_DATA) not in self.alarms:
                    a = _Alarm(device, NO_DATA, True, seen, None)
                    self.alarms[(device, NO_DATA)] = a
                    self._raise(a, now, out)
        while self.heap and self.heap[0][0] <= now:
            _, _, a = heapq.heappop(self.heap)
            if a.held and self.alarms.get((a.device, a.rule)) is a:
                a.held = False
                self._release(a, now, out)
        if own:
            self.pages.extend(out)
        return out

    def active(self):
        """Currently active alarms as (device, rule, folded_into) tuples."""
        return sorted((a.device, a.rule, a.folded_into)
                      for a in self.alarms.values() if a.status == "active")

    def at_risk(self, device):
        """Dual-fed devices that lost a feed through `device` (which is down
        or explained by something that is) and still have another live feed,
        so they are running without redundancy."""
        out, seen, stack = set(), set(), [device]
        while stack:
            d = stack.pop()
            d_down = self._explains(d) is not None
            for c in self.children.get(d, ()):
                ps = self.parents[c]
                if len(ps) > 1 and d_down and any(
                        self._explains(p) is None for p in ps):
                    out.add(c)
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return sorted(out)

    def explained(self, page):
        """Everything currently hanging on a page, flattened through
        intermediate devices: (folded alarms, transfer consequences)."""
        folded, cons, stack, seen = [], [], [page.device], set()
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            for name in self.by_device.get(d, ()):
                a = self.alarms[(d, name)]
                cons.extend(a.consequences)
                for key in a.children:
                    if key in self.alarms:
                        folded.append(key)
                        stack.append(key[0])
        return sorted(set(folded) - {(page.device, page.rule)}), sorted(set(cons))

    # -- internals ------------------------------------------------------
    def _apply(self, rule, ts, device, value, out):
        key = (device, rule.name)
        a = self.alarms.get(key)
        if a is None:
            if rule.violated(value):
                a = _Alarm(device, rule.name, rule.upstream, ts, value, rule.transfer)
                self.alarms[key] = a
                if rule.on_delay <= 0:
                    self._raise(a, ts, out)
            return
        a.value = value
        if a.status == "pending":
            if not rule.violated(value):
                del self.alarms[key]
            elif ts - a.since >= rule.on_delay:
                self._raise(a, ts, out)
            return
        if rule.cleared(value):
            if a.clear_since is None:
                a.clear_since = ts
            if ts - a.clear_since >= rule.off_delay:
                self._clear(a, ts, out)
        else:
            a.clear_since = None

    def _raise(self, a, ts, out):
        a.status, a.raised_ts = "active", ts
        if a.upstream:
            self.by_device.setdefault(a.device, set()).add(a.rule)
        if self.fold and self.holding and (
                (a.upstream and a.device in self.parents)
                or (a.transfer and self._partners(a.device))):
            self._hold(a, self._deadline(a, ts))
        else:
            self._release(a, ts, out)

    def _hold(self, a, until):
        a.held = True
        self.seq += 1
        heapq.heappush(self.heap, (until, self.seq, a))

    def _deadline(self, a, ts):
        """When a held alarm stops waiting for an upstream explanation."""
        if self.hold != AUTO:
            return ts + self.hold
        # A device that could explain this alarm raises, counted from the
        # start of the fault, either no_data (its stale threshold plus one
        # tick) or a threshold alarm (first bad sample within one poll, then
        # the on-delay, which is only checked on a sample, so one more poll).
        # A threshold alarm here started at or after the fault. A no_data
        # alarm started before it: by up to one poll, or two if a poll was
        # dropped just before the fault, which is as far as this allows for.
        iv = self.interval.get
        own = iv(a.device, 0.0)
        refs = self._partners(a.device) if a.transfer else self.parents[a.device]
        wait = 0.0
        for p in refs:
            pi, src = iv(p), p
            if pi is None:      # never seen (unmetered): its other children
                pi, src = own, a.device  # have to raise instead, assume like us
            thr = self.stale_thr.get(src, self.stale_after) or 0.0
            wait = max(wait, thr + 1, 2 * pi + self.max_on)
        lead = 2 * own if a.rule == NO_DATA else 0.0
        return max(ts, a.since + lead + wait + 2)

    def _rehold(self, a, ts, out):
        """An alarm lost its explanation. Give it time to clear by itself."""
        a.folded_into, a.causes = None, ()
        if not self.holding:
            self._release(a, ts, out)
        elif self.hold == AUTO:
            # First clean sample within one poll (two if one is dropped),
            # then the off-delay, which is only checked on a sample.
            self._hold(a, ts + 3 * self.interval.get(a.device, 0.0) + self.max_off + 2)
        else:
            self._hold(a, ts + self.hold)

    def _explains(self, device):
        """The device itself if it has an active upstream-type alarm, else
        whatever explains it, else None."""
        if self.by_device.get(device):
            return device
        return self._cause(device)

    def _cause(self, device, _depth=0):
        """Nearest ancestor with an active upstream-type alarm. With several
        parents every one of them must be explained, otherwise None."""
        ps = self.parents.get(device)
        if not ps or _depth > 64:
            return None
        first = None
        for p in ps:
            c = p if self.by_device.get(p) else self._cause(p, _depth + 1)
            if c is None:
                return None
            if first is None:
                first = c
        return first

    def _causes(self, device):
        out = []
        for p in self.parents.get(device, ()):
            c = self._explains(p)
            if c is not None and c not in out:
                out.append(c)
        return tuple(out)

    def _partners(self, device):
        """Other feeds of the dual-fed devices this device feeds."""
        out = []
        for c in self.children.get(device, ()):
            ps = self.parents[c]
            if len(ps) > 1:
                out.extend(p for p in ps if p != device and p not in out)
        return out

    def _twin(self, a):
        """Another upstream-type alarm on the same device that already went
        out (paged or folded upward), e.g. 0 V followed by no_data."""
        for name in self.by_device.get(a.device, ()):
            b = self.alarms[(a.device, name)]
            if b is not a and not b.held and b.folded_into != a.device:
                return b
        return None

    def _top(self, device):
        return self.alarms[(device, sorted(self.by_device[device])[0])]

    def _count(self, parent):
        """(children in an upstream-type alarm, children that could be)."""
        k = n = 0
        for c in self.children.get(parent, ()):
            if len(self.parents[c]) != 1:
                continue
            if self.last_seen.get(c) is None and c not in self.children:
                continue  # a leaf that has never reported cannot vote
            n += 1
            if self.by_device.get(c):
                k += 1
        return k, n

    def _need(self, n):
        return max(self.infer_min, int(math.ceil(self.infer_frac * n - 1e-9)))

    def _infer(self, a, ts):
        """Create an inferred alarm on a's parent if enough siblings agree.
        Returns the parent, or None. The caller releases the new alarm."""
        ps = self.parents.get(a.device, ())
        if self.infer_frac is None or len(ps) != 1:
            return None
        p = ps[0]
        k, n = self._count(p)
        if k < self._need(n):
            return None
        b = _Alarm(p, INFERRED, True, a.since, None)
        b.status, b.raised_ts, b.evidence = "active", ts, (k, n)
        self.alarms[(p, INFERRED)] = b
        self.by_device.setdefault(p, set()).add(INFERRED)
        return p

    def _release(self, a, ts, out):
        inferred = None
        if self.fold and a.upstream:
            twin = self._twin(a)
            cause = a.device if twin else self._cause(a.device)
            if cause is None and not twin:
                cause = inferred = self._infer(a, ts)
            if cause is not None:
                a.folded_into = cause
                a.causes = (cause,) if twin else (self._causes(a.device) or (cause,))
                for c in a.causes:
                    self.folded.setdefault(c, []).append(a)
                top = twin or self._top(cause)
                top.children.append((a.device, a.rule))
                if inferred is not None:
                    b = self.alarms[(inferred, INFERRED)]
                    # If the inferred device hangs off a device that has never
                    # reported, its siblings need time to be inferred too.
                    gp = self.parents.get(inferred, ())
                    if self.holding and gp and any(
                            self.last_seen.get(g) is None for g in gp):
                        self._hold(b, self._deadline(b, ts) if self.hold == AUTO
                                   else ts + self.hold)
                    else:
                        self._release(b, ts, out)
                return
        elif self.fold and a.transfer:
            for q in self._partners(a.device):
                cause = self._explains(q)
                if cause is not None:
                    a.folded_into, a.causes = cause, (cause,)
                    self.folded.setdefault(cause, []).append(a)
                    self._top(cause).consequences.append((a.device, a.rule))
                    self.updates.append((ts, a.device, a.rule, cause))
                    return
        a.page = Page(ts, a.device, a.rule, a.value, a.raised_ts)
        a.page.folded = a.children  # shared, so later folds show up on the page
        a.page.consequences = a.consequences
        a.page.evidence = a.evidence
        if a.upstream and self.fold:
            a.page.at_risk = self.at_risk(a.device)
        out.append(a.page)

    def _clear(self, a, ts, out):
        del self.alarms[(a.device, a.rule)]
        a.held = False
        if a.upstream and a.status == "active":
            names = self.by_device.get(a.device)
            if names:
                names.discard(a.rule)
            if not names:
                self.by_device.pop(a.device, None)
            rest = self.folded.pop(a.device, [])
            if names:
                # The device still has another upstream alarm. Keep the
                # children folded and only re-check that alarm itself.
                keep = [c for c in rest if c.device != a.device]
                if keep:
                    self.folded[a.device] = keep
                rest = [c for c in rest if c.device == a.device]
            # Alarms folded into this one lose their explanation.
            # Anything still active gets a fresh hold and may page itself.
            for child in rest:
                if self.alarms.get((child.device, child.rule)) is child \
                        and a.device in child.causes:
                    self._unlink(child, skip=a.device)
                    self._rehold(child, ts, out)
        self._unlink(a)
        if a.upstream and a.status == "active" and self.infer_frac is not None:
            # An inferred parent alarm only stands while enough children agree.
            for p in self.parents.get(a.device, ()):
                b = self.alarms.get((p, INFERRED))
                if b is not None:
                    k, n = self._count(p)
                    if k < self._need(n):
                        self._clear(b, ts, out)

    def _unlink(self, a, skip=None):
        for c in a.causes:
            if c == skip:
                continue
            lst = self.folded.get(c)
            if lst and a in lst:
                lst.remove(a)
        a.causes = ()


def load_config(path):
    """Config JSON: {"rules": [...], "parents": {...}, "stale_after": n,
    "hold": n or "auto", "infer_frac": f, "infer_min": n, "stale_polls": n}"""
    with open(path) as f:
        cfg = json.load(f)
    rules = [Rule(**r) for r in cfg["rules"]]
    return Engine(rules, cfg.get("parents"), cfg.get("stale_after"),
                  cfg.get("hold", 0), cfg.get("infer_frac"),
                  cfg.get("infer_min", 3), cfg.get("stale_polls"))


def main(argv):
    if len(argv) != 3:
        print("usage: python alarmtree.py config.json samples.csv")
        print("samples.csv lines: ts,device,metric,value (sorted by ts)")
        return 2
    eng = load_config(argv[1])
    last = None
    with open(argv[2]) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("ts,"):
                continue
            ts, device, metric, value = line.split(",")
            last = float(ts)
            for p in eng.feed(last, device, metric, float(value)):
                print(p)
    if last is not None:
        eng.stale_after = eng.stale_polls = None  # the file ended, the devices did not go quiet
        eng.stale_thr.clear()
        end = max([last] + [h[0] for h in eng.heap])
        for p in eng.tick(end + 1):
            print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
