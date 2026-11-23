"""Shared fixtures.

The corpus is loaded once per session and the property tests assert against it, so editing
`data/` and breaking an invariant fails the build rather than silently changing the numbers in
the docs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rule_monitor.detect import evaluate_series
from rule_monitor.models import Detector, Direction, Evaluation, Incident, Observation, Rule
from rule_monitor.series import Series, build_series
from rule_monitor.sources import load_incidents, load_observations, load_rules

ROOT = Path(__file__).resolve().parents[1]


def make_rule(
    rule_id: str = "r",
    *,
    direction: Direction | str = Direction.UP,
    fixed: float | None = None,
    metric: str = "false_negative_rate",
) -> Rule:
    return Rule(
        rule_id=rule_id, metric=metric, direction=Direction(direction), fixed_threshold=fixed
    )


def make_series(values: list[float | None], rule_id: str = "r", start: int = 0) -> Series:
    """A series from a list, where `None` means that period was never observed."""
    return Series(rule_id, [
        Observation(rule_id=rule_id, period=start + offset, value=value)
        for offset, value in enumerate(values)
        if value is not None
    ])


def make_incident(
    rule_id: str = "r", first: int = 0, last: int = 0, kind: str = "spike"
) -> Incident:
    return Incident(rule_id=rule_id, first_period=first, last_period=last, kind=kind)


@pytest.fixture(scope="session")
def rules() -> list[Rule]:
    items, problems = load_rules(ROOT / "data" / "rules.jsonl")
    assert not problems, problems
    return items


@pytest.fixture(scope="session")
def observations() -> list[Observation]:
    items, problems = load_observations(ROOT / "data" / "observations.jsonl")
    assert not problems, problems
    return items


@pytest.fixture(scope="session")
def incidents() -> list[Incident]:
    items, problems = load_incidents(ROOT / "data" / "incidents.jsonl")
    assert not problems, problems
    return items


@pytest.fixture(scope="session")
def series(observations: list[Observation]) -> dict[str, Series]:
    return build_series(observations)


@pytest.fixture(scope="session")
def evaluations(
    rules: list[Rule], series: dict[str, Series]
) -> list[Evaluation]:
    items: list[Evaluation] = []
    for rule in rules:
        found = series.get(rule.rule_id)
        if found is None:
            continue
        for detector in Detector:
            items.extend(evaluate_series(rule, found, detector))
    return items
