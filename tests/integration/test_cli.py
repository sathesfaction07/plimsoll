"""End-to-end CLI behaviour: output, exit codes, and machine-readable files."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from click.testing import CliRunner

from rule_monitor.cli import cli

ROOT = Path(__file__).resolve().parents[2]
RULES = str(ROOT / "data" / "rules.jsonl")
OBS = str(ROOT / "data" / "observations.jsonl")
INC = str(ROOT / "data" / "incidents.jsonl")


class TestWatch:
    def test_it_fails_on_the_planted_duplicate_period(self):
        result = CliRunner().invoke(cli, ["watch", "-r", RULES, "-i", OBS])
        # A duplicated period makes the surviving value, and any threshold from it, arbitrary.
        assert result.exit_code == 1
        assert "observed more than once" in result.output

    def test_it_reports_gaps_as_blind_rather_than_failing(self):
        result = CliRunner().invoke(cli, ["watch", "-r", RULES, "-i", OBS])
        assert "BLIND, not healthy" in result.output
        assert "blind" in result.output

    def test_it_names_the_orphan_rule(self):
        result = CliRunner().invoke(cli, ["watch", "-r", RULES, "-i", OBS])
        assert "r-unknown" in result.output
        assert "stale" in result.output

    def test_the_outcome_legend_is_always_shown(self):
        result = CliRunner().invoke(cli, ["watch", "-r", RULES, "-i", OBS])
        assert "Only QUIET means healthy" in result.output
        for term in ("absorbed", "degenerate", "untrained"):
            assert term in result.output

    def test_absorbed_periods_are_called_out_separately(self):
        result = CliRunner().invoke(cli, ["watch", "-r", RULES, "-i", OBS])
        assert "Silence here is a lie" in result.output

    def test_the_fixed_detector_reports_no_absorption(self):
        result = CliRunner().invoke(
            cli, ["watch", "-r", RULES, "-i", OBS, "-d", "fixed"]
        )
        # A fixed threshold has no baseline to absorb anything into.
        assert "Silence here is a lie" not in result.output

    def test_json_and_csv_are_written(self, tmp_path: Path):
        result = CliRunner().invoke(
            cli, ["watch", "-r", RULES, "-i", OBS, "-o", str(tmp_path)]
        )
        assert result.exit_code == 1  # still the duplicate
        payload = json.loads((tmp_path / "watch.json").read_text())
        assert payload["summary"]["rules"] == 26
        assert payload["summary"]["duplicate_periods"] == [["r-dup", 7]]
        assert payload["summary"]["orphan_rules"] == ["r-unknown"]
        assert payload["summary"]["blind_periods"] == 9
        assert set(payload["outcomes"]) == {
            "fired", "quiet", "absorbed", "blind", "untrained", "degenerate"
        }

        rows = list(csv.DictReader((tmp_path / "evaluations.csv").open()))
        blind = [row for row in rows if row["outcome"] == "blind"]
        assert blind
        # Empty cells, never 0 -- a threshold of 0 would read as "the bar was zero".
        assert all(row["value"] == "" and row["threshold"] == "" for row in blind)

    def test_a_missing_input_fails_cleanly(self):
        result = CliRunner().invoke(cli, ["watch", "-r", "/nope.jsonl", "-i", OBS])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_k_must_be_positive(self):
        result = CliRunner().invoke(cli, ["watch", "-r", RULES, "-i", OBS, "-k", "0"])
        assert result.exit_code != 0


class TestCompare:
    def test_the_fixed_threshold_wins_which_contradicts_the_brief(self):
        result = CliRunner().invoke(cli, ["compare", "-r", RULES, "-i", OBS, "-c", INC])
        assert result.exit_code == 0
        assert "preferred detector" in result.output
        lines = result.output.splitlines()
        preferred = next(
            line for index, line in enumerate(lines)
            if index > 0 and "preferred detector" in lines[index - 2]
        )
        assert "fixed" in preferred

    def test_it_shows_recall_by_incident_shape(self):
        result = CliRunner().invoke(cli, ["compare", "-r", RULES, "-i", OBS, "-c", INC])
        for kind in ("spike", "step", "drift"):
            assert kind in result.output
        assert "not computable" in result.output

    def test_it_shows_both_scales_of_the_false_alarm_budget(self):
        result = CliRunner().invoke(cli, ["compare", "-r", RULES, "-i", OBS, "-c", INC])
        assert "false-alarm budget" in result.output
        assert "alarm fatigue" in result.output

    def test_it_states_the_absorption_horizon(self):
        result = CliRunner().invoke(cli, ["compare", "-r", RULES, "-i", OBS, "-c", INC])
        assert "silence during a known incident" in result.output
        assert "only reference point that cannot drift" in result.output

    def test_the_json_carries_the_findings(self, tmp_path: Path):
        result = CliRunner().invoke(
            cli, ["compare", "-r", RULES, "-i", OBS, "-c", INC, "-o", str(tmp_path)]
        )
        assert result.exit_code == 0
        payload = json.loads((tmp_path / "compare.json").read_text())
        assert payload["preferred"] == "fixed"
        detectors = {row["detector"]: row for row in payload["detectors"]}
        assert set(detectors) == {"fixed", "sigma", "robust"}
        # Fixed dominates on F1.
        assert detectors["fixed"]["f1"] > 3 * detectors["sigma"]["f1"]
        # Robust raises MORE false alarms than sigma on cyclic data.
        assert detectors["robust"]["false_positives"] > detectors["sigma"]["false_positives"]
        # Everything catches a spike; only fixed holds on to a step.
        assert detectors["sigma"]["recall_by_kind"]["spike"] == 1.0
        assert detectors["sigma"]["recall_by_kind"]["step"] < 0.3
        assert detectors["fixed"]["recall_by_kind"]["step"] > 0.8
        # Only the rolling detectors have long silent runs.
        rolling = max(row["periods"] for row in detectors["sigma"]["silent_runs"])
        assert rolling >= 10
        assert payload["budget"]["fleet_total"] > payload["budget"][
            "per_rule_per_period_span"
        ]

    def test_a_higher_k_shrinks_the_budget(self, tmp_path: Path):
        for k in ("2.0", "3.0"):
            CliRunner().invoke(cli, [
                "compare", "-r", RULES, "-i", OBS, "-c", INC,
                "-k", k, "-o", str(tmp_path / k),
            ])
        loose = json.loads((tmp_path / "2.0" / "compare.json").read_text())
        tight = json.loads((tmp_path / "3.0" / "compare.json").read_text())
        assert tight["budget"]["fleet_total"] < loose["budget"]["fleet_total"] / 10

    def test_a_missing_incidents_file_fails_cleanly(self):
        result = CliRunner().invoke(
            cli, ["compare", "-r", RULES, "-i", OBS, "-c", "/nope.jsonl"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output


class TestExplain:
    def test_it_shows_the_baseline_and_both_spreads(self):
        result = CliRunner().invoke(
            cli, ["explain", "r-step", "26", "-r", RULES, "-i", OBS]
        )
        assert result.exit_code == 0
        assert "baseline" in result.output
        assert "stdev" in result.output
        assert "MAD" in result.output
        assert "tolerates 0 contaminated" in result.output

    def test_it_flags_a_zero_spread_baseline(self):
        result = CliRunner().invoke(
            cli, ["explain", "r-flat", "20", "-r", RULES, "-i", OBS]
        )
        assert "ZERO SPREAD" in result.output

    def test_it_says_a_period_is_blind(self):
        result = CliRunner().invoke(
            cli, ["explain", "r-delayed", "34", "-r", RULES, "-i", OBS]
        )
        assert "BLIND" in result.output

    def test_it_explains_that_the_window_counts_periods(self):
        result = CliRunner().invoke(
            cli, ["explain", "r-quiet", "10", "-r", RULES, "-i", OBS]
        )
        assert "last N PERIODS, not the last N observations" in result.output

    def test_an_unknown_rule_fails(self):
        result = CliRunner().invoke(
            cli, ["explain", "nope", "1", "-r", RULES, "-i", OBS]
        )
        assert result.exit_code == 1
        assert "unknown rule" in result.output


class TestDoctor:
    def test_it_runs_with_no_credentials_and_explains_the_vocabulary(self):
        result = CliRunner().invoke(
            cli, ["doctor", "-r", RULES, "-i", OBS, "-c", INC]
        )
        assert result.exit_code == 0
        for term in ("quiet", "blind", "untrained", "absorbed", "degenerate", "coverage"):
            assert term in result.output
        assert "no credentials, no network, no model" in result.output

    def test_it_shows_the_false_alarm_budget_up_front(self):
        result = CliRunner().invoke(
            cli, ["doctor", "-r", RULES, "-i", OBS, "-c", INC]
        )
        assert "false-alarm budget" in result.output
        assert "alarm fatigue" in result.output

    def test_it_fails_when_an_input_is_missing(self):
        result = CliRunner().invoke(
            cli, ["doctor", "-r", "/nope.jsonl", "-i", OBS, "-c", INC]
        )
        assert result.exit_code == 1
        assert "MISSING" in result.output
