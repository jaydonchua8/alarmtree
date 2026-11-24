# alarmtree

Threshold alarms for data center telemetry that send one page per cause instead of one page per device, including on A/B power feeds and behind panels that have no meter.

This is an independent prototype that I wrote on my own. All benchmark data is synthetic and comes from the simulator in this repository. It is not affiliated with, connected to or reviewed by Aravolta or any other company.

## The problem

A monitoring node polls power and cooling equipment and compares readings with limits. A plain threshold check is noisy in ways that get worse as the site gets more redundant:

1. A reading that sits on its limit pages every time noise carries it across, and one bad sample pages.
2. When a PDU breaker opens, every strip behind it reads 0 V or stops answering, so one event becomes a page per device. The downstream devices often report first because every device is polled at its own phase.
3. Racks usually have two feeds. A rack that goes quiet after one feed is lost is a second problem and must not be hidden behind the first. A rack that goes quiet after both feeds are lost is not a new problem.
4. Losing a feed moves the whole rack load onto the other strip. Strips that were at 45 % go to 90 % and raise overcurrent alarms that are real, but that belong on the page for the lost feed.
5. Some upstream devices have no meter on the network at all. Nothing ever raises an alarm on them, so there is nothing to fold their children into.
6. Devices are polled at different rates and polls get dropped, so one fixed "no data after N seconds" and one fixed wait time are wrong for part of the fleet.

Version 1 of this project handled 1 and 2 on a single-parent tree. This version handles 3 to 6 and reads limits from a public device format instead of only from hand-written config.

## Why I built it with Aravolta in mind

I read Aravolta's internship posting on Y Combinator and their site. What I could verify:

