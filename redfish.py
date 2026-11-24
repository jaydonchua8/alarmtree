"""Turn Redfish JSON resources into alarmtree samples and rules.

Redfish is the DMTF's public REST/JSON management standard. This module reads
the resource shapes defined by these public schemas, and nothing else:

  Sensor   Reading, ReadingType, Thresholds{UpperCritical{Reading, DwellTime,
           HysteresisReading}, ...}, Status{State}
  Circuit  Voltage{Reading}, CurrentAmps{Reading}, PowerWatts{Reading},
           RatedCurrentAmps, BreakerState      (PDU branches and mains)
  Thermal  Temperatures[]{MemberId, Name, ReadingCelsius,
           UpperThresholdCritical, LowerThresholdCritical}  (older BMCs)
  Power    Voltages[]{MemberId, Name, ReadingVolts, UpperThresholdCritical,
           LowerThresholdCritical}                          (older BMCs)

It does no network I/O. A poller hands it a parsed response body, the device
name the alarm engine should use and the poll timestamp.
"""
import re

from alarmtree import Rule

# Sensor.ReadingType -> metric name used in rules
SENSOR_METRIC = {"Temperature": "temp_c", "Voltage": "voltage",
                 "Current": "amps", "Power": "watts", "Humidity": "humidity_pct"}
# Threshold names I map to pages. Caution and User thresholds are ignored.
UPPER = ("UpperCritical", ">")
LOWER = ("LowerCritical", "<")
OFFLINE = ("Absent", "UnavailableOffline", "Disabled")

_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


class RedfishError(ValueError):
    pass


