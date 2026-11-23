"""Command line interface.

Exit codes gate on what is demonstrable. `watch` fails on unreadable input or a duplicated
period, because both make a threshold arbitrary. It does NOT fail on a blind period, because
delayed data is normal and a check that always fires carries no information -- it is reported
loudly instead. `compare` fails when no detector has a measurable F1.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from rule_monitor.config import Settings
from rule_monitor.detect import (
    DEFAULT_K,
    DEFAULT_WINDOW,
    evaluate_series,
    false_alarm_budget,
    normal_tail,
    resolve_threshold,
)
from rule_monitor.measure import (
    absorption_report,
    best,
    outcome_counts,
    score_by_kind,
)
from rule_monitor.measure import (
    compare as compare_detectors,
)
from rule_monitor.models import Detector, Evaluation, Outcome
from rule_monitor.render import (
    accuracy_payload,
    render_absorption,
    render_accuracy,
    render_budget,
    render_inputs,
    render_outcomes,
    write_evaluations_csv,
    write_json,
)
from rule_monitor.series import build_series, duplicate_periods
from rule_monitor.sources import (
    load_incidents,
    load_observations,
    load_rules,
    orphan_observations,
)
from rule_monitor.stats import MAD_SCALE, MIN_BASELINE, mad, stdev

SETTINGS = Settings()


def _read(rules_path: Path, observations_path: Path, incidents_path: Path | None):
    for label, path in [("rules", rules_path), ("observations", observations_path)]:
        if not path.exists():
            click.echo(f"{label} file not found: {path}", err=True)
            raise SystemExit(1)

    rules, rule_problems = load_rules(rules_path)
    observations, observation_problems = load_observations(observations_path)
    incidents: list = []
    incident_problems: list[str] = []
    if incidents_path is not None:
        if not incidents_path.exists():
            click.echo(f"incidents file not found: {incidents_path}", err=True)
            raise SystemExit(1)
        incidents, incident_problems = load_incidents(incidents_path)

    problems = rule_problems + observation_problems + incident_problems
    return (rules, observations, incidents, problems)


def _evaluate_all(rules, series, window: int, k: float) -> list[Evaluation]:
    evaluations: list[Evaluation] = []
    for rule in rules:
        found = series.get(rule.rule_id)
        if found is None:
            continue
        for detector in Detector:
            evaluations.extend(
                evaluate_series(rule, found, detector, window=window, k=k)
            )
    return evaluations


@click.group()
@click.version_option()
def cli() -> None:
    """Statistical anomaly detection for monitored rules, with the blind spots named."""


@cli.command()
@click.option("-r", "--rules", type=click.Path(path_type=Path), default=SETTINGS.rules)
@click.option("-i", "--observations", type=click.Path(path_type=Path),
              default=SETTINGS.observations)
@click.option("-d", "--detector", type=click.Choice([d.value for d in Detector]),
              default=Detector.SIGMA.value)
@click.option("-w", "--window", type=click.IntRange(2, 200), default=DEFAULT_WINDOW)
@click.option("-k", type=click.FloatRange(0.0, 10.0, min_open=True), default=DEFAULT_K)
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None)
def watch(
    rules: Path, observations: Path, detector: str, window: int, k: float,
    output: Path | None,
) -> None:
    """Evaluate every rule and period under one detector."""
    rule_list, observation_list, _, problems = _read(rules, observations, None)
    series = build_series(observation_list)
    duplicates = duplicate_periods(observation_list)
    orphans = orphan_observations(observation_list, rule_list)
    gaps = {
        rule_id: found.missing_periods
        for rule_id, found in series.items()
        if found.missing_periods
    }

    chosen = Detector(detector)
    evaluations = [
        item
        for rule in rule_list
        if (found := series.get(rule.rule_id)) is not None
        for item in evaluate_series(rule, found, chosen, window=window, k=k)
    ]

    click.echo(render_inputs(
        len(rule_list), len(observation_list), 0, problems, duplicates, orphans, gaps
    ))
    click.echo(render_outcomes({chosen: outcome_counts(evaluations, chosen)}))

    alerts = [item for item in evaluations if item.outcome.is_alert]
    if alerts:
        click.echo("alerts\n")
        for item in sorted(alerts, key=lambda e: (e.rule_id, e.period))[:25]:
            caveat = "  [degenerate baseline: zero spread]" \
                if item.outcome is Outcome.DEGENERATE else ""
            click.echo(
                f"  {item.rule_id:<14} period {item.period:<4} value {item.value:>10.4f}"
                f"  threshold {item.threshold:>10.4f}{caveat}"
            )
        if len(alerts) > 25:
            click.echo(f"  ... and {len(alerts) - 25} more")
        click.echo("")

    absorbed = [item for item in evaluations if item.outcome is Outcome.ABSORBED]
    if absorbed:
        click.echo("absorbed -- quiet, but the level has shifted. Silence here is a lie.\n")
        for item in sorted(absorbed, key=lambda e: (e.rule_id, e.period))[:15]:
            click.echo(
                f"  {item.rule_id:<14} period {item.period:<4} value {item.value:>10.4f}"
                f"  level shift {item.reference_shift:+.4f}"
            )
        click.echo("")

    if output:
        write_json(output / "watch.json", {
            "summary": {
                "detector": chosen.value,
                "window": window,
                "k": k,
                "rules": len(rule_list),
                "observations": len(observation_list),
                "unreadable_rows": len(problems),
                "duplicate_periods": [[r, p] for r, p in duplicates],
                "orphan_rules": orphans,
                "blind_periods": sum(len(v) for v in gaps.values()),
                "alerts": len(alerts),
                "absorbed": len(absorbed),
            },
            "outcomes": {
                outcome.value: count
                for outcome, count in outcome_counts(evaluations, chosen).items()
            },
        })
        write_evaluations_csv(output / "evaluations.csv", evaluations)
        click.echo(f"wrote {output}/watch.json and evaluations.csv")

    if problems or duplicates:
        for problem in problems:
            click.echo(f"FAIL {problem}", err=True)
        for rule_id, period in duplicates:
            click.echo(f"FAIL {rule_id} period {period} observed more than once", err=True)
        sys.exit(1)


@cli.command()
@click.option("-r", "--rules", type=click.Path(path_type=Path), default=SETTINGS.rules)
@click.option("-i", "--observations", type=click.Path(path_type=Path),
              default=SETTINGS.observations)
@click.option("-c", "--incidents", type=click.Path(path_type=Path), default=SETTINGS.incidents)
@click.option("-w", "--window", type=click.IntRange(2, 200), default=DEFAULT_WINDOW)
@click.option("-k", type=click.FloatRange(0.0, 10.0, min_open=True), default=DEFAULT_K)
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None)
def compare(
    rules: Path, observations: Path, incidents: Path, window: int, k: float,
    output: Path | None,
) -> None:
    """Score every detector against known incidents."""
    rule_list, observation_list, incident_list, _ = _read(
        rules, observations, incidents
    )
    series = build_series(observation_list)
    evaluations = _evaluate_all(rule_list, series, window, k)

    results = compare_detectors(evaluations, incident_list)
    by_kind = {
        detector: score_by_kind(evaluations, incident_list, detector)
        for detector in results
    }
    preferred = best(results)

    click.echo(render_outcomes({
        detector: outcome_counts(evaluations, detector) for detector in Detector
    }))
    click.echo(render_accuracy(results, by_kind, preferred))

    periods = max((len(found) for found in series.values()), default=0)
    click.echo(render_budget(
        false_alarm_budget(k, len(rule_list), periods),
        {detector: accuracy.false_positives for detector, accuracy in results.items()},
    ))
    click.echo(render_absorption(
        {
            detector: absorption_report(evaluations, incident_list, detector)
            for detector in Detector
        },
        window,
    ))

    if output:
        write_json(output / "compare.json", {
            "window": window,
            "k": k,
            "rules": len(rule_list),
            "incidents": len(incident_list),
            "preferred": None if preferred is None else preferred.value,
            "budget": {
                "per_test_rate": normal_tail(k),
                "per_rule_per_period_span": normal_tail(k) * periods,
                "fleet_total": normal_tail(k) * periods * len(rule_list),
            },
            "detectors": [
                {
                    **accuracy_payload(accuracy),
                    "outcomes": {
                        outcome.value: count
                        for outcome, count in outcome_counts(evaluations, detector).items()
                    },
                    "recall_by_kind": {
                        kind: by_kind[detector][kind].recall
                        for kind in by_kind[detector]
                    },
                    "silent_runs": [
                        {"rule_id": r, "from_period": f, "periods": n, "shift": s}
                        for r, f, n, s in absorption_report(
                            evaluations, incident_list, detector
                        )
                    ],
                }
                for detector, accuracy in results.items()
            ],
        })
        click.echo(f"wrote {output}/compare.json")

    if preferred is None:
        click.echo("FAIL no detector had a measurable F1", err=True)
        sys.exit(1)


@cli.command()
@click.argument("rule_id")
@click.argument("period", type=int)
@click.option("-r", "--rules", type=click.Path(path_type=Path), default=SETTINGS.rules)
@click.option("-i", "--observations", type=click.Path(path_type=Path),
              default=SETTINGS.observations)
@click.option("-w", "--window", type=click.IntRange(2, 200), default=DEFAULT_WINDOW)
@click.option("-k", type=click.FloatRange(0.0, 10.0, min_open=True), default=DEFAULT_K)
def explain(
    rule_id: str, period: int, rules: Path, observations: Path, window: int, k: float
) -> None:
    """Show the baseline and threshold behind one rule in one period, for every detector."""
    rule_list, observation_list, _, _ = _read(rules, observations, None)
    rule = next((item for item in rule_list if item.rule_id == rule_id), None)
    if rule is None:
        click.echo(f"unknown rule: {rule_id}", err=True)
        raise SystemExit(1)
    series = build_series(observation_list).get(rule_id)
    if series is None:
        click.echo(f"no observations for {rule_id}", err=True)
        raise SystemExit(1)

    baseline = series.baseline(period, window)
    value = series.value_at(period)
    click.echo("")
    click.echo(f"  rule       {rule_id}  ({rule.metric}, worse when {rule.direction.value})")
    click.echo(f"  period     {period}")
    click.echo(f"  value      {'MISSING -- this period is BLIND' if value is None else value}")
    click.echo(f"  baseline   {len(baseline)} of {window} period(s): "
               f"{[round(item, 4) for item in baseline]}")
    if len(baseline) < MIN_BASELINE:
        click.echo(f"             fewer than {MIN_BASELINE}, so no spread can be estimated")
    click.echo("")
    click.echo("  Note the baseline is the last N PERIODS, not the last N observations. It does")
    click.echo("  not reach back over a gap, because a full-sized window built from stale data")
    click.echo("  would look healthy while comparing against months-old numbers.")
    click.echo("")

    if len(baseline) >= 2:
        raw = mad(baseline, scaled=False)
        click.echo(f"  stdev      {stdev(baseline):.6f}")
        click.echo(f"  MAD (raw)  {raw:.6f}")
        click.echo(f"  MAD x{MAD_SCALE}  {raw * MAD_SCALE:.6f}   "
                   "scaled so the same k means the same thing for both")
        click.echo("")

    for detector in Detector:
        threshold = resolve_threshold(
            rule, series, period, detector, window=window, k=k
        )
        if threshold.value is None:
            click.echo(f"  {detector.value:<8} no threshold "
                       f"(baseline {threshold.baseline_size})")
            continue
        note = "  ZERO SPREAD: threshold sits on the centre" if threshold.degenerate else ""
        click.echo(
            f"  {detector.value:<8} threshold {threshold.value:>10.4f}"
            f"  tolerates {threshold.tolerated_outliers} contaminated baseline point(s){note}"
        )
    click.echo("")
    click.echo("  'Tolerates' is the breakdown point. Mean and stdev tolerate ZERO: one outlier")
    click.echo("  in the window raises the threshold and hides genuine anomalies beneath it.")
    click.echo("")


@cli.command()
@click.option("-r", "--rules", type=click.Path(path_type=Path), default=SETTINGS.rules)
@click.option("-i", "--observations", type=click.Path(path_type=Path),
              default=SETTINGS.observations)
@click.option("-c", "--incidents", type=click.Path(path_type=Path), default=SETTINGS.incidents)
def doctor(rules: Path, observations: Path, incidents: Path) -> None:
    """Check configuration and inputs, and explain how to read the report."""
    click.echo("")
    click.echo("configuration")
    click.echo(f"  rules         {SETTINGS.rules}")
    click.echo(f"  observations  {SETTINGS.observations}")
    click.echo(f"  incidents     {SETTINGS.incidents}")
    click.echo(f"  window        {SETTINGS.window} periods")
    click.echo(f"  k             {SETTINGS.k}")
    click.echo("  no credentials, no network, no model: this is a small time-series problem")
    click.echo("  and the accuracy question is which threshold, which is arithmetic")

    click.echo("")
    click.echo("false-alarm budget at the configured k")
    rate = normal_tail(SETTINGS.k)
    click.echo(f"  {rate:.4%} per test, so {rate * 52:.2f} per rule per 52 periods")
    click.echo("  multiply by the number of rules for the fleet figure -- that multiplication")
    click.echo("  is the difference between an acceptable rate and alarm fatigue")

    click.echo("")
    click.echo("inputs")
    failed = False
    for label, path, loader in [
        ("rules", rules, load_rules),
        ("observations", observations, load_observations),
        ("incidents", incidents, load_incidents),
    ]:
        if not path.exists():
            click.echo(f"  {label:<13} MISSING: {path}")
            failed = True
            continue
        items, problems = loader(path)
        click.echo(f"  {label:<13} {len(items)} row(s), {len(problems)} unreadable")

    click.echo("")
    click.echo("how to read the report")
    click.echo("  quiet       the ONLY outcome that means healthy")
    click.echo("  blind       no observation arrived. Unmeasured, not healthy")
    click.echo("  untrained   too little history to estimate a spread")
    click.echo("  absorbed    quiet, but the level has shifted -- the rolling window has")
    click.echo("              swallowed a real degradation and stopped reporting it")
    click.echo("  degenerate  fired from a zero-spread baseline, so the magnitude is meaningless")
    click.echo("  coverage    fraction of periods actually monitored. Recall alone cannot")
    click.echo("              distinguish looking-and-missing from never looking")
    click.echo("  n/a         unmeasured, not zero")
    click.echo("")

    if failed:
        sys.exit(1)
