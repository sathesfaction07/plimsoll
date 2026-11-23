"""Domain types.

The one that carries the design is `Outcome`. A detector's answer is not a boolean, because
"did not fire" has four distinct causes with four different remedies -- and three of them mean
the metric is unmonitored rather than healthy.

This is the third project in this portfolio to replace a boolean with an enum for the same
underlying reason, so it is a pattern rather than an insight. What is specific here is *which*
failures get names, and two of them (ABSORBED, DEGENERATE) are properties of rolling-window
statistics that no amount of care elsewhere prevents.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Detector(StrEnum):
    """How a threshold is decided.

    FIXED     a constant, set by hand. No baseline, so nothing to contaminate and nothing to
              absorb -- and no adaptation to a metric whose normal level changes.
    SIGMA     rolling mean + k*stdev. The spec's recommendation. Breakdown point 0%.
    ROBUST    rolling median + k*MAD. Breakdown point ~50%.
    """

    FIXED = "fixed"
    SIGMA = "sigma"
    ROBUST = "robust"


class Outcome(StrEnum):
    """What a detector concluded for one rule in one period.

    FIRED       the value exceeded the threshold.
    QUIET       it did not, and the baseline was sound. The only outcome that means healthy.
    ABSORBED    it did not, AND the recent level has shifted away from an older reference.
                The metric is degraded and the detector has stopped saying so, because the
                rolling window filled up with the new bad normal. Silence here is a LIE.
    BLIND       there is no observation for this period at all. Not healthy -- unmeasured.
                This is the spec's "rules that should fire but didn't because data is
                delayed", and it is invisible in any report that only lists alerts.
    UNTRAINED   fewer than MIN_BASELINE observations behind this period, so no spread can be
                estimated. Common at the start of a series and after a long gap.
    DEGENERATE  the baseline has zero spread, so the threshold collapses onto the centre and
                any increase at all exceeds it. Firing here carries no information about
                magnitude, and an epsilon fudge would invent a scale the data does not have.
    """

    FIRED = "fired"
    QUIET = "quiet"
    ABSORBED = "absorbed"
    BLIND = "blind"
    UNTRAINED = "untrained"
    DEGENERATE = "degenerate"

    @property
    def is_alert(self) -> bool:
        """Whether this outcome should reach a human as an alert.

        DEGENERATE counts: the threshold was exceeded, and the caveat travels with it rather
        than suppressing it. Suppressing would hide a real move on a previously flat metric,
        which is the most interesting kind.
        """
        return self in {Outcome.FIRED, Outcome.DEGENERATE}

    @property
    def is_monitored(self) -> bool:
        """Whether the metric was actually being watched this period.

        BLIND and UNTRAINED are not. Reporting a detection rate without separating them
        overstates coverage by whatever fraction of periods were never examined.
        """
        return self not in {Outcome.BLIND, Outcome.UNTRAINED}


class Direction(StrEnum):
    """Which way is bad.

    A false-negative rate going up is bad; a throughput going down is bad. Without this the
    detector would need two-tailed thresholds everywhere, doubling the false-alarm rate to
    watch for a move that cannot happen.
    """

    UP = "up"
    DOWN = "down"


class Rule(BaseModel):
    """One monitored rule and how it should be watched."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    metric: str
    direction: Direction = Direction.UP
    fixed_threshold: float | None = None
    """The hand-set threshold, or None if the rule has never had one.

    None rather than a default, because there is no defensible default for an unknown metric
    and inventing one would make the FIXED detector silently useless on that rule instead of
    visibly inapplicable.
    """


class Observation(BaseModel):
    """One rule's metric value in one period."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    period: int
    """Period index, not a date. See `series.py` for why the index has to be explicit."""
    value: float


class Incident(BaseModel):
    """A period range during which a rule was genuinely degraded.

    The truth set. Ranges rather than single periods because a real degradation persists, and
    that persistence is exactly what a rolling window absorbs.
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str
    first_period: int
    last_period: int
    kind: str
    """'spike', 'step' or 'drift' -- carried so the report can break accuracy down by shape.

    An aggregate over all three describes none of them: a spike and a step are opposite
    problems for a rolling baseline.
    """

    def covers(self, period: int) -> bool:
        return self.first_period <= period <= self.last_period

    @property
    def length(self) -> int:
        return self.last_period - self.first_period + 1


class Evaluation(BaseModel):
    """One detector's answer for one rule in one period, with the arithmetic behind it."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    period: int
    detector: Detector
    outcome: Outcome
    value: float | None
    """None when BLIND. Not 0.0 -- a missing observation is not a zero measurement, and on a
    rate metric zero would read as perfect."""
    threshold: float | None = None
    centre: float | None = None
    spread: float | None = None
    baseline_size: int = 0
    reference_shift: float | None = None
    """Recent level minus reference level, when both were computable.

    Carried on every evaluation rather than only on ABSORBED ones, so a reader can see the
    shift growing during the periods when the detector was still firing.
    """


class Accuracy(BaseModel):
    """Detection counts and rates, any of which may be undefined."""

    model_config = ConfigDict(frozen=True)

    detector: Detector
    true_positives: int
    false_positives: int
    false_negatives: int
    monitored_periods: int
    unmonitored_periods: int

    @property
    def precision(self) -> float | None:
        raised = self.true_positives + self.false_positives
        return self.true_positives / raised if raised else None

    @property
    def recall(self) -> float | None:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else None

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None:
            return None
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    @property
    def coverage(self) -> float | None:
        """Fraction of periods actually monitored.

        Reported alongside recall because recall alone cannot distinguish a detector that
        looked and missed from one that never looked. A detector blind for half the series
        can post respectable recall on the half it saw.
        """
        total = self.monitored_periods + self.unmonitored_periods
        return self.monitored_periods / total if total else None


class FalseAlarmBudget(BaseModel):
    """Expected false alarms from the threshold alone, before any real anomaly.

    Computed rather than measured, because it is a property of the choice of k and the number
    of tests -- not of the data. A per-rule rate that reads as acceptable becomes alarm
    fatigue once multiplied by the fleet, and that multiplication is the whole point.
    """

    model_config = ConfigDict(frozen=True)

    k: float
    per_test_rate: float
    rules: int
    periods: int

    @property
    def per_rule_per_year(self) -> float:
        return self.per_test_rate * self.periods

    @property
    def fleet_total(self) -> float:
        return self.per_test_rate * self.periods * self.rules


class Threshold(BaseModel):
    """A resolved threshold and the baseline it came from."""

    model_config = ConfigDict(frozen=True)

    value: float | None
    centre: float | None
    spread: float | None
    baseline_size: int
    degenerate: bool = False
    tolerated_outliers: int = Field(default=0, ge=0)
    """How many contaminated baseline points this estimator survives. 0 for mean/stdev."""
