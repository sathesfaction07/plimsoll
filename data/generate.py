"""Regenerates data/rules.jsonl, data/observations.jsonl and data/incidents.jsonl.

Kept in the repo so the corpus is reproducible and its planted failures are auditable. Every
rule below exists to exercise one named weakness of rolling-window alerting.
"""

import json
import random
from pathlib import Path

rng = random.Random(31)
PERIODS = 52

rules: list[dict] = []
observations: list[dict] = []
incidents: list[dict] = []


def add_rule(rule_id: str, metric: str, direction: str = "up",
             fixed: float | None = None) -> None:
    rules.append({"rule_id": rule_id, "metric": metric, "direction": direction,
                  "fixed_threshold": fixed})


def emit(rule_id: str, values: dict[int, float]) -> None:
    for period in sorted(values):
        observations.append({"rule_id": rule_id, "period": period,
                             "value": round(values[period], 4)})


def add_incident(rule_id: str, first: int, last: int, kind: str) -> None:
    incidents.append({"rule_id": rule_id, "first_period": first,
                      "last_period": last, "kind": kind})


def noisy(level: float, spread: float, periods=range(PERIODS)) -> dict[int, float]:
    return {p: max(0.0, rng.gauss(level, spread)) for p in periods}


# 1. A quiet rule with no incidents. Any alert here is a false positive, so this is what makes
#    the false-alarm rate measurable rather than theoretical.
add_rule("r-quiet", "false_negative_rate", fixed=0.14)
emit("r-quiet", noisy(0.10, 0.012))

# 2. A single spike. Every detector should catch this; it is the easy case, and it also
#    contaminates the windows that follow it -- which is how sigma gets desensitised.
add_rule("r-spike", "false_negative_rate", fixed=0.14)
spike = noisy(0.10, 0.010)
spike[24] = 0.22
emit("r-spike", spike)
add_incident("r-spike", 24, 24, "spike")

# 3. A SUSTAINED STEP. The centrepiece: a permanent 60% degradation from period 20 onward.
#    A rolling window fires for a period or two and then treats the new level as normal, so the
#    remaining 30 periods of genuine degradation pass in silence. That silence is what ABSORBED
#    exists to label.
add_rule("r-step", "false_negative_rate", fixed=0.14)
step = noisy(0.10, 0.008)
for p in range(20, PERIODS):
    step[p] = max(0.0, rng.gauss(0.16, 0.008))
emit("r-step", step)
add_incident("r-step", 20, PERIODS - 1, "step")

# 4. Gradual drift. No single period is a jump, so a rolling baseline follows it up and never
#    fires at all -- while a fixed threshold catches it the moment the level crosses.
add_rule("r-drift", "false_negative_rate", fixed=0.14)
drift = noisy(0.09, 0.007)
for p in range(26, PERIODS):
    drift[p] = max(0.0, rng.gauss(0.09 + 0.004 * (p - 25), 0.007))
emit("r-drift", drift)
add_incident("r-drift", 34, PERIODS - 1, "drift")

# 5. DELAYED DATA. Nine periods never arrive, and a genuine spike sits inside the gap. No
#    detector can catch it -- the point is that the report must say BLIND rather than implying
#    health, and that recall must not be blamed for a pipeline failure.
add_rule("r-delayed", "false_negative_rate", fixed=0.14)
delayed = noisy(0.11, 0.011)
for p in range(30, 39):
    delayed.pop(p, None)
emit("r-delayed", delayed)
add_incident("r-delayed", 32, 35, "spike")

# 6. A PERFECTLY FLAT metric that then moves. Zero variance means the sigma threshold collapses
#    onto the mean, so a one-unit change is infinitely many sigmas -- DEGENERATE rather than a
#    confident alert.
add_rule("r-flat", "open_appeals", fixed=None)
flat = dict.fromkeys(range(PERIODS), 40.0)
for p in range(41, PERIODS):
    flat[p] = 43.0
emit("r-flat", flat)
add_incident("r-flat", 41, PERIODS - 1, "step")

# 7. Strong weekly seasonality on a 4-period cycle, with no incident at all. The seasonal peaks
#    are what make a fixed threshold produce false alarms, which is the spec's complaint.
add_rule("r-seasonal", "queue_depth", fixed=150.0)
seasonal = {}
for p in range(PERIODS):
    peak = 60.0 if p % 4 == 3 else 0.0
    seasonal[p] = max(0.0, rng.gauss(110.0 + peak, 6.0))
emit("r-seasonal", seasonal)

# 8. A rule watched in the DOWN direction, so the threshold sits below the centre. Without a
#    direction the detector would need two tails and double its false-alarm rate.
add_rule("r-throughput", "cases_closed", direction="down", fixed=60.0)
throughput = noisy(100.0, 6.0)
for p in range(44, 49):
    throughput[p] = max(0.0, rng.gauss(70.0, 5.0))
emit("r-throughput", throughput)
add_incident("r-throughput", 44, 48, "step")

# 9. A SHORT series: only 5 periods, so most of it cannot support a baseline at all.
add_rule("r-new", "false_negative_rate", fixed=0.14)
emit("r-new", noisy(0.10, 0.01, periods=range(5)))

# 10. A rule with a DUPLICATE observation, which the loader must report rather than absorb --
#     the surviving value is arbitrary and so is any threshold built from it.
add_rule("r-dup", "false_negative_rate", fixed=0.14)
dup = noisy(0.10, 0.01, periods=range(20))
emit("r-dup", dup)
observations.append({"rule_id": "r-dup", "period": 7, "value": 0.19})

# 11-26. Filler rules with no incidents, so the fleet is large enough for the multiple-
#     comparisons arithmetic to bite: 26 rules x 52 periods at k=2 expects ~31 false alarms.
for index in range(16):
    rule_id = f"r-fill-{index:02d}"
    add_rule(rule_id, "false_negative_rate", fixed=0.14)
    emit(rule_id, noisy(0.10, 0.012))

# 27. An observation for a rule that is NOT in the rule list. Reported, not dropped: it means
#     either the rule list or the metric feed is stale.
observations.append({"rule_id": "r-unknown", "period": 3, "value": 0.11})

rng.shuffle(observations)

root = Path(__file__).parent
for name, rows in [("rules", rules), ("observations", observations),
                   ("incidents", incidents)]:
    with (root / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

print(f"rules.jsonl         {len(rules)} rules")
print(f"observations.jsonl  {len(observations)} rows")
print(f"incidents.jsonl     {len(incidents)} incidents")
for incident in incidents:
    length = incident["last_period"] - incident["first_period"] + 1
    print(f"  {incident['rule_id']:<14} {incident['kind']:<6} "
          f"periods {incident['first_period']}-{incident['last_period']} ({length})")
