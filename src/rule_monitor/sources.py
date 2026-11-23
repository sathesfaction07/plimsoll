"""Reading rules, observations and incidents.

Malformed rows are collected and returned rather than raised on: one bad line in a fleet-wide
export should not abort the run, but it must not vanish either, so the count reaches the report
and the exit code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from rule_monitor.models import Incident, Observation, Rule

Model = TypeVar("Model", bound=BaseModel)


def _load(path: Path, model: type[Model]) -> tuple[list[Model], list[str]]:
    items: list[Model] = []
    problems: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                items.append(model.model_validate_json(stripped))
            except ValidationError as error:
                problems.append(f"{path.name}:{number}: {error.error_count()} field error(s)")
            except json.JSONDecodeError as error:
                problems.append(f"{path.name}:{number}: not valid JSON ({error.msg})")
    return (items, problems)


def load_rules(path: Path) -> tuple[list[Rule], list[str]]:
    return _load(path, Rule)


def load_observations(path: Path) -> tuple[list[Observation], list[str]]:
    return _load(path, Observation)


def load_incidents(path: Path) -> tuple[list[Incident], list[str]]:
    return _load(path, Incident)


def orphan_observations(
    observations: list[Observation], rules: list[Rule]
) -> list[str]:
    """Rule ids present in the observations but not in the rule list.

    Reported rather than dropped silently: an observation for an unknown rule means either the
    rule list is stale or the metric feed is, and both are worth knowing. Dropping them would
    make the fleet look smaller than it is and would shrink the false-alarm budget accordingly.
    """
    known = {rule.rule_id for rule in rules}
    return sorted({item.rule_id for item in observations} - known)
