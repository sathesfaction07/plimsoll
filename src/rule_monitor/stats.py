"""The arithmetic, kept separate from the detectors that use it.

Two families, and the difference between them is the whole argument of this project:

    mean and standard deviation      breakdown point 0%   -- ONE outlier moves them
    median and MAD                   breakdown point 50%  -- unmoved until more than
                                                            half the window is bad

"Breakdown point" is the fraction of contaminated observations an estimator tolerates before
it can be made arbitrarily wrong. A detector whose baseline has a 0% breakdown point is
desensitised by the very anomalies it exists to catch.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

MAD_SCALE = 1.4826
"""Scales MAD so it estimates the same quantity as the standard deviation on normal data.

Without it, `k=3` means something different for each detector and the comparison between them
is meaningless -- which is the point of running both. Verified empirically rather than taken
from a table: over 300 samples of 4,000 normal draws, stdev/MAD averages 1.4826.

The constant is 1/Phi^-1(0.75). It is exactly right only for normal data, so on a skewed
metric the two detectors are again not quite comparable -- and nothing here can detect that.
"""

MIN_BASELINE = 4
"""Fewest observations from which a spread is worth computing.

`statistics.stdev` needs 2 and will happily return a number from 2, but a spread estimated
from 2 points is noise wearing a decimal point. MAD needs more still: from 4 points the median
absolute deviation takes one of only a few discrete values, so the threshold moves in visible
steps rather than smoothly.

4 is a judgement, not a derivation. It is low enough to be usable on quarterly data and high
enough that the number is not purely an artefact. Below it the detector reports UNTRAINED
rather than guessing.
"""


def mean(values: Sequence[float]) -> float:
    return statistics.fmean(values)


def stdev(values: Sequence[float]) -> float:
    """Sample standard deviation, or 0.0 for a single value.

    Returning 0.0 rather than raising, because a zero spread is a real state that the caller
    has to handle either way -- see `spread` and the DEGENERATE verdict. Raising would just
    move the same decision somewhere less convenient.
    """
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def median(values: Sequence[float]) -> float:
    return statistics.median(values)


def mad(values: Sequence[float], *, scaled: bool = True) -> float:
    """Median absolute deviation from the median.

    Scaled by default, so the result is comparable to a standard deviation and a caller can
    use the same `k` for both. `scaled=False` exists for tests that need the raw statistic.
    """
    if not values:
        return 0.0
    centre = statistics.median(values)
    deviation = statistics.median([abs(value - centre) for value in values])
    return deviation * MAD_SCALE if scaled else deviation


def centre_and_spread(values: Sequence[float], *, robust: bool) -> tuple[float, float]:
    """The pair a threshold is built from, chosen together.

    Together rather than separately, because mixing them is incoherent: a median with a
    standard deviation would centre robustly and then scale by a statistic one outlier can
    move, which has neither estimator's advantage.
    """
    if robust:
        return (median(values), mad(values))
    return (mean(values), stdev(values))


def breakdown_count(window_size: int, *, robust: bool) -> int:
    """How many contaminated observations the baseline tolerates.

    Returned as a number rather than described in a docstring so the report can state it.
    For the robust pair it is just under half the window; for mean and stdev it is zero, and
    zero is the honest answer -- a single outlier shifts both.
    """
    if not robust:
        return 0
    return (window_size - 1) // 2
