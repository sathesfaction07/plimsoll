"""Configuration from `RM_`-prefixed environment variables or a `.env` file.

No list-typed fields: Pydantic Settings parses those as JSON, so `RM_RULES=a,b` would fail with
a decode error that reads like a bug in the tool rather than a malformed variable.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RM_", env_file=".env", extra="ignore")

    rules: Path = Path("data/rules.jsonl")
    observations: Path = Path("data/observations.jsonl")
    incidents: Path = Path("data/incidents.jsonl")
    output_dir: Path = Path("output")

    window: int = Field(default=6, ge=2)
    """Baseline length in PERIODS, not observations. See `Series.baseline`."""

    k: float = Field(default=2.0, gt=0.0)
    """Threshold in spreads above the centre.

    Bounded above zero by the model rather than checked at the call site, so a nonsensical
    value fails at load with a message naming the field. There is deliberately no upper bound:
    a very large k is a legitimate choice for a noisy metric, and capping it would substitute
    the tool's judgement for the operator's.
    """