- The site lists Redfish next to SNMPv3, IPMI 2.0, Modbus TCP/RTU and BACnet as supported protocols, and lists PDUs, RPPs, UPS units and server BMCs as device types (https://www.aravolta.com/).
- The site describes branch circuit monitoring with per-cabinet power tracking and automatic overage detection, alert policies with configurable thresholds, and alert delivery to PagerDuty, Slack, email and SMS (same page).
- The site describes an "Aravolta Node" that connects to IT and OT devices, and the YC company page says a single node onboards a site's assets (https://www.ycombinator.com/companies/aravolta).
- The intern posting says the work goes "deep into the weeds of data center operations" and may involve physical infrastructure at customer sites (https://www.ycombinator.com/companies/aravolta/jobs/xNqAuCd-intern).

What I do not know: how their alerting works internally, whether it already correlates alarms by power topology, or what their customers' topologies look like. Nothing here is based on their code, which is not public. Their stack is TypeScript and this is Python, because I wanted the logic to be readable and runnable with nothing installed.

The public interface I matched is DMTF Redfish, since they list it and its schemas are open. `redfish.py` reads `Sensor`, `Circuit`, and the older `Thermal` and `Power` resources, and builds rules from the limits a device publishes about itself: `Thresholds.UpperCritical.Reading`, `DwellTime` (an ISO 8601 duration, which becomes the on-delay), `HysteresisReading` (an offset, which becomes the clear value) and `RatedCurrentAmps` on a circuit.

## What it does

- `Rule`: a limit on one metric with a separate clear value, an on-delay and an off-delay.
- `no_data`: a device that stops reporting gets an alarm. With `stale_polls=3` the threshold is three of that device's own observed poll intervals, so a 60 s poller and a 5 s poller can share one engine.
- Folding: an alarm that an upstream device explains is attached to that device's page and sends nothing.
- Dual feeds: `parents` may map a device to a list of feeds. The device is explained only when every feed is explained. A page also lists the dual-fed devices it left running on one feed.
- Inference: if a parent has no alarm of its own and at least `infer_frac` of its children (and at least `infer_min`) are in a power alarm, the engine raises `inferred_upstream_loss` on the parent and folds the children into it. This recurses, so two unmetered levels still give one page.
- Transfer: a rule marked `transfer=True` (overcurrent) on a strip whose partner feed is down is attached to the lost feed's page as a consequence. If it is still over after the feed returns, it pages.
- `hold="auto"`: how long an alarm waits for an upstream explanation is computed per alarm from observed poll intervals and the stale threshold, instead of being one number for the site.
- `redfish.samples()` and `redfish.rules()`: turn Redfish JSON bodies into samples and rules. A null reading or a sensor whose `Status.State` is `Absent` or `UnavailableOffline` produces no sample, so it becomes `no_data` and is never compared with a limit as if it were 0.

## Architecture

| file | role |
|---|---|
| `alarmtree.py` | The engine. `Engine.feed(ts, device, metric, value)` returns the pages that sample caused. State is a dict of active alarms, a heap of held alarms and per-device poll statistics. Also a small CLI. |
| `redfish.py` | Pure functions from parsed Redfish resources to samples and `Rule` objects. No network I/O. |
| `sim.py` | Synthetic telemetry with labelled faults. `make()` is the v1 single-feed tree. `make_dual()` has A/B feeds, unmetered PDUs, jittered and dropped polls, load transfer and overlapping faults. |
| `bench.py` | Scores every page against the labels, picks settings on tuning seeds and reruns them unchanged on held-out data. Writes `bench_results.json`. |
| `demo.py` | An eight-strip example fed through the Redfish adapter. |
| `test_alarmtree.py`, `test_v2.py` | 68 unit tests. |

The flow is: poller response -> `redfish.samples()` -> `Engine.feed()` -> `Page` objects with `folded`, `consequences`, `at_risk` and `evidence`.

## Key decisions and tradeoffs

**Waiting costs time, so the wait is derived and not guessed.** Folding needs the child to wait for the parent. In v1 that was a fixed 30 s that I tuned on a simulator with 10 s polls. It is wrong when polls are slower. `hold="auto"` uses a bound: counted from the start of the fault, a device that could explain an alarm raises either `no_data` (its stale threshold plus one tick) or a threshold alarm (one poll, then the on-delay, which is only checked when a sample arrives, so one more poll). A held alarm waits for the slower of those and no longer. On the tuning data this was both quieter and quicker than every fixed hold I tried (0 extra pages at 46.9 s median, against 7 at 59.3 s for hold 30 and 0 at 74.3 s for hold 45).

**All feeds must be explained.** For a dual-fed rack I fold `no_data` only if both feeds are down. The alternative, folding when any feed is down, hides a dead rack behind an unrelated feed loss, which is the more expensive mistake.

**Inference can be wrong, so it has a floor.** Three strips failing for their own reasons on one PDU look like a PDU fault if the threshold is low. The simulator includes that case on purpose. With `infer_frac=0.3` the engine merged those into one wrong page and missed 6 of 90 faults on the tuning set. With 0.5 it missed none. A never-seen leaf does not count toward the denominator, and an inferred alarm is withdrawn when its children recover, so a child that stays dead pages by itself.

**Consequences are attached, not dropped.** A transfer overload is still listed on the page and logged in `Engine.updates`. I chose not to page it separately because the action is the same as for the root page, but the list of overloaded strips is the first thing I would want to read.

**Rules are fleet wide.** `redfish.merge_rules()` refuses two devices that publish different limits under one rule name. Per-device limits would need the rule lookup keyed by device, which I have not built.

**Samples must arrive in time order.** The engine keeps no reorder buffer. `sim.py` sorts after adding jitter.

## Install and usage

Python 3.9 or newer. Nothing to install.

```
python demo.py                   # about a second
python -m unittest               # 68 tests, about 10 seconds
python bench.py --quick          # one tuning seed and one held-out seed
python bench.py                  # full run, about a minute of CPU, writes bench_results.json
python alarmtree.py example_config.json samples.csv
```

```python
from alarmtree import Engine, Rule
import redfish

rules = redfish.merge_rules(
    [redfish.rules(body, circuit_delay=10) for body in first_poll_bodies]
    + [[Rule("voltage_low", "voltage", "<", 180, 190, 10, 10, upstream=True)]])
parents = {"pdu-a": "ups-a", "pdu-b": "ups-b",
           "rk1-a": "pdu-a", "rk1-b": "pdu-b", "rk1": ["rk1-a", "rk1-b"]}
eng = Engine(rules, parents, stale_after=15, stale_polls=3, hold="auto", infer_frac=0.5)

for ts, device, body in poller:
    for sample in redfish.samples(ts, device, body):
        for page in eng.feed(*sample):
            notify(page.device, page.rule, page.folded, page.at_risk, page.consequences)
```

## A real example

`python demo.py` builds four dual-fed racks. `pdu-a` has no meter. Its breaker opens at t=100, rack 2 develops a hotspot at t=160 while on one feed, strip `rk1-b` fails at t=300 which leaves rack 1 with no power, and `pdu-a` returns at t=420. The inputs are Redfish-shaped JSON bodies written by the demo, not captured from hardware. This is the output:

```
rules built from the Redfish bodies plus one site rule:
  breaker_tripped  breaker_tripped  > 0.5   clear 0.5    on-delay  0s  upstream
  overcurrent      amps             > 25.6  clear 24.06  on-delay 10s  transfer
  temp_c_high      temp_c           > 32    clear 30     on-delay 10s
  voltage_low      voltage          < 180   clear 190    on-delay 10s  upstream

same rules with no topology: 8 pages

with topology, dual feeds, inference and transfer folding:
PAGE t=125  pdu-a inferred_upstream_loss
PAGE t=215  rk2 temp_c_high
open pages at t=290, before the second failure:
  pdu-a inferred_upstream_loss (paged t=125)
    inferred from 4 of 4 children, the device itself reports nothing
    explains: rk1-a voltage_low, rk2-a no_data, rk3-a voltage_low, rk4-a no_data
    running on one feed: rk1, rk2, rk3, rk4
    overloaded by the transfer: rk3-b overcurrent
  rk2 temp_c_high (paged t=215)
PAGE t=330  rk1-b no_data
open pages at t=410, before pdu-a comes back:
  pdu-a inferred_upstream_loss (paged t=125)
    inferred from 4 of 4 children, the device itself reports nothing
    explains: rk1 no_data, rk1-a voltage_low, rk2-a no_data, rk3-a voltage_low, rk4-a no_data
    running on one feed: rk2, rk3, rk4
    overloaded by the transfer: rk3-b overcurrent
  rk2 temp_c_high (paged t=215)
  rk1-b no_data (paged t=330)

3 pages instead of 8
```

The hotspot on rack 2 still pages although the rack sits under an open power page, and rack 1 going dark is folded only once its second feed is also explained.

## Tests and verified results

`python -m unittest` runs 68 tests: the 24 from v1 unchanged, and 44 new ones for dual feeds, inference, transfer, adaptive staleness, the derived hold, the Redfish adapter (including null readings, offline sensors, malformed bodies and bad durations), the scorer and the demo.

`python bench.py` output, copied from `bench_results.json`. Every number below is from synthetic data.

**Part 1, the v1 benchmark, unchanged.** Single feed, 356 devices, 3 seeds of 2 h, 2,114,483 samples, 105 faults. The v2 engine reproduces the v1 numbers exactly.

| config | pages | detected | symptom | repeat | false | median delay, power faults |
|---|---|---|---|---|---|---|
| naive threshold | 1350 | 105/105 | 444 | 621 | 180 | 6.5 s |
| hysteresis + 20 s on-delay | 549 | 105/105 | 444 | 0 | 0 | 24.4 s |
| v1 tree, hold 30 | 105 | 105/105 | 0 | 0 | 0 | 55.1 s |

**Part 2, dual feed.** I picked `hold`, `infer_frac` and `stale_polls` from 30 candidates on the tuning set and then ran the picked settings (`hold=auto, infer_frac=0.5, stale_polls=3`) on held-out data. The held-out data uses different seeds and different generator settings: 15 s polls instead of 10 s, 464 devices instead of 318, 40 % of PDUs unmetered instead of 25 %, 2 % of polls dropped instead of 0.5 %, 2 s of timestamp jitter instead of 0.5 s.

A page is counted as *root* (first page on the device at the root of a labelled fault), *symptom* (a device downstream of a fault), *consequence* (a strip overloaded by load transfer), *repeat* or *false*.

Tuning set, seeds 1 to 3, 1,069,755 samples, 90 faults:

| config | pages | detected | symptom | consequence | repeat | false | median delay, power faults |
|---|---|---|---|---|---|---|---|
| naive threshold | 842 | 83/90 | 279 | 66 | 324 | 90 | 6.8 s |
| hysteresis + on-delay | 429 | 83/90 | 279 | 65 | 0 | 2 | 29.5 s |
| v1 tree, hold 30 | 213 | 83/90 | 65 | 65 | 0 | 0 | 60.0 s |
| v2 picked | 90 | 90/90 | 0 | 0 | 0 | 0 | 46.9 s |

Held-out set, seeds 201 to 203, 1,030,899 samples, 120 faults:

| config | pages | detected | symptom | consequence | repeat | false | median delay, power faults |
|---|---|---|---|---|---|---|---|
| naive threshold | 4633 | 110/120 | 476 | 97 | 308 | 3642 | 17.4 s |
| hysteresis + on-delay | 4241 | 110/120 | 471 | 96 | 34 | 3530 | 31.1 s |
| v1 tree, hold 30 | 386 | 110/120 | 150 | 73 | 0 | 53 | 61.6 s |
| v2 picked | 121 | 120/120 | 0 | 0 | 0 | 1 | 67.1 s |
| v2 with single-parent topology | 198 | 120/120 | 4 | 73 | 0 | 1 | 67.1 s |
| v2 without inference | 231 | 110/120 | 120 | 0 | 0 | 1 | 68.5 s |
| v2 with fixed stale_after | 172 | 120/120 | 0 | 0 | 0 | 52 | 67.1 s |

On the held-out set v2 attached 73 of 73 expected transfer overloads to the lost feed's page.

How to read this honestly:

- The faults the first three rows miss are trips on unmetered PDUs. Nothing pages on the PDU, so the scorer counts the fault as not detected even though its strips paged.
- Most of the false pages in the first two held-out rows are `no_data` flapping, because `stale_after=30` was carried over unchanged to a site that polls every 15 s and drops 2 % of polls. An operator would retune that number. The fairer comparisons are v1 (386 pages) and the three ablation rows.
- v2 is slower on power faults on the held-out set (67.1 s against 61.6 s for v1), because the derived hold stretches with the 15 s poll. That is the price of the 150 symptom pages it removes.
- On the tuning set, fixed and adaptive staleness tie and so do `infer_frac` 0.5 and 0.8, because that data cannot tell them apart. I broke ties toward the setting with fewer site constants and toward the default. I decided that after a debugging run on seed 101 with the held-out generator settings showed fixed staleness flapping. I also fixed several timing bugs in the hold logic against that seed and against tuning seed 1. For that reason the reported held-out seeds are 201 to 203, which I ran once at the end and did not inspect page by page.
- I added the clustered strip failures to the generator after the first grid could not separate the `infer_frac` values. Without them 0.3 looks as good as 0.5.
- Part 1 throughput for the tree config was 613,807 samples/s on one core in the final run and 252,829 in an earlier one on the same shared machine, so I only read it as "not the bottleneck". I did not measure v2 throughput separately.

## Limitations and next steps

- All data is synthetic and I wrote both the simulator and the engine. The simulator only contains failure modes I thought of. The 120/120 says the engine handles those, not that it handles a real site.
- I have not run this against a real Redfish service. The adapter follows the published DMTF schemas, and I confirmed the property names for `Sensor` and `Circuit` against the schema files, but the `BreakerState` value `"Tripped"` and the legacy `Thermal` and `Power` field names are from my reading of the spec and are covered only by my own fixtures. Real BMCs deviate from the schema.
- Only Redfish is adapted. SNMP, Modbus and BACnet, which matter more for PDUs and building equipment, are not.
- The derived hold tolerates one dropped poll. Two drops in a row at the wrong moment can still leak a page; I saw one such page on the debugging seed.
- Releasing a held alarm early when its parent reports healthy after the fault began would cut the delay for isolated faults. I left it out because it fights with inference, which needs siblings to arrive first.
- Rules are fleet wide, state is in memory and lost on restart, and there is no reorder buffer.
- The topology has to be given. Aravolta's site mentions LLDP-based network discovery, but a power topology would have to come from a DCIM model, and a wrong edge here produces a wrong fold.
- Next: per-device limits, persisting alarm state, an SNMP adapter, and replaying a real export of historical readings if I can get one.