def duration_s(text):
    """ISO 8601 duration as used by DwellTime ("PT30S", "PT1M30S") -> seconds."""
    if text is None:
        return 0.0
    m = _DUR.match(text) if isinstance(text, str) else None
    if not m or text in ("P", "PT"):
        raise RedfishError("bad duration %r" % (text,))
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _num(v):
    """A usable reading, or None. Redfish sends null for 'cannot read'."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


def _kind(body):
    if not isinstance(body, dict):
        raise RedfishError("resource is not a JSON object")
    t = body.get("@odata.type")
    if not isinstance(t, str) or not t.startswith("#"):
        raise RedfishError("resource has no @odata.type")
    return t[1:].split(".")[0]


def _offline(obj):
    st = obj.get("Status")
    return isinstance(st, dict) and st.get("State") in OFFLINE


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def samples(ts, device, body):
    """One resource -> list of (ts, device, metric, value).

    Null readings and sensors whose Status.State says they are offline give
    no sample at all. That is deliberate: the engine then sees the device go
    quiet and raises no_data, instead of comparing a made-up 0 to a limit."""
    kind = _kind(body)
    out = []

    def add(metric, v):
        v = _num(v)
        if v is not None:
            out.append((ts, device, metric, v))

    if kind == "Sensor":
        metric = SENSOR_METRIC.get(body.get("ReadingType"))
        if metric and not _offline(body):
            add(metric, body.get("Reading"))
    elif kind == "Circuit":
        if not _offline(body):
            for prop, metric in (("Voltage", "voltage"), ("CurrentAmps", "amps"),
                                 ("PowerWatts", "watts")):
                ex = body.get(prop)
                if isinstance(ex, dict):
                    add(metric, ex.get("Reading"))
            state = body.get("BreakerState")
            if state is not None:
                add("breaker_tripped", 1.0 if state == "Tripped" else 0.0)
    elif kind == "Thermal":
        for t in body.get("Temperatures") or ():
            if isinstance(t, dict) and not _offline(t):
                add("temp_c:" + _slug(t.get("MemberId", t.get("Name", ""))),
                    t.get("ReadingCelsius"))
    elif kind == "Power":
        for v in body.get("Voltages") or ():
            if isinstance(v, dict) and not _offline(v):
                add("voltage:" + _slug(v.get("MemberId", v.get("Name", ""))),
                    v.get("ReadingVolts"))
    else:
        raise RedfishError("unsupported resource type %s" % kind)
    return out


def rules(body, upstream_low_voltage=True, derate=0.8, circuit_delay=0.0):
    """Rules from the limits the device itself publishes.

    Sensor: UpperCritical / LowerCritical become rules. DwellTime becomes the
    on-delay and HysteresisReading (an offset from the threshold) becomes the
    clear value.
    Circuit: RatedCurrentAmps * derate becomes an overcurrent rule marked
    transfer=True, since a circuit goes over when its partner feed is lost.
    The Circuit resource carries no dwell time, so circuit_delay supplies it.
    Thermal / Power: UpperThresholdCritical / LowerThresholdCritical.
    A low-voltage rule is marked upstream=True when upstream_low_voltage."""
    kind = _kind(body)
    out = []

    def rule(metric, op, limit, clear=None, on_delay=0.0):
        limit = _num(limit)
        if limit is None:
            return
        low_v = op == "<" and metric.split(":")[0] == "voltage"
        name = "%s_%s" % (_slug(metric), "high" if op == ">" else "low")
        out.append(Rule(name, metric, op, limit, clear, on_delay, on_delay,
                        upstream=low_v and upstream_low_voltage))

    if kind == "Sensor":
        metric = SENSOR_METRIC.get(body.get("ReadingType"))
        th = body.get("Thresholds")
        if metric and isinstance(th, dict):
            for (key, op) in (UPPER, LOWER):
                t = th.get(key)
                if not isinstance(t, dict) or _num(t.get("Reading")) is None:
                    continue
                limit = float(t["Reading"])
                hyst = _num(t.get("HysteresisReading")) or 0.0
                # The schema's example is -2 on an upper threshold. Take the
                # magnitude so a positive offset cannot put clear past limit.
                clear = limit - abs(hyst) if op == ">" else limit + abs(hyst)
                rule(metric, op, limit, clear, duration_s(t.get("DwellTime")))
    elif kind == "Circuit":
        rated = _num(body.get("RatedCurrentAmps"))
        if rated is not None and rated > 0:
            if not 0 < derate <= 1:
                raise RedfishError("derate must be in (0, 1]")
            lim = rated * derate
            out.append(Rule("overcurrent", "amps", ">", lim, lim * 0.94,
                            circuit_delay, circuit_delay, transfer=True))
        # upstream=True so that a tripped breaker explains what it feeds.
        out.append(Rule("breaker_tripped", "breaker_tripped", ">", 0.5,
                        upstream=True))
    elif kind == "Thermal":
        for t in body.get("Temperatures") or ():
            if isinstance(t, dict):
                m = "temp_c:" + _slug(t.get("MemberId", t.get("Name", "")))
                rule(m, ">", t.get("UpperThresholdCritical"))
                rule(m, "<", t.get("LowerThresholdCritical"))
    elif kind == "Power":
        for v in body.get("Voltages") or ():
            if isinstance(v, dict):
                m = "voltage:" + _slug(v.get("MemberId", v.get("Name", "")))
                rule(m, ">", v.get("UpperThresholdCritical"))
                rule(m, "<", v.get("LowerThresholdCritical"))
    else:
        raise RedfishError("unsupported resource type %s" % kind)
    return out


def merge_rules(rule_lists):
    """Many devices publish the same limits. Keep one rule per name, and
    refuse two devices that disagree, because rules here are fleet wide."""
    seen = {}
    for rs in rule_lists:
        for r in rs:
            key = (r.metric, r.op, r.limit, r.clear, r.on_delay, r.upstream, r.transfer)
            old = seen.get(r.name)
            if old is None:
                seen[r.name] = (key, r)
            elif old[0] != key:
                raise RedfishError("devices disagree on rule %s" % r.name)
    return [r for _, r in seen.values()]
