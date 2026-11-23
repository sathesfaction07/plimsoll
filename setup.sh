#!/usr/bin/env bash
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN} ok ${NC} $1"; }
fail() { echo -e "${RED}fail${NC} $1"; exit 1; }

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'USAGE'
Usage: bash setup.sh

No options, no credentials, no network, no model. This is a small time-series problem and
the accuracy question is which threshold, which is arithmetic.
USAGE
            exit 0 ;;
        *) fail "unknown option: $arg (try --help)" ;;
    esac
done

info "Checking Python"
command -v python3 >/dev/null 2>&1 || fail "python3 not found (macOS: brew install python@3.12)"
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
[ "$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')" = "1" ] \
    || fail "Python 3.11+ required (StrEnum). Found $PY_VER."
ok "Python $PY_VER"

info "Creating virtual environment"
[ -d .venv ] || python3 -m venv .venv || fail "could not create .venv"
ok ".venv ready"

info "Installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]" || fail "pip install failed"
ok "dependencies installed"

info "Verifying imports"
.venv/bin/python - <<'PYCHECK' || fail "core imports failed"
import click                                          # noqa: F401
from pydantic_settings import BaseSettings             # noqa: F401
from rule_monitor.detect import evaluate               # noqa: F401
from rule_monitor.measure import score                 # noqa: F401
from rule_monitor.stats import mad                     # noqa: F401
PYCHECK
ok "imports resolve"

info "Deriving the MAD scaling constant rather than trusting a table"
.venv/bin/python - <<'PYCHECK' || fail "the MAD constant does not hold"
import random
import statistics

from rule_monitor.stats import MAD_SCALE, mad, stdev

rng = random.Random(11)
ratios = []
for _ in range(120):
    sample = [rng.gauss(0.0, 1.0) for _ in range(4000)]
    ratios.append(stdev(sample) / mad(sample, scaled=False))
observed = statistics.fmean(ratios)
assert abs(observed - MAD_SCALE) < 0.01, observed
print(f"     stdev / MAD over 120 normal samples = {observed:.4f}   constant = {MAD_SCALE}")
print("     Without this scaling, k=3 means one thing for sigma and another for MAD, and")
print("     comparing the two detectors would be meaningless.")
PYCHECK
ok "the MAD constant is what the docs claim"

info "Deriving the breakdown point that separates the two detectors"
.venv/bin/python - <<'PYCHECK' || fail "the breakdown behaviour is not as documented"
from rule_monitor.stats import breakdown_count, mad, stdev

clean = [100.0] * 8
print(f"     {'window':<32}{'stdev':>10}{'MAD':>10}")
print(f"     {'all 100 (clean)':<32}{stdev(clean):>10.2f}{mad(clean):>10.2f}")
for count in (1, 2, 3, 4):
    window = [100.0] * (8 - count) + [150.0] * count
    label = f"{count} of 8 contaminated"
    print(f"     {label:<32}{stdev(window):>10.2f}{mad(window):>10.2f}")

# ONE outlier moves stdev; MAD is unmoved until MORE THAN HALF the window is bad.
assert stdev([100.0] * 7 + [150.0]) > 15.0
for count in (1, 2, 3):
    assert mad([100.0] * (8 - count) + [150.0] * count) == 0.0, count
assert mad([100.0] * 4 + [150.0] * 4) > 0.0

print()
print(f"     tolerated contaminated points, window of 8:  stdev "
      f"{breakdown_count(8, robust=False)}   MAD {breakdown_count(8, robust=True)}")
print("     A detector whose baseline tolerates ZERO outliers is desensitised by the very")
print("     anomalies it exists to catch: one spike raises the bar and hides a whole band")
print("     of genuine anomalies underneath it.")
PYCHECK
ok "breakdown points behave as documented"

info "Deriving the false-alarm budget the choice of k commits you to"
.venv/bin/python - <<'PYCHECK' || fail "the budget arithmetic does not hold"
import random

from rule_monitor.detect import false_alarm_budget, normal_tail

# Exact, via erfc -- and cross-checked against simulation so the closed form is not taken
# on faith.
rng = random.Random(5)
draws = [rng.gauss(0.0, 1.0) for _ in range(200_000)]
print(f"     {'k':>5}{'exact':>11}{'simulated':>12}{'per rule/52':>13}{'40 rules/52':>13}")
for k in (2.0, 2.5, 3.0):
    exact = normal_tail(k)
    simulated = sum(1 for value in draws if value > k) / len(draws)
    assert abs(exact - simulated) < 0.003, (k, exact, simulated)
    budget = false_alarm_budget(k, rules=40, periods=52)
    print(f"     {k:>5}{exact:>10.4%}{simulated:>12.4%}"
          f"{budget.per_rule_per_year:>13.2f}{budget.fleet_total:>13.1f}")

