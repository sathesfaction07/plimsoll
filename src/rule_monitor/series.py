"""A rule's observations over time, with the gaps left visible.

The one decision that matters here: a period with no observation is represented explicitly as
`None` rather than skipped. The spec's worst case is "rules that should fire but didn't because
data is delayed", and a series that quietly closes over its gaps makes that undetectable in two
separate ways at once.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rule_monitor.models import Observation


class Series:
    """One rule's values indexed by period, gaps included.

    Backed by a dense list over the observed period range, so index arithmetic is period
    arithmetic and a missing period is a `None` you have to handle. The dense form costs
    memory proportional to the calendar span rather than the observation count, which for a
    rule observed once a year across a decade is wasteful -- and correctness here is worth
    more than the bytes.
    """

    def __init__(self, rule_id: str, observations: Iterable[Observation]) -> None:
        self.rule_id = rule_id
        by_period: dict[int, float] = {}
        for observation in observations:
            if observation.rule_id != rule_id:
                raise ValueError(f"observation for {observation.rule_id} in series {rule_id}")
            # A repeated period is a data problem the loader reports; last write wins here so
            # the series is always well-formed, and `duplicate_periods` names them.
            by_period[observation.period] = observation.value

        self._first = min(by_period, default=0)
        self._last = max(by_period, default=-1)
        self._values: list[float | None] = [
            by_period.get(period) for period in range(self._first, self._last + 1)
        ]

    @property
    def first_period(self) -> int:
        return self._first

    @property
    def last_period(self) -> int:
        return self._last

    @property
    def periods(self) -> range:
        return range(self._first, self._last + 1)

    def __len__(self) -> int:
        return len(self._values)

    def value_at(self, period: int) -> float | None:
        """The value, or None if that period was never observed or is out of range."""
        if not (self._first <= period <= self._last):
            return None
        return self._values[period - self._first]

    @property
    def observed_count(self) -> int:
        return sum(1 for value in self._values if value is not None)

    @property
    def missing_periods(self) -> tuple[int, ...]:
        """Periods inside the observed range with no value.

        Only interior gaps. A series simply starting later is not missing anything -- there is
        no evidence a period before the first observation was ever expected.
        """
        return tuple(
            period for period in self.periods if self.value_at(period) is None
        )

    def baseline(self, period: int, window: int) -> list[float]:
        """The observed values in the `window` periods immediately before `period`.

        Two things this deliberately does NOT do.

        It does not include `period` itself. Including the value under test in its own baseline
        pulls the threshold towards it, so a large anomaly partly justifies itself -- and the
        larger it is, the more it does.

        It does not reach further back to make up for gaps. A window of 6 means the last 6
        PERIODS, not the last 6 observations. Reaching back would silently compare against a
        stale baseline while reporting a full-sized one, so a rule with delayed data would look
        healthy on numbers months out of date. The count comes back short instead, and short
        enough means UNTRAINED.
        """
        if window < 1:
            raise ValueError(f"window must be at least 1, got {window}")
        start = max(self._first, period - window)
        return [
            value
            for offset in range(start, period)
            if (value := self.value_at(offset)) is not None
        ]

    def reference(self, period: int, window: int, gap: int) -> list[float]:
        """Observed values in a window ending `gap` periods before `period`.

        The comparison that catches a level shift the rolling baseline has swallowed. `gap` has
        to be at least the baseline window, or the reference overlaps the baseline and both
        contain the same degradation -- which is precisely the blindness this exists to break.
        """
        end = period - gap
        start = max(self._first, end - window)
        return [
            value
            for offset in range(start, end)
            if (value := self.value_at(offset)) is not None
        ]


def build_series(observations: Iterable[Observation]) -> dict[str, Series]:
    """Group observations into one series per rule."""
    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.rule_id, []).append(observation)
    return {
        rule_id: Series(rule_id, items) for rule_id, items in sorted(grouped.items())
    }


def duplicate_periods(observations: Sequence[Observation]) -> list[tuple[str, int]]:
    """Rule and period pairs observed more than once.

    Returned rather than raised on: two values for one period is a real thing exports do, and
    aborting a fleet-wide run over it is worse than reporting it. But it must not be silent,
    because the surviving value is arbitrary and a threshold computed from it is too.
    """
    seen: set[tuple[str, int]] = set()
    repeated: set[tuple[str, int]] = set()
    for observation in observations:
        key = (observation.rule_id, observation.period)
        if key in seen:
            repeated.add(key)
        seen.add(key)
    return sorted(repeated)
