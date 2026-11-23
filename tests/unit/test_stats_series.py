"""Statistics and series: breakdown points, gaps, and the arithmetic behind the thresholds."""

from __future__ import annotations

import random
import statistics

import pytest

from rule_monitor.detect import false_alarm_budget, normal_tail
from rule_monitor.models import Observation
from rule_monitor.series import Series, build_series, duplicate_periods
from rule_monitor.stats import (
    MAD_SCALE,
    MIN_BASELINE,
    breakdown_count,
    centre_and_spread,
    mad,
    mean,
    median,
    stdev,
)
from tests.conftest import make_series


class TestMadScaleIsDerivedNotAssumed:
    def test_the_constant_reproduces_the_standard_deviation_on_normal_data(self):
        # Verified rather than taken from a table. Without this scaling, k=3 means one thing
        # for sigma and another for MAD, and comparing the two detectors is meaningless.
        rng = random.Random(11)
        ratios = []
        for _ in range(40):
            sample = [rng.gauss(0.0, 1.0) for _ in range(3000)]
            ratios.append(stdev(sample) / mad(sample, scaled=False))
        assert statistics.fmean(ratios) == pytest.approx(MAD_SCALE, abs=0.02)

    def test_scaling_is_applied_by_default_and_can_be_turned_off(self):
        values = [1.0, 2.0, 3.0, 4.0, 100.0]
        assert mad(values, scaled=False) == 1.0
        assert mad(values) == pytest.approx(MAD_SCALE)


class TestBreakdownPoint:
    """The property that separates the two detectors, asserted rather than described."""

    CLEAN = [100.0] * 8

    def test_one_outlier_moves_the_standard_deviation_a_long_way(self):
        contaminated = [*self.CLEAN[:-1], 150.0]
        # Breakdown point 0%: a single bad point is enough.
        assert stdev(self.CLEAN) == 0.0
        assert stdev(contaminated) > 15.0

    def test_but_leaves_mad_untouched(self):
        contaminated = [*self.CLEAN[:-1], 150.0]
        assert mad(contaminated) == 0.0

    def test_mad_survives_up_to_half_the_window(self):
        # Three of eight contaminated and MAD is still zero. Four of eight is the tipping
        # point, which is what a 50% breakdown point means.
        for count in (1, 2, 3):
            window = [100.0] * (8 - count) + [150.0] * count
            assert mad(window) == 0.0, count
        assert mad([100.0] * 4 + [150.0] * 4) > 0.0

    def test_the_tolerated_count_is_reported_as_a_number(self):
        assert breakdown_count(8, robust=True) == 3
        assert breakdown_count(8, robust=False) == 0
        assert breakdown_count(7, robust=True) == 3

    def test_centre_and_spread_are_chosen_as_a_pair(self):
        values = [1.0, 1.0, 1.0, 1.0, 50.0]
        classical = centre_and_spread(values, robust=False)
        robust = centre_and_spread(values, robust=True)
        assert classical == (mean(values), stdev(values))
        assert robust == (median(values), mad(values))
        # Mixing them would centre robustly and then scale by a contaminated statistic,
        # gaining neither estimator's advantage.
        assert robust[0] < classical[0]
        assert robust[1] < classical[1]


class TestStdevEdges:
    def test_a_single_value_has_zero_spread_rather_than_raising(self):
        assert stdev([5.0]) == 0.0

    def test_an_empty_window_gives_zero_mad(self):
        assert mad([]) == 0.0


class TestNormalTail:
    def test_it_matches_the_textbook_one_tailed_values(self):
        assert normal_tail(2.0) == pytest.approx(0.02275, abs=1e-5)
        assert normal_tail(3.0) == pytest.approx(0.00135, abs=1e-5)

    def test_it_agrees_with_simulation(self):
        rng = random.Random(5)
        draws = [rng.gauss(0.0, 1.0) for _ in range(60_000)]
        for k in (2.0, 2.5):
            observed = sum(1 for value in draws if value > k) / len(draws)
            assert observed == pytest.approx(normal_tail(k), abs=0.004)

    def test_a_higher_k_is_always_stricter(self):
        rates = [normal_tail(k) for k in (1.5, 2.0, 2.5, 3.0, 3.5)]
        assert rates == sorted(rates, reverse=True)


class TestFalseAlarmBudget:
    def test_the_per_rule_rate_looks_fine_and_the_fleet_rate_does_not(self):
        budget = false_alarm_budget(2.0, rules=40, periods=52)
        assert budget.per_rule_per_year == pytest.approx(1.183, abs=0.01)
        assert budget.fleet_total == pytest.approx(47.32, abs=0.05)
        # The same number multiplied. That multiplication is the difference between an
        # acceptable alert rate and one nobody reads.
        assert budget.fleet_total == pytest.approx(budget.per_rule_per_year * 40)

    def test_raising_k_shrinks_it_sharply(self):
        loose = false_alarm_budget(2.0, rules=40, periods=52)
        tight = false_alarm_budget(3.0, rules=40, periods=52)
        assert tight.fleet_total < loose.fleet_total / 10


