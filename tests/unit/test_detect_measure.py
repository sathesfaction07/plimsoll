"""Detection, measurement, rendering, and the corpus properties.

The two findings this file exists to pin down:

  absorption   a rolling baseline swallows a sustained shift, and NO choice of statistic
               fixes it -- so the ABSORBED outcome, and its horizon, are asserted directly
  seasonality  the statistical detectors are far WORSE than a fixed threshold on a cyclic
               metric, and the robust one is worst of all
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

from rule_monitor.detect import (
    DEFAULT_WINDOW,
    evaluate,
    evaluate_series,
    resolve_threshold,
)
from rule_monitor.measure import (
    absorption_report,
    best,
    compare,
    during_incident,
    outcome_counts,
    score,
    score_by_kind,
)
from rule_monitor.models import Accuracy, Detector, Direction, Evaluation, Outcome
from rule_monitor.render import (
    accuracy_payload,
    render_absorption,
    render_accuracy,
    render_budget,
    render_inputs,
    render_outcomes,
    write_evaluations_csv,
)
from rule_monitor.series import build_series, duplicate_periods
from rule_monitor.sources import load_observations, orphan_observations
from rule_monitor.stats import MIN_BASELINE
from tests.conftest import make_incident, make_rule, make_series

FLAT = [100.0] * 10


class TestOutcomeIsNotABoolean:
    def test_only_quiet_means_healthy(self):
        healthy = [outcome for outcome in Outcome if outcome is Outcome.QUIET]
        assert len(healthy) == 1

    def test_alerts_include_degenerate(self):
        # The threshold WAS exceeded. The caveat travels with the alert rather than
        # suppressing it, because a move on a previously flat metric is the interesting kind.
        assert Outcome.FIRED.is_alert
        assert Outcome.DEGENERATE.is_alert
        assert not Outcome.QUIET.is_alert
        assert not Outcome.ABSORBED.is_alert

    def test_blind_and_untrained_are_not_monitored(self):
        assert not Outcome.BLIND.is_monitored
        assert not Outcome.UNTRAINED.is_monitored
        assert Outcome.QUIET.is_monitored
        assert Outcome.ABSORBED.is_monitored


class TestBlindAndUntrained:
    def test_a_missing_period_is_blind_not_quiet(self):
        series = make_series([*FLAT[:5], None, *FLAT[:4]])
        result = evaluate(make_rule(), series, 5, Detector.SIGMA)
        assert result.outcome is Outcome.BLIND
        # Not 0.0: a missing observation is not a zero measurement.
        assert result.value is None

    def test_too_little_history_is_untrained(self):
        series = make_series([100.0, 101.0, 102.0])
        result = evaluate(make_rule(), series, 2, Detector.SIGMA)
        assert result.outcome is Outcome.UNTRAINED
        assert result.baseline_size < MIN_BASELINE

    def test_a_rule_with_no_fixed_threshold_is_untrained_under_fixed(self):
        series = make_series(FLAT)
        result = evaluate(make_rule(fixed=None), series, 8, Detector.FIXED)
        assert result.outcome is Outcome.UNTRAINED

    def test_a_gap_makes_the_following_periods_untrained_again(self):
        # The baseline counts periods, not observations, so a long gap starves it.
        series = make_series(FLAT + [None] * 5 + [100.0])
        result = evaluate(make_rule(), series, 15, Detector.SIGMA)
        assert result.outcome is Outcome.UNTRAINED


class TestDegenerateBaseline:
    def test_zero_spread_collapses_the_threshold_onto_the_centre(self):
        series = make_series([*FLAT[:6], 101.0])
        threshold = resolve_threshold(make_rule(), series, 6, Detector.SIGMA, window=6)
        assert threshold.spread == 0.0
        assert threshold.value == 100.0
        assert threshold.degenerate

    def test_and_any_increase_fires_but_is_labelled(self):
        series = make_series([*FLAT[:6], 100.0001])
        result = evaluate(make_rule(), series, 6, Detector.SIGMA, window=6)
        # Fires, because the threshold was crossed -- and the magnitude means nothing, so it
        # is DEGENERATE rather than FIRED.
        assert result.outcome is Outcome.DEGENERATE
        assert result.outcome.is_alert

    def test_an_unchanged_value_does_not_fire_on_a_flat_baseline(self):
        # Strict comparison, so a degenerate threshold sitting exactly on the flat level
        # waits for a genuine move.
        series = make_series(FLAT)
        result = evaluate(make_rule(), series, 8, Detector.SIGMA, window=6)
        assert result.outcome is Outcome.QUIET


class TestDirection:
    def test_the_threshold_sits_below_the_centre_when_down_is_bad(self):
        series = make_series([100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 70.0])
        rule = make_rule(direction=Direction.DOWN)
        threshold = resolve_threshold(rule, series, 6, Detector.SIGMA, window=6)
        assert threshold.value < threshold.centre

    def test_a_drop_fires_only_for_a_down_rule(self):
        series = make_series([100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 70.0])
        down = evaluate(make_rule(direction=Direction.DOWN), series, 6, Detector.SIGMA)
        up = evaluate(make_rule(direction=Direction.UP), series, 6, Detector.SIGMA)
        assert down.outcome is Outcome.FIRED
        assert up.outcome is Outcome.QUIET


class TestSelfMasking:
    """One outlier in the baseline raises the bar, hiding a band of genuine anomalies."""

    def test_sigma_threshold_climbs_after_a_single_spike(self):
        clean = make_series([*FLAT[:6], 100.0])
        contaminated = make_series([*FLAT[:5], 150.0, 100.0])
        clean_threshold = resolve_threshold(
            make_rule(), clean, 6, Detector.SIGMA, window=6
        ).value
        dirty_threshold = resolve_threshold(
            make_rule(), contaminated, 6, Detector.SIGMA, window=6
        ).value
        # A genuine anomaly anywhere in this band is now invisible. I first predicted a
        # same-sized second spike would be masked; measurement showed it still fires, and the
        # real effect is this band.
        assert dirty_threshold > clean_threshold
        assert dirty_threshold - clean_threshold > 20.0

    def test_the_robust_threshold_does_not_move_at_all(self):
        clean = make_series([*FLAT[:6], 100.0])
        contaminated = make_series([*FLAT[:5], 150.0, 100.0])
        before = resolve_threshold(make_rule(), clean, 6, Detector.ROBUST, window=6).value
        after = resolve_threshold(
            make_rule(), contaminated, 6, Detector.ROBUST, window=6
        ).value
        assert before == after

    def test_the_reported_tolerance_matches_the_behaviour(self):
        series = make_series(FLAT)
        classical = resolve_threshold(make_rule(), series, 8, Detector.SIGMA, window=6)
        robust = resolve_threshold(make_rule(), series, 8, Detector.ROBUST, window=6)
        assert classical.tolerated_outliers == 0
        assert robust.tolerated_outliers > 0


class TestAbsorption:
    """A sustained step: fires briefly, then the window becomes the new normal."""

    STEP = [100.0] * 12 + [140.0] * 14

    def _outcomes(self, detector: Detector) -> list[Outcome]:
        series = make_series(self.STEP)
        return [
            item.outcome
            for item in evaluate_series(make_rule(), series, detector, window=DEFAULT_WINDOW)
        ]

    def test_sigma_fires_at_the_leading_edge_then_stops(self):
        outcomes = self._outcomes(Detector.SIGMA)
        alerts = [index for index, outcome in enumerate(outcomes) if outcome.is_alert]
        assert alerts, "the step must be detected at least once"
        assert min(alerts) == 12
        # A permanent degradation and a handful of alerts. The rest of the run is silence.
        assert len(alerts) < 5

    def test_the_silence_afterwards_is_labelled_absorbed_not_quiet(self):
        outcomes = self._outcomes(Detector.SIGMA)
        assert Outcome.ABSORBED in outcomes

    def test_robust_absorbs_it_too(self):
        # Robustness helps with contamination and does NOT help with absorption. The window
        # eventually contains only the new level, whatever statistic summarises it.
        outcomes = self._outcomes(Detector.ROBUST)
        alerts = [index for index, outcome in enumerate(outcomes) if outcome.is_alert]
        assert len(alerts) < 6

    def test_a_fixed_threshold_keeps_firing_because_it_cannot_drift(self):
        series = make_series(self.STEP)
        outcomes = [
            item.outcome
            for item in evaluate_series(make_rule(fixed=120.0), series, Detector.FIXED)
        ]
        alerts = [index for index, outcome in enumerate(outcomes) if outcome.is_alert]
        # Every degraded period, not two. The reference point is not computed from recent data.
        assert len(alerts) == 14

    def test_absorbed_decays_back_to_quiet_once_the_reference_also_shifts(self):
        # The honest limitation of the remedy: the reference window slides forward too, so
        # past roughly twice the window the shift collapses and ABSORBED cannot be detected.
        long_step = [100.0] * 10 + [140.0] * 40
        series = make_series(long_step)
        outcomes = [
            item.outcome
            for item in evaluate_series(make_rule(), series, Detector.SIGMA, window=6)
        ]
        tail = outcomes[-10:]
        assert all(outcome is Outcome.QUIET for outcome in tail)
        assert Outcome.ABSORBED not in tail

    def test_the_level_shift_is_none_when_it_cannot_be_computed(self):
        series = make_series(FLAT[:6])
        result = evaluate(make_rule(), series, 5, Detector.SIGMA, window=6)
        # Not 0.0 -- an unmeasurable shift is not a stable level.
        assert result.reference_shift is None


class TestDriftFollowsTheBaselineUp:
    def test_a_slow_ramp_never_fires_under_a_rolling_baseline(self):
        ramp = [100.0 + 3.0 * index for index in range(24)]
        series = make_series(ramp)
        outcomes = [
            item.outcome
            for item in evaluate_series(make_rule(), series, Detector.SIGMA, window=6)
        ]
        # Each step is small relative to the spread the ramp itself creates, so the threshold
        # rises just as fast as the metric.
        assert not any(outcome.is_alert for outcome in outcomes)

    def test_but_a_fixed_threshold_catches_it_on_crossing(self):
        ramp = [100.0 + 3.0 * index for index in range(24)]
        series = make_series(ramp)
        outcomes = [
            item.outcome
            for item in evaluate_series(make_rule(fixed=140.0), series, Detector.FIXED)
        ]
        assert any(outcome.is_alert for outcome in outcomes)


class TestMeasurement:
    def _evaluations(self, outcomes: list[Outcome]) -> list[Evaluation]:
        return [
            Evaluation(
                rule_id="r", period=index, detector=Detector.SIGMA,
                outcome=outcome, value=None if outcome is Outcome.BLIND else 1.0,
            )
            for index, outcome in enumerate(outcomes)
        ]

    def test_scoring_is_per_period(self):
        # A five-period incident caught once is one hit and four misses, not one success.
        outcomes = [Outcome.FIRED] + [Outcome.QUIET] * 4
        result = score(self._evaluations(outcomes), [make_incident(last=4)], Detector.SIGMA)
        assert result.true_positives == 1
        assert result.false_negatives == 4

    def test_blind_periods_are_excluded_rather_than_blamed(self):
        outcomes = [Outcome.BLIND] * 3
        result = score(self._evaluations(outcomes), [make_incident(last=2)], Detector.SIGMA)
        # Not false negatives -- that would blame the detector for a pipeline failure.
        assert result.false_negatives == 0
        assert result.unmonitored_periods == 3
        assert result.recall is None

    def test_coverage_separates_looking_from_finding(self):
        outcomes = [Outcome.QUIET] * 5 + [Outcome.BLIND] * 5
        result = score(self._evaluations(outcomes), [], Detector.SIGMA)
        assert result.coverage == 0.5

    def test_precision_is_none_when_nothing_fired(self):
        result = score(self._evaluations([Outcome.QUIET] * 4), [], Detector.SIGMA)
        assert result.precision is None
        assert result.recall is None
        assert result.f1 is None

    def test_a_real_zero_f1_is_distinguished_from_unmeasured(self):
        accuracy = Accuracy(
            detector=Detector.SIGMA, true_positives=0, false_positives=2,
            false_negatives=2, monitored_periods=4, unmonitored_periods=0,
        )
        assert accuracy.precision == 0.0
        assert accuracy.recall == 0.0
        assert accuracy.f1 == 0.0

    def test_absorbed_counts_as_a_miss_because_nobody_was_told(self):
        outcomes = [Outcome.ABSORBED] * 3
        result = score(self._evaluations(outcomes), [make_incident(last=2)], Detector.SIGMA)
        assert result.false_negatives == 3

    def test_outcome_counts_include_the_zeros(self):
        counts = outcome_counts(self._evaluations([Outcome.QUIET]), Detector.SIGMA)
        assert set(counts) == set(Outcome)
        assert counts[Outcome.FIRED] == 0

    def test_during_incident_matches_only_the_right_rule(self):
        evaluation = self._evaluations([Outcome.FIRED])[0]
        assert during_incident(evaluation, {"r": [make_incident(last=0)]}) is not None
        assert during_incident(evaluation, {"other": [make_incident(last=0)]}) is None

    def test_best_returns_none_when_nothing_is_measurable(self):
        empty = score(self._evaluations([Outcome.BLIND]), [], Detector.SIGMA)
        assert best({Detector.SIGMA: empty}) is None
        assert best({}) is None

    def test_score_by_kind_separates_shapes(self):
        outcomes = [Outcome.FIRED, Outcome.QUIET, Outcome.QUIET, Outcome.FIRED]
        incidents = [
            make_incident(first=0, last=1, kind="spike"),
            make_incident(first=2, last=3, kind="step"),
        ]
        by_kind = score_by_kind(self._evaluations(outcomes), incidents, Detector.SIGMA)
        assert by_kind["spike"].recall == 0.5
        assert by_kind["step"].recall == 0.5

    def test_absorption_report_finds_silent_runs(self):
        outcomes = [Outcome.FIRED, Outcome.QUIET, Outcome.ABSORBED, Outcome.QUIET]
        rows = absorption_report(
            self._evaluations(outcomes), [make_incident(last=3)], Detector.SIGMA
        )
        assert len(rows) == 1
        assert rows[0][1] == 1
        assert rows[0][2] == 3


class TestRendering:
    def _accuracy(self, **overrides) -> Accuracy:
        base = {
            "detector": Detector.SIGMA, "true_positives": 3, "false_positives": 1,
            "false_negatives": 2, "monitored_periods": 10, "unmonitored_periods": 2,
        }
        return Accuracy(**{**base, **overrides})

    def test_unmeasured_prints_n_a_and_never_zero(self):
        blank = self._accuracy(
            true_positives=0, false_positives=0, false_negatives=0,
            monitored_periods=0, unmonitored_periods=0,
        )
        text = render_accuracy({Detector.SIGMA: blank}, {Detector.SIGMA: {}}, None)
        assert "n/a" in text
        assert "0.0%" not in text

    def test_a_real_zero_recall_is_shown(self):
        zero = self._accuracy(true_positives=0, false_positives=0, false_negatives=5)
        text = render_accuracy({Detector.SIGMA: zero}, {Detector.SIGMA: {}}, None)
        assert "0.0%" in text
        assert "n/a" in text

    def test_no_preferred_detector_says_so(self):
        text = render_accuracy({}, {}, None)
        assert "none" in text

    def test_every_outcome_appears_even_at_zero(self):
        counts = {Detector.SIGMA: dict.fromkeys(Outcome, 0)}
        text = render_outcomes(counts)
        for outcome in Outcome:
            assert outcome.value in text

    def test_the_outcome_legend_says_only_quiet_means_healthy(self):
        text = render_outcomes({Detector.SIGMA: dict.fromkeys(Outcome, 0)})
        assert "Only QUIET means healthy" in text

    def test_the_budget_shows_both_scales(self):
        from rule_monitor.detect import false_alarm_budget
        text = render_budget(false_alarm_budget(2.0, 40, 52), {Detector.SIGMA: 12})
        assert "1.18" in text
        assert "47.3" in text

    def test_the_absorption_note_states_the_horizon(self):
        findings = {Detector.SIGMA: [("r", 20, 9, 0.05)]}
        text = render_absorption(findings, window=6)
        assert "12" in text
        assert "fixed threshold" in text

    def test_absorption_with_no_findings_says_none(self):
        text = render_absorption({Detector.SIGMA: []}, window=6)
        assert "none" in text

    def test_gaps_are_called_blind_rather_than_healthy(self):
        text = render_inputs(1, 10, 0, [], [], [], {"r": (3, 4, 5)})
        assert "BLIND, not healthy" in text

    def test_duplicates_and_orphans_are_explained(self):
        text = render_inputs(1, 10, 0, [], [("r", 7)], ["r-unknown"], {})
        assert "arbitrary" in text
        assert "stale" in text

    def test_csv_leaves_unmeasured_cells_empty(self, tmp_path: Path):
        rows = [
            Evaluation(
                rule_id="r", period=0, detector=Detector.SIGMA,
                outcome=Outcome.BLIND, value=None,
            ),
            Evaluation(
                rule_id="r", period=1, detector=Detector.SIGMA,
                outcome=Outcome.FIRED, value=5.0, threshold=4.0, centre=3.0,
                spread=0.5, baseline_size=6, reference_shift=0.25,
            ),
        ]
        target = tmp_path / "evaluations.csv"
        write_evaluations_csv(target, rows)
        parsed = list(csv.DictReader(target.open()))
        # Empty, not 0 -- 0 in a threshold column would read as "the bar was zero".
        assert parsed[0]["threshold"] == ""
        assert parsed[0]["value"] == ""
        assert parsed[1]["threshold"] == "4.000000"

    def test_json_keeps_none_as_null(self):
        blank = self._accuracy(
            true_positives=0, false_positives=0, false_negatives=0,
            monitored_periods=0, unmonitored_periods=0,
        )
        payload = json.loads(json.dumps(accuracy_payload(blank)))
        assert payload["precision"] is None
        assert payload["coverage"] is None


class TestLoading:
    def test_a_bad_row_is_reported_not_raised(self, tmp_path: Path):
        path = tmp_path / "observations.jsonl"
        path.write_text(
            '{"rule_id":"a","period":0,"value":1.0}\n'
            "not json\n"
            '{"rule_id":"b","period":"x","value":1.0}\n',
            encoding="utf-8",
        )
        items, problems = load_observations(path)
        assert len(items) == 1
        assert len(problems) == 2

    def test_orphans_are_named(self):
        from rule_monitor.models import Observation
        rows = [Observation(rule_id="ghost", period=0, value=1.0)]
        assert orphan_observations(rows, [make_rule("real")]) == ["ghost"]


class TestPropertiesOverTheShippedCorpus:
    """Assertions against `data/`, so editing the corpus and breaking an invariant fails."""

    def test_the_corpus_exercises_every_outcome(self, evaluations):
        # Guards the other corpus tests from becoming vacuous.
        seen = {item.outcome for item in evaluations}
        assert seen == set(Outcome), sorted(set(Outcome) - seen)

    def test_and_every_incident_shape(self, incidents):
        assert {incident.kind for incident in incidents} == {"spike", "step", "drift"}

    def test_the_planted_duplicate_and_orphan_are_present(self, observations, rules):
        assert duplicate_periods(observations) == [("r-dup", 7)]
        assert orphan_observations(observations, rules) == ["r-unknown"]

    def test_the_delayed_rule_has_a_gap_that_swallows_an_incident(
        self, series, incidents
    ):
        gap = series["r-delayed"].missing_periods
        assert gap == tuple(range(30, 39))
        incident = next(i for i in incidents if i.rule_id == "r-delayed")
        # The incident sits entirely inside the gap, so no detector can see it. That must
        # show as lost coverage, never as a detector failure.
        assert all(period in gap for period in range(incident.first_period,
                                                     incident.last_period + 1))

    def test_a_fixed_threshold_beats_both_statistical_detectors(
        self, evaluations, incidents
    ):
        results = compare(evaluations, incidents)
        chosen = best(results)
        # This contradicts the brief, which recommended replacing fixed thresholds with
        # rolling sigma. On this corpus fixed wins by a wide margin, because it is the only
        # detector whose reference point cannot drift into the degradation.
        assert chosen is Detector.FIXED
        assert results[Detector.FIXED].f1 > 3 * results[Detector.SIGMA].f1
        assert results[Detector.FIXED].f1 > 3 * results[Detector.ROBUST].f1

    def test_the_statistical_detectors_lose_on_steps_and_drift_not_spikes(
        self, evaluations, incidents
    ):
        fixed = score_by_kind(evaluations, incidents, Detector.FIXED)
        sigma = score_by_kind(evaluations, incidents, Detector.SIGMA)
        # A spike is the easy case and everything catches it.
        assert fixed["spike"].recall == sigma["spike"].recall == 1.0
        # A step and a drift are what a rolling baseline cannot hold on to.
        assert sigma["step"].recall < fixed["step"].recall / 3
        assert sigma["drift"].recall < fixed["drift"].recall / 2

    def test_the_robust_detector_raises_more_false_alarms_than_sigma(
        self, evaluations, incidents
    ):
        results = compare(evaluations, incidents)
        # Counter-intuitive and measured: on a cyclic metric most of the window sits at the
        # trough, so MAD is SMALLER than stdev, the threshold is tighter, and every seasonal
        # peak fires. Being robust to outliers makes it worse here.
        assert results[Detector.ROBUST].false_positives > results[Detector.SIGMA].false_positives

    def test_every_fixed_false_alarm_comes_from_the_seasonal_rule(
        self, evaluations, incidents
    ):
        from rule_monitor.measure import _incident_index
        index = _incident_index(incidents)
        offenders = {
            item.rule_id
            for item in evaluations
            if item.detector is Detector.FIXED
            and item.outcome.is_alert
            and during_incident(item, index) is None
        }
        assert offenders == {"r-seasonal"}

    def test_absorption_is_found_on_the_step_rule_for_both_rolling_detectors(
        self, evaluations, incidents
    ):
        for detector in (Detector.SIGMA, Detector.ROBUST):
            rows = absorption_report(evaluations, incidents, detector)
            assert any(row[0] == "r-step" for row in rows), detector
            longest = max(row[2] for row in rows if row[0] == "r-step")
            # Double figures of consecutive silent periods during a permanent degradation.
            assert longest >= 10, (detector, longest)

    def test_the_fixed_detector_has_much_shorter_silent_runs(
        self, evaluations, incidents
    ):
        rolling = max(
            row[2]
            for row in absorption_report(evaluations, incidents, Detector.SIGMA)
        )
        fixed_rows = absorption_report(evaluations, incidents, Detector.FIXED)
        fixed_longest = max((row[2] for row in fixed_rows), default=0)
        assert fixed_longest < rolling

    def test_the_flat_rule_produces_degenerate_outcomes(self, evaluations):
        degenerate = {
            item.rule_id
            for item in evaluations
            if item.outcome is Outcome.DEGENERATE
        }
        assert "r-flat" in degenerate

    def test_coverage_is_below_one_and_differs_between_detectors(
        self, evaluations, incidents
    ):
        results = compare(evaluations, incidents)
        for accuracy in results.values():
            assert accuracy.coverage < 1.0
        # FIXED needs no baseline, so it is untrained far less often -- its coverage is
        # higher for a structural reason, not a better algorithm.
        assert results[Detector.FIXED].coverage > results[Detector.SIGMA].coverage

    def test_the_series_are_stable_under_reordered_input(self, observations):
        def snapshot(rows):
            return {
                rule_id: [found.value_at(period) for period in found.periods]
                for rule_id, found in build_series(rows).items()
            }

        shuffled = list(observations)
        random.Random(3).shuffle(shuffled)
        assert snapshot(shuffled) == snapshot(observations)
