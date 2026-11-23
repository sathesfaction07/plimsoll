"""Text and machine-readable output.

The rules, applied in both renderers: an unmeasured rate prints `n/a` and exports as `null`,
never `0.0`; every outcome category is shown even when its count is zero; and silence is never
reported as health without saying which kind of silence it was.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from rule_monitor.models import (
    Accuracy,
    Detector,
    Evaluation,
    FalseAlarmBudget,
    Outcome,
)


def _percent(value: float | None) -> str:
    """`n/a` for unmeasured, never 0.0%.

    A rate over an empty denominator is not zero. Printing 0.0% would read as total failure
    and would rank a detector that never looked above one that looked and missed.
    """
    return "n/a" if value is None else f"{value:>6.1%}"


def _decimal(value: float | None) -> str:
    return "n/a" if value is None else f"{value:>5.3f}"


def render_outcomes(counts: dict[Detector, dict[Outcome, int]]) -> str:
    lines = ["", "what each detector concluded, over every rule and period", ""]
    header = "  " + f"{'detector':<10}" + "".join(f"{o.value:>12}" for o in Outcome)
    lines.append(header)
    for detector, per_outcome in counts.items():
        lines.append(
            f"  {detector.value:<10}" + "".join(f"{per_outcome[o]:>12}" for o in Outcome)
        )
    lines.extend([
        "",
        "  Only QUIET means healthy. The other four non-firing outcomes each mean something",
        "  else, and three of them mean the metric was not being watched at all:",
        "",
        "    blind       no observation arrived for that period",
        "    untrained   too little history behind it to estimate a spread",
        "    absorbed    quiet, but the level has shifted away from an older reference --",
        "                the degradation is real and the detector has stopped saying so",
        "    degenerate  fired, but from a zero-spread baseline, so the threshold had",
        "                collapsed onto the centre and the magnitude means nothing",
    ])
    return "\n".join(lines) + "\n"


def render_accuracy(
    results: dict[Detector, Accuracy],
    by_kind: dict[Detector, dict[str, Accuracy]],
    preferred: Detector | None,
) -> str:
    lines = ["", "accuracy against known incidents, scored PER PERIOD", ""]
    lines.append(f"  {'detector':<10}{'precision':>11}{'recall':>9}{'F1':>8}{'coverage':>11}"
                 f"{'TP':>6}{'FP':>6}{'FN':>6}")
    for detector, accuracy in results.items():
        lines.append(
            f"  {detector.value:<10}{_percent(accuracy.precision):>11}"
            f"{_percent(accuracy.recall):>9}{_decimal(accuracy.f1):>8}"
            f"{_percent(accuracy.coverage):>11}"
            f"{accuracy.true_positives:>6}{accuracy.false_positives:>6}"
            f"{accuracy.false_negatives:>6}"
        )
    lines.extend([
        "",
        "  Per period, not per incident. A thirty-period degradation caught once and missed",
        "  twenty-nine times counts as one hit and twenty-nine misses -- which is what an",
        "  operator watching each week actually experiences. Per-incident scoring would call",
        "  that a complete success.",
        "",
        "  Coverage is the fraction of periods actually monitored. Recall alone cannot tell a",
        "  detector that looked and missed from one that never looked.",
    ])

    kinds = sorted({kind for per_detector in by_kind.values() for kind in per_detector})
    if kinds:
        lines.extend(["", "  recall by incident shape", ""])
        lines.append("  " + f"{'detector':<10}" + "".join(f"{kind:>12}" for kind in kinds))
        for detector, per_kind in by_kind.items():
            row = "".join(
                f"{_percent(per_kind[kind].recall).strip():>12}" if kind in per_kind
                else f"{'--':>12}"
                for kind in kinds
            )
            lines.append(f"  {detector.value:<10}{row}")
        lines.extend([
            "",
            "  Precision is not shown per shape and is not computable: a false positive falls",
            "  outside every incident, so it cannot be attributed to one.",
        ])

    lines.extend(["", "preferred detector", ""])
    if preferred is None:
        lines.append("  none -- no detector had a measurable F1 on this input.")
    else:
        lines.append(f"  {preferred.value}, on F1.")
    return "\n".join(lines) + "\n"


def render_budget(budget: FalseAlarmBudget, observed: dict[Detector, int]) -> str:
    lines = ["", "false-alarm budget: what the threshold commits you to", ""]
    lines.append(f"  k = {budget.k}, so each test has a {budget.per_test_rate:.4%} chance of "
                 "exceeding the threshold on noise alone")
    lines.append(f"  one rule over {budget.periods} periods   expects "
                 f"{budget.per_rule_per_year:>6.2f} false alarms")
    lines.append(f"  {budget.rules} rules over {budget.periods} periods expects "
                 f"{budget.fleet_total:>6.1f} false alarms")
    lines.extend([
        "",
        "  This is arithmetic, not a measurement: it is what the choice of k implies before",
        "  any real anomaly happens. The per-rule figure reads as acceptable and the fleet",
        "  figure is alarm fatigue, and they are the same number multiplied.",
        "",
        "  observed false positives, for comparison:",
    ])
    for detector, count in observed.items():
        lines.append(f"    {detector.value:<10}{count:>5}")
    lines.append("")
    lines.append("  A count far above the budget means the metric is not the smooth noise the")
    lines.append("  budget assumes -- seasonality is the usual reason, and it inflates the")
    lines.append("  statistical detectors far more than a fixed threshold.")
    return "\n".join(lines) + "\n"


def render_absorption(
    findings: dict[Detector, list[tuple[str, int, int, float | None]]],
    window: int,
) -> str:
    lines = ["", "silence during a known incident", ""]
    any_found = False
    for detector, rows in findings.items():
        if not rows:
            lines.append(f"  {detector.value}: none")
            continue
        any_found = True
        lines.append(f"  {detector.value}:")
        for rule_id, first, length, shift in sorted(rows, key=lambda r: -r[2]):
            drift = "n/a" if shift is None else f"{shift:+.4f}"
            lines.append(
                f"    {rule_id:<14} silent from period {first:<4} for {length:>3} period(s)"
                f"   level shift {drift}"
            )
    if any_found:
        lines.extend([
            "",
            "  A rolling baseline eventually treats a sustained degradation as normal, because",
            "  the window fills with it. No choice of statistic prevents this -- it is what a",
            "  rolling window IS.",
            "",
            f"  The level-shift column is the remedy, and it has a horizon of about {2 * window}",
            "  periods. Past that the reference window has itself slid into the degraded era,",
            "  the shift collapses towards zero, and ABSORBED decays back to plain QUIET. The",
            "  only reference point that cannot drift is one that is not computed from recent",
            "  data at all -- which is what a fixed threshold is.",
        ])
    return "\n".join(lines) + "\n"


def render_inputs(
    rules: int,
    observations: int,
    incidents: int,
    problems: Sequence[str],
    duplicates: Sequence[tuple[str, int]],
    orphans: Sequence[str],
    gaps: dict[str, tuple[int, ...]],
) -> str:
    lines = ["", "inputs", ""]
    lines.append(f"  {rules} rule(s), {observations} observation(s), {incidents} known incident(s)")
    if problems:
        lines.append(f"  {len(problems)} unreadable row(s):")
        lines.extend(f"    {problem}" for problem in problems[:5])
    if duplicates:
        lines.append(f"  {len(duplicates)} period(s) observed more than once:")
        for rule_id, period in duplicates[:5]:
            lines.append(f"    {rule_id} period {period}")
        lines.append("    the surviving value is arbitrary, and so is any threshold from it")
    if orphans:
        lines.append(f"  {len(orphans)} observed rule(s) absent from the rule list: "
                     f"{', '.join(orphans[:5])}")
        lines.append("    either the rule list or the metric feed is stale")
    if gaps:
        total = sum(len(periods) for periods in gaps.values())
        lines.append(f"  {total} missing period(s) across {len(gaps)} rule(s):")
        for rule_id, periods in sorted(gaps.items())[:5]:
            shown = list(periods[:6])
            more = "" if len(periods) <= 6 else f" (+{len(periods) - 6} more)"
            lines.append(f"    {rule_id}: {shown}{more}")
        lines.append("    these periods are BLIND, not healthy -- nothing was measured")
    return "\n".join(lines) + "\n"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_evaluations_csv(path: Path, evaluations: Iterable[Evaluation]) -> None:
    """One row per rule, period and detector.

    An unmeasured number is an EMPTY cell, never 0. A spreadsheet cannot distinguish them once
    written, and 0 in a threshold column would read as "the bar was zero" -- the opposite of
    "there was no bar".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "rule_id", "period", "detector", "outcome", "value", "threshold",
            "centre", "spread", "baseline_size", "reference_shift",
        ])
        for item in sorted(
            evaluations, key=lambda e: (e.rule_id, e.period, e.detector.value)
        ):
            writer.writerow([
                item.rule_id, item.period, item.detector.value, item.outcome.value,
                "" if item.value is None else f"{item.value:.6f}",
                "" if item.threshold is None else f"{item.threshold:.6f}",
                "" if item.centre is None else f"{item.centre:.6f}",
                "" if item.spread is None else f"{item.spread:.6f}",
                item.baseline_size,
                "" if item.reference_shift is None else f"{item.reference_shift:.6f}",
            ])


def accuracy_payload(accuracy: Accuracy) -> dict:
    """Accuracy as JSON, with None preserved as null rather than coerced to 0."""
    return {
        "detector": accuracy.detector.value,
        "true_positives": accuracy.true_positives,
        "false_positives": accuracy.false_positives,
        "false_negatives": accuracy.false_negatives,
        "monitored_periods": accuracy.monitored_periods,
        "unmonitored_periods": accuracy.unmonitored_periods,
        "precision": accuracy.precision,
        "recall": accuracy.recall,
        "f1": accuracy.f1,
        "coverage": accuracy.coverage,
    }
