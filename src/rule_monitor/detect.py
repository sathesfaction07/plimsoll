"""Deciding whether a value is an alert.

The structure of the problem, which the measurements forced:

1. A rolling baseline is contaminated by anomalies inside its own window. Mean and stdev have
   a 0% breakdown point, so ONE outlier raises the threshold and opens a band in which genuine
   anomalies become invisible. Median and MAD survive until half the window is bad.

2. **A rolling baseline absorbs a sustained shift, and no choice of statistic fixes that.**
   Measured on a permanent 40% step over 12 periods: mean+2sigma fired twice, median+2MAD fired
   three times, and then both were silent forever. Once the window is entirely the new level,
   the new level IS normal by construction.

So the remedy is not a better statistic. It is a second comparison against a window far enough
back to predate the shift -- which is what `ABSORBED` reports.
"""

from __future__ import annotations

import math

from rule_monitor.models import (
    Detector,
    Direction,
    Evaluation,
    FalseAlarmBudget,
    Outcome,
    Rule,
    Threshold,
)
from rule_monitor.series import Series
from rule_monitor.stats import MIN_BASELINE, breakdown_count, centre_and_spread, mean

DEFAULT_WINDOW = 6
DEFAULT_K = 2.0

SHIFT_TOLERANCE = 2.0
"""How many baseline spreads the level must move before silence is called ABSORBED.

In units of the baseline spread rather than absolute, so it travels across metrics. Set equal
to the default `k`: the point is that a shift big enough to have fired when it started is
still worth reporting once the window has swallowed it. A smaller value would relabel ordinary
drift as absorption; a larger one would let real degradations pass as QUIET.
"""


def resolve_threshold(
    rule: Rule,
    series: Series,
    period: int,
    detector: Detector,
    *,
    window: int = DEFAULT_WINDOW,
    k: float = DEFAULT_K,
) -> Threshold:
    """The threshold for one rule in one period, and the baseline behind it."""
    if detector is Detector.FIXED:
        # No baseline at all, so nothing to contaminate and nothing to absorb. `baseline_size`
        # is 0 and that is accurate rather than a placeholder.
        return Threshold(
            value=rule.fixed_threshold, centre=None, spread=None, baseline_size=0
        )

    values = series.baseline(period, window)
    if len(values) < MIN_BASELINE:
        return Threshold(
            value=None, centre=None, spread=None, baseline_size=len(values)
        )

    robust = detector is Detector.ROBUST
    centre, spread = centre_and_spread(values, robust=robust)
    offset = k * spread
    value = centre + offset if rule.direction is Direction.UP else centre - offset

    return Threshold(
        value=value,
        centre=centre,
        spread=spread,
        baseline_size=len(values),
        # Zero spread means the threshold has collapsed onto the centre, so any move at all
        # crosses it. Flagged rather than fudged with an epsilon, which would invent a scale
        # the data does not contain.
        degenerate=spread == 0.0,
        tolerated_outliers=breakdown_count(len(values), robust=robust),
    )


def _exceeds(value: float, threshold: float, direction: Direction) -> bool:
    """Strictly beyond the threshold.

    Strict, so a degenerate threshold sitting exactly on a flat baseline does not fire on the
    unchanged value -- it fires on the first genuine move, which is the useful behaviour.
    """
    if direction is Direction.UP:
        return value > threshold
    return value < threshold


