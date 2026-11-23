"""Scoring a detector against known incidents.

Two things reported that a plain precision/recall pair cannot express.

**Coverage.** Recall counts what a detector missed, but it cannot distinguish a detector that
looked and missed from one that never looked. A rule blind for half its periods can post
respectable recall on the half it saw, so the fraction of periods actually monitored is
reported next to it.

**Breakdown by incident shape.** A spike and a step are opposite problems for a rolling
baseline: a spike contaminates the window it later sits in, a step becomes the window. An
aggregate over both describes neither.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from rule_monitor.models import Accuracy, Detector, Evaluation, Incident, Outcome


def _incident_index(incidents: Iterable[Incident]) -> dict[str, list[Incident]]:
    grouped: dict[str, list[Incident]] = {}
    for incident in incidents:
        grouped.setdefault(incident.rule_id, []).append(incident)
    return grouped


def during_incident(
    evaluation: Evaluation, incidents: dict[str, list[Incident]]
) -> Incident | None:
    for incident in incidents.get(evaluation.rule_id, ()):
        if incident.covers(evaluation.period):
            return incident
    return None


def score(
    evaluations: Sequence[Evaluation],
    incidents: Iterable[Incident],
    detector: Detector,
) -> Accuracy:
    """Per-period scoring for one detector.

    Per period, not per incident. A five-period incident caught once and missed four times is
    counted as one hit and four misses, which is the honest reading when the question is
    whether an operator watching the dashboard each week would know. Per-incident scoring would
    call that a complete success and hide exactly the absorption this project is about.

    An unmonitored period is neither a hit nor a miss. Counting BLIND periods during an
    incident as false negatives would blame the detector for a data pipeline failure, and
    counting them as anything else would be worse -- so they are excluded from precision and
    recall and counted separately as lost coverage.
    """
    index = _incident_index(incidents)
    relevant = [item for item in evaluations if item.detector is detector]

    true_positives = false_positives = false_negatives = 0
    monitored = unmonitored = 0

    for evaluation in relevant:
        if not evaluation.outcome.is_monitored:
            unmonitored += 1
            continue
        monitored += 1
        inside = during_incident(evaluation, index) is not None
        if evaluation.outcome.is_alert:
            if inside:
                true_positives += 1
            else:
                false_positives += 1
        elif inside:
            false_negatives += 1

    return Accuracy(
        detector=detector,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        monitored_periods=monitored,
        unmonitored_periods=unmonitored,
    )


def score_by_kind(
    evaluations: Sequence[Evaluation],
    incidents: Iterable[Incident],
    detector: Detector,
) -> dict[str, Accuracy]:
    """Recall broken down by incident shape.

    Precision is meaningless per-kind and is not claimed: a false positive happens outside
    every incident, so it cannot be attributed to one shape. Only the recall side of each
    returned `Accuracy` should be read, and the renderer labels it accordingly.
    """
    index = _incident_index(incidents)
    relevant = [item for item in evaluations if item.detector is detector]
    kinds = sorted({incident.kind for incident in incidents})

    results: dict[str, Accuracy] = {}
    for kind in kinds:
        hits = misses = unmonitored = 0
        for evaluation in relevant:
            incident = during_incident(evaluation, index)
            if incident is None or incident.kind != kind:
                continue
            if not evaluation.outcome.is_monitored:
                unmonitored += 1
            elif evaluation.outcome.is_alert:
                hits += 1
            else:
                misses += 1
        results[kind] = Accuracy(
            detector=detector,
            true_positives=hits,
            false_positives=0,
            false_negatives=misses,
            monitored_periods=hits + misses,
            unmonitored_periods=unmonitored,
        )
    return results


def outcome_counts(
    evaluations: Sequence[Evaluation], detector: Detector
) -> dict[Outcome, int]:
    """Every outcome counted, including the zeros.

    Zeros included so a reader can see that a category was considered and came back empty,
    which is different from the category being absent from the report.
    """
    counts = dict.fromkeys(Outcome, 0)
    for evaluation in evaluations:
        if evaluation.detector is detector:
            counts[evaluation.outcome] += 1
    return counts


def absorption_report(
    evaluations: Sequence[Evaluation],
    incidents: Iterable[Incident],
    detector: Detector,
) -> list[tuple[str, int, int, float | None]]:
    """Rules where the detector went silent while an incident was still running.

    Returns `(rule_id, first silent period, periods silent, shift at that point)`.

    This is the finding the accuracy numbers cannot show. A detector can post a decent recall
    while being silent through the tail of every long incident, because the periods it caught
    at the leading edge outnumber nothing -- and the operator's experience is of an alert that
    stopped mattering.
    """
    index = _incident_index(incidents)
    relevant = sorted(
        (item for item in evaluations if item.detector is detector),
        key=lambda item: (item.rule_id, item.period),
    )

    findings: list[tuple[str, int, int, float | None]] = []
    current: tuple[str, int, int, float | None] | None = None

    for evaluation in relevant:
        incident = during_incident(evaluation, index)
        silent = (
            incident is not None
            and evaluation.outcome in {Outcome.QUIET, Outcome.ABSORBED}
        )
        if silent:
            if current is None or current[0] != evaluation.rule_id:
                current = (
                    evaluation.rule_id, evaluation.period, 1, evaluation.reference_shift
                )
            else:
                current = (current[0], current[1], current[2] + 1, evaluation.reference_shift)
        elif current is not None:
            findings.append(current)
            current = None

    if current is not None:
        findings.append(current)
    return findings


def compare(
    evaluations: Sequence[Evaluation],
    incidents: Iterable[Incident],
) -> dict[Detector, Accuracy]:
    """Every detector present in the evaluations, scored."""
    present = sorted({item.detector for item in evaluations}, key=lambda d: d.value)
    materialised = list(incidents)
    return {detector: score(evaluations, materialised, detector) for detector in present}


def best(results: dict[Detector, Accuracy]) -> Detector | None:
    """The detector with the highest F1, or None if none is measurable.

    None rather than an arbitrary default, so the caller has to handle "the data cannot say"
    instead of receiving a fabricated recommendation. That happens whenever no incident falls
    in a monitored period -- an entirely plausible outcome on a short series with delayed data.
    """
    eligible = {
        detector: accuracy
        for detector, accuracy in results.items()
        if accuracy.f1 is not None
    }
    if not eligible:
        return None
    return max(eligible, key=lambda detector: eligible[detector].f1 or 0.0)