budget = false_alarm_budget(2.0, rules=40, periods=52)
assert abs(budget.per_rule_per_year - 1.183) < 0.01
assert abs(budget.fleet_total - 47.32) < 0.05
print()
print("     At k=2 one rule expects 1.18 false alarms a year, which reads as acceptable.")
print("     Forty rules expect 47.3 -- almost one a week. Same number, multiplied.")
PYCHECK
ok "the false-alarm budget is arithmetic, and it is stated"

info "Deriving the absorption failure: a rolling baseline swallows a sustained shift"
.venv/bin/python - <<'PYCHECK' || fail "absorption does not behave as documented"
from rule_monitor.detect import evaluate_series
from rule_monitor.models import Detector, Direction, Observation, Outcome, Rule
from rule_monitor.series import Series

WINDOW = 6
values = [100.0] * 12 + [140.0] * 14          # a PERMANENT 40% degradation from period 12
series = Series("r", [
    Observation(rule_id="r", period=index, value=value)
    for index, value in enumerate(values)
])

print(f"     a permanent 40% step at period 12, window {WINDOW}")
print(f"     {'detector':<10}{'alerts':>8}{'of degraded periods':>22}   outcomes after the edge")
for detector in (Detector.SIGMA, Detector.ROBUST):
    rule = Rule(rule_id="r", metric="m", direction=Direction.UP)
    outcomes = [item.outcome for item in evaluate_series(rule, series, detector, window=WINDOW)]
    alerts = [i for i, outcome in enumerate(outcomes) if outcome.is_alert]
    tail = {outcome.value for outcome in outcomes[max(alerts) + 1:]}
    print(f"     {detector.value:<10}{len(alerts):>8}{14:>22}   {sorted(tail)}")
    assert len(alerts) < 6, (detector, len(alerts))

# A fixed threshold fires on EVERY degraded period, because its reference point is not
# computed from recent data and therefore cannot drift into the degradation.
fixed_rule = Rule(rule_id="r", metric="m", direction=Direction.UP, fixed_threshold=120.0)
fixed = [item.outcome for item in evaluate_series(fixed_rule, series, Detector.FIXED)]
fixed_alerts = sum(1 for outcome in fixed if outcome.is_alert)
assert fixed_alerts == 14, fixed_alerts
print(f"     {'fixed':<10}{fixed_alerts:>8}{14:>22}   []")

print()
print("     Robustness helps with CONTAMINATION and does nothing for ABSORPTION. Once the")
print("     window contains only the new level, the new level IS normal -- whatever")
print("     statistic summarises it. This is not a tuning problem.")

# And the remedy has a horizon: the reference window slides forward too.
long_step = [100.0] * 10 + [140.0] * 40
long_series = Series("r", [
    Observation(rule_id="r", period=index, value=value)
    for index, value in enumerate(long_step)
])
rule = Rule(rule_id="r", metric="m", direction=Direction.UP)
late = [
    item.outcome
    for item in evaluate_series(rule, long_series, Detector.SIGMA, window=WINDOW)
][-10:]
assert all(outcome is Outcome.QUIET for outcome in late), late
print()
print(f"     ABSORBED has a horizon of about {2 * WINDOW} periods. On a step 40 periods old")
print("     the reference window has itself slid into the degraded era, the shift collapses")
print("     to zero, and the outcome decays back to plain QUIET.")
print("     The only reference point that cannot drift is one not computed from recent data")
print("     at all -- which is what a fixed threshold is.")
PYCHECK
ok "absorption, and the limit of the remedy, both derived"

info "Measuring the shipped corpus"
.venv/bin/python - <<'PYCHECK' || fail "the corpus did not behave as documented"
from pathlib import Path

from rule_monitor.detect import evaluate_series
from rule_monitor.measure import (
    absorption_report, best, compare, during_incident, outcome_counts, score_by_kind,
)
from rule_monitor.measure import _incident_index
from rule_monitor.models import Detector, Outcome
from rule_monitor.series import build_series, duplicate_periods
from rule_monitor.sources import (
    load_incidents, load_observations, load_rules, orphan_observations,
)

rules, rule_problems = load_rules(Path("data/rules.jsonl"))
observations, observation_problems = load_observations(Path("data/observations.jsonl"))
incidents, incident_problems = load_incidents(Path("data/incidents.jsonl"))
assert not (rule_problems + observation_problems + incident_problems)
print(f"     {len(rules)} rules, {len(observations)} observations, {len(incidents)} incidents")

assert duplicate_periods(observations) == [("r-dup", 7)]
assert orphan_observations(observations, rules) == ["r-unknown"]
print("     the planted duplicate period and orphan rule are both reported, not absorbed")

series = build_series(observations)
gap = series["r-delayed"].missing_periods
assert gap == tuple(range(30, 39)), gap
print(f"     r-delayed is missing periods {gap[0]}-{gap[-1]}, and a real incident sits")
print("       entirely inside that gap -- so it is BLIND, never a detector failure")

