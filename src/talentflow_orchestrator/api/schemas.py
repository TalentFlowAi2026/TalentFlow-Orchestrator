"""Public API schemas kept separate from provider and persistence models."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from talentflow_orchestrator.domain.models import Model
from talentflow_orchestrator.scheduling.models import SchedulingMode


class RescheduleAvailabilityRequest(Model):
    """Side-effect-free availability input for an existing interview."""

    company_id: UUID
    timezone: str = Field(min_length=1, max_length=64)
    start_date: date
    end_date: date
    window_start: time | None = None
    window_end: time | None = None
    duration_minutes: int = Field(ge=5, le=180)
    buffer_minutes: int = Field(default=0, ge=0, le=120)
    interviewer_ids: list[UUID] = Field(default_factory=list, max_length=20)
    mode: SchedulingMode = SchedulingMode.AI_ASSISTED

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("interviewer_ids")
    @classmethod
    def interviewers_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("interviewer_ids must be unique")
        return value

    @model_validator(mode="after")
    def coherent_window(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 92:
            raise ValueError("date range exceeds the absolute 92-day safety limit")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("window_start and window_end must be provided together")
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end <= self.window_start
        ):
            raise ValueError("window_end must be later than window_start")
        if self.mode == SchedulingMode.EXPLICIT:
            if self.start_date != self.end_date:
                raise ValueError("explicit scheduling requires a single date")
            if self.window_start is None or self.window_end is None:
                raise ValueError("explicit scheduling requires an exact time")
            window_minutes = (
                datetime.combine(self.start_date, self.window_end)
                - datetime.combine(self.start_date, self.window_start)
            ).total_seconds() / 60
            if window_minutes != self.duration_minutes:
                raise ValueError("explicit scheduling window must equal duration_minutes")
        return self


class RescheduleRequest(Model):
    company_id: UUID
    start: datetime
    duration_minutes: int = Field(ge=5, le=180)
    timezone: str = Field(min_length=1, max_length=64)
    buffer_minutes: int = Field(default=0, ge=0, le=120)
    interviewer_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("start")
    @classmethod
    def start_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("start must include a UTC offset")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def interval_must_stay_on_one_local_day(self) -> Self:
        local_start = self.start.astimezone(ZoneInfo(self.timezone))
        local_end = local_start + timedelta(minutes=self.duration_minutes)
        if local_end.date() != local_start.date():
            raise ValueError("reschedule interval may not cross a local date boundary")
        return self


class CancelRequest(Model):
    company_id: UUID
    reason: str = Field(min_length=1, max_length=500)


class InterpretRequest(Model):
    company_id: UUID
    text: str = Field(min_length=1, max_length=4000)


class GoogleAuthorizeRequest(Model):
    company_id: UUID


class CandidateAssignmentRequest(Model):
    company_id: UUID
    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)

    @field_validator("candidate_ids")
    @classmethod
    def candidates_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("candidate_ids must be unique")
        return value


class ErrorBody(Model):
    code: str
    message: str
    retryable: bool
    correlation_id: UUID


class ErrorResponse(Model):
    error: ErrorBody
