"""Strict natural-language intent output. It deliberately contains no entity IDs."""

from __future__ import annotations

from datetime import date, time
from enum import StrEnum

from pydantic import Field

from talentflow_orchestrator.domain.models import Model


class IntentAction(StrEnum):
    CREATE_SINGLE = "CREATE_SINGLE"
    CREATE_BULK = "CREATE_BULK"
    RESCHEDULE = "RESCHEDULE"
    CANCEL = "CANCEL"
    FIND_AVAILABILITY = "FIND_AVAILABILITY"


class SchedulingIntent(Model):
    schema_version: int = Field(default=1, ge=1, le=1)
    action: IntentAction
    candidate_names: list[str] = Field(default_factory=list, max_length=100)
    position_name: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    start_date: date | None = None
    end_date: date | None = None
    window_start: time | None = None
    window_end: time | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=180)
    buffer_minutes: int | None = Field(default=None, ge=0, le=120)
    language: str | None = Field(default=None, max_length=50)
    parallelism_limit: int | None = Field(default=None, ge=1, le=100)
    clarification_fields: list[str] = Field(default_factory=list, max_length=20)
    explanation: str = Field(default="", max_length=1000)