evaluations = []
for rule in rules:
    found = series.get(rule.rule_id)
    if found is None:
        continue
    for detector in Detector:
        evaluations.extend(evaluate_series(rule, found, detector))

seen = {item.outcome for item in evaluations}
assert seen == set(Outcome), sorted(set(Outcome) - seen)
print(f"     every one of the {len(Outcome)} outcomes occurs, so no assertion below is vacuous")

print()
results = compare(evaluations, incidents)
print(f"     {'detector':<10}{'P':>9}{'R':>9}{'F1':>8}{'coverage':>11}{'FP':>6}")
for detector, accuracy in results.items():
    print(f"     {detector.value:<10}{accuracy.precision:>8.1%}{accuracy.recall:>9.1%}"
          f"{accuracy.f1:>8.3f}{accuracy.coverage:>11.1%}{accuracy.false_positives:>6}")

chosen = best(results)
assert chosen is Detector.FIXED, chosen
assert results[Detector.FIXED].f1 > 3 * results[Detector.SIGMA].f1
print()
print("     The FIXED threshold wins, by more than three times on F1. The brief recommended")
print("     replacing it with rolling sigma. The measurement says otherwise, and the reason")
print("     is structural: it is the only detector whose reference cannot drift.")

print()
print(f"     recall by incident shape")
kinds = sorted({incident.kind for incident in incidents})
print(f"     {'detector':<10}" + "".join(f"{kind:>10}" for kind in kinds))
for detector in Detector:
    by_kind = score_by_kind(evaluations, incidents, detector)
    print(f"     {detector.value:<10}" + "".join(
        f"{by_kind[kind].recall:>10.1%}" for kind in kinds
    ))
fixed_kinds = score_by_kind(evaluations, incidents, Detector.FIXED)
sigma_kinds = score_by_kind(evaluations, incidents, Detector.SIGMA)
assert fixed_kinds["spike"].recall == sigma_kinds["spike"].recall == 1.0
assert sigma_kinds["step"].recall < fixed_kinds["step"].recall / 3
print("     Everything catches a SPIKE. Only the fixed threshold holds on to a STEP or a")
print("     DRIFT, which are the shapes a rolling baseline follows rather than flags.")

print()
assert results[Detector.ROBUST].false_positives > results[Detector.SIGMA].false_positives
index = _incident_index(incidents)
offenders = {
    item.rule_id for item in evaluations
    if item.detector is Detector.FIXED and item.outcome.is_alert
    and during_incident(item, index) is None
}
assert offenders == {"r-seasonal"}, offenders
print(f"     ROBUST raises MORE false alarms than SIGMA "
      f"({results[Detector.ROBUST].false_positives} vs "
      f"{results[Detector.SIGMA].false_positives}), which is counter-intuitive:")
print("       on a cyclic metric most of the window sits at the trough, so MAD is SMALLER")
print("       than stdev, the threshold is TIGHTER, and every seasonal peak fires.")
print(f"     Every one of the fixed threshold's false alarms comes from {offenders} alone.")

print()
for detector in (Detector.SIGMA, Detector.ROBUST):
    rows = [row for row in absorption_report(evaluations, incidents, detector)
            if row[0] == "r-step"]
    longest = max(row[2] for row in rows)
    assert longest >= 10, (detector, longest)
    print(f"     {detector.value:<8} silent for up to {longest} consecutive periods during the")
    print(f"                permanent degradation on r-step")
fixed_rows = absorption_report(evaluations, incidents, Detector.FIXED)
print(f"     fixed    longest silent run: "
      f"{max((row[2] for row in fixed_rows), default=0)} period(s)")

degenerate = {item.rule_id for item in evaluations if item.outcome is Outcome.DEGENERATE}
assert "r-flat" in degenerate
print()
print(f"     r-flat produces DEGENERATE outcomes: a zero-spread baseline means the threshold")
print("       collapses onto the centre, so the magnitude of the alert means nothing. Flagged")
print("       rather than fudged with an epsilon, which would invent a scale the data lacks.")
PYCHECK
ok "the corpus behaves as documented"

info "Running the test suite"
.venv/bin/python -m pytest -q || fail "tests failed"
ok "tests pass"

cat <<'DONE'

Setup complete.

  source .venv/bin/activate

  plimsoll doctor
  plimsoll watch   -d sigma
  plimsoll watch   -d fixed
  plimsoll compare
  plimsoll compare -k 3.0            # see the budget shrink and the recall with it
  plimsoll explain r-step 26
  plimsoll explain r-flat 42         # a zero-spread baseline

`watch` exits non-zero on an unreadable row or a duplicated period, because both make a
threshold arbitrary. It does NOT fail on a blind period: delayed data is normal, and a check
that always fires carries no information. It says so in the output instead.

`compare` exits non-zero only when no detector has a measurable F1.
DONE