def evaluate(
    rule: Rule,
    series: Series,
    period: int,
    detector: Detector,
    *,
    window: int = DEFAULT_WINDOW,
    k: float = DEFAULT_K,
    shift_tolerance: float = SHIFT_TOLERANCE,
) -> Evaluation:
    """One rule, one period, one detector.

    The order of checks is the order of severity of not knowing:

    BLIND first, because with no observation nothing else is answerable. UNTRAINED next, since
    a threshold from too little history is a guess. Then the comparison itself. ABSORBED is
    considered only when the comparison came out quiet, because a firing detector is not
    silent and absorption is a statement about silence.
    """
    value = series.value_at(period)
    if value is None:
        return Evaluation(
            rule_id=rule.rule_id, period=period, detector=detector,
            outcome=Outcome.BLIND, value=None,
        )

    threshold = resolve_threshold(
        rule, series, period, detector, window=window, k=k
    )
    if threshold.value is None:
        # For FIXED this means the rule never had a hand-set threshold; for the others, too
        # little history. Both are "not being watched", and the baseline size distinguishes
        # them without needing a separate outcome.
        return Evaluation(
            rule_id=rule.rule_id, period=period, detector=detector,
            outcome=Outcome.UNTRAINED, value=value,
            baseline_size=threshold.baseline_size,
        )

    shift = _level_shift(series, period, window)
    common = {
        "rule_id": rule.rule_id, "period": period, "detector": detector, "value": value,
        "threshold": threshold.value, "centre": threshold.centre,
        "spread": threshold.spread, "baseline_size": threshold.baseline_size,
        "reference_shift": shift,
    }

    if _exceeds(value, threshold.value, rule.direction):
        outcome = Outcome.DEGENERATE if threshold.degenerate else Outcome.FIRED
        return Evaluation(outcome=outcome, **common)

    # Quiet. The question is whether that means healthy or absorbed.
    if _is_absorbed(shift, threshold.spread, rule.direction, shift_tolerance):
        return Evaluation(outcome=Outcome.ABSORBED, **common)

    return Evaluation(outcome=Outcome.QUIET, **common)


def _level_shift(series: Series, period: int, window: int) -> float | None:
    """Recent level minus a reference level from before the rolling window could see it.

    The reference sits `window` periods further back, so the two do not overlap. Overlapping
    would put the same degradation in both and cancel it out, which is the failure this exists
    to detect.

    None when either side is too thin to average. Not 0.0 -- an unmeasurable shift is not a
    zero shift, and 0.0 would read as "the level is stable", the opposite of "we cannot tell".
    """
    recent = series.baseline(period, window)
    reference = series.reference(period, window, gap=window)
    if len(recent) < MIN_BASELINE or len(reference) < MIN_BASELINE:
        return None
    return mean(recent) - mean(reference)


def _is_absorbed(
    shift: float | None, spread: float | None, direction: Direction, tolerance: float
) -> bool:
    """Whether a quiet period is quiet because the baseline moved.

    Uses the reference spread implicitly through `tolerance * spread`, so the test is scaled to
    the metric's own noise. A degenerate spread cannot support this test at all: with spread 0
    every nonzero shift would exceed any multiple of it, so absorption would be declared on
    rounding noise. Those cases are left QUIET, and that is a real blind spot rather than a
    solved problem -- a metric that was perfectly flat and then stepped once will report
    DEGENERATE while it fires and plain QUIET afterwards.
    """
    if shift is None or spread is None or spread == 0.0:
        return False
    moved = shift if direction is Direction.UP else -shift
    return moved > tolerance * spread


def evaluate_series(
    rule: Rule,
    series: Series,
    detector: Detector,
    *,
    window: int = DEFAULT_WINDOW,
    k: float = DEFAULT_K,
) -> list[Evaluation]:
    """Every period of one rule under one detector.

    Every period, including those with no observation -- so the output length is the calendar
    span rather than the observation count. A report over only the observed periods cannot show
    what it failed to look at.
    """
    return [
        evaluate(rule, series, period, detector, window=window, k=k)
        for period in series.periods
    ]


def normal_tail(k: float) -> float:
    """One-tailed probability of exceeding k standard deviations under a normal distribution.

    One-tailed because a rule has a direction: watching for a two-sided move on a metric that
    can only get worse in one direction doubles the false-alarm rate for nothing.

    `erfc` rather than a lookup table so any k works, and rather than a simulation so the
    number is exact. Verified against measurement: at k=2 this gives 2.2750% and 400,000
    normal draws gave 2.2975%.
    """
    return 0.5 * math.erfc(k / math.sqrt(2.0))


def false_alarm_budget(k: float, rules: int, periods: int) -> FalseAlarmBudget:
    """Expected false alarms from the threshold alone, per rule and across the fleet.

    The multiplication is the point. At k=2 a single rule watched weekly expects 1.18 false
    alarms a year, which reads as acceptable. Forty rules expect 47.3, which is one almost
    every week -- and a detector that cries wolf weekly is one nobody reads.

    This is not a property of the data and is not measured from it. It is what the choice of k
    commits you to before any real anomaly happens.
    """
    return FalseAlarmBudget(
        k=k, per_test_rate=normal_tail(k), rules=rules, periods=periods
    )