class TestSeriesKeepsGapsVisible:
    def test_a_missing_period_reads_as_none_not_zero(self):
        series = make_series([1.0, 2.0, None, 4.0])
        assert series.value_at(2) is None
        # Zero would be a measurement. None is the absence of one, and on a rate metric zero
        # would read as perfect.
        assert series.value_at(2) != 0.0

    def test_interior_gaps_are_listed(self):
        series = make_series([1.0, None, None, 4.0])
        assert series.missing_periods == (1, 2)

    def test_a_series_that_starts_late_is_not_missing_anything(self):
        series = make_series([1.0, 2.0], start=10)
        assert series.first_period == 10
        assert series.missing_periods == ()

    def test_out_of_range_lookups_are_none(self):
        series = make_series([1.0, 2.0])
        assert series.value_at(-1) is None
        assert series.value_at(99) is None

    def test_an_empty_series_is_well_formed(self):
        series = Series("r", [])
        assert len(series) == 0
        assert series.observed_count == 0
        assert series.missing_periods == ()

    def test_an_observation_for_another_rule_is_rejected(self):
        with pytest.raises(ValueError, match="observation for"):
            Series("r", [Observation(rule_id="other", period=0, value=1.0)])


class TestBaselineWindow:
    def test_it_excludes_the_period_under_test(self):
        series = make_series([1.0, 2.0, 3.0, 4.0, 5.0])
        # Including the value under test would let a large anomaly partly justify itself, and
        # the larger it is the more it would.
        assert series.baseline(4, window=3) == [2.0, 3.0, 4.0]

    def test_it_counts_periods_not_observations(self):
        series = make_series([1.0, None, None, 4.0, 5.0])
        # A window of 3 before period 4 covers periods 1, 2, 3 -- two of which are missing.
        # Reaching further back would silently compare against stale data while reporting a
        # full-sized baseline.
        assert series.baseline(4, window=3) == [4.0]

    def test_it_shrinks_at_the_start_rather_than_wrapping(self):
        series = make_series([1.0, 2.0, 3.0])
        assert series.baseline(1, window=6) == [1.0]

    def test_a_window_below_one_is_rejected(self):
        series = make_series([1.0, 2.0])
        with pytest.raises(ValueError, match="at least 1"):
            series.baseline(1, window=0)

    def test_min_baseline_is_more_than_stdev_technically_needs(self):
        # statistics.stdev works from 2 points and returns noise. MIN_BASELINE is the point
        # at which the number is worth acting on.
        assert MIN_BASELINE > 2


class TestReferenceWindow:
    def test_it_sits_before_the_baseline_and_does_not_overlap(self):
        series = make_series([float(index) for index in range(20)])
        baseline = series.baseline(15, window=5)
        reference = series.reference(15, window=5, gap=5)
        assert baseline == [10.0, 11.0, 12.0, 13.0, 14.0]
        assert reference == [5.0, 6.0, 7.0, 8.0, 9.0]
        # Overlapping would put the same degradation in both and cancel it out, which is the
        # failure the reference exists to detect.
        assert not set(baseline) & set(reference)

    def test_it_comes_back_empty_early_in_a_series(self):
        series = make_series([1.0, 2.0, 3.0])
        assert series.reference(2, window=5, gap=5) == []


class TestLoadingAndGrouping:
    def test_observations_group_into_one_series_per_rule(self):
        rows = [
            Observation(rule_id="a", period=0, value=1.0),
            Observation(rule_id="b", period=0, value=2.0),
            Observation(rule_id="a", period=1, value=3.0),
        ]
        grouped = build_series(rows)
        assert sorted(grouped) == ["a", "b"]
        assert grouped["a"].observed_count == 2

    def test_a_repeated_period_is_reported_rather_than_absorbed(self):
        rows = [
            Observation(rule_id="a", period=0, value=1.0),
            Observation(rule_id="a", period=0, value=9.0),
            Observation(rule_id="a", period=1, value=2.0),
        ]
        # The surviving value is arbitrary, and so is any threshold built from it -- so this
        # has to reach the report and the exit code rather than being silently resolved.
        assert duplicate_periods(rows) == [("a", 0)]

    def test_no_duplicates_reports_nothing(self):
        rows = [Observation(rule_id="a", period=index, value=1.0) for index in range(3)]
        assert duplicate_periods(rows) == []
