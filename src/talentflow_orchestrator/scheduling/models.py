"""Strict, provider-independent scheduling models."""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from talentflow_orchestrator.domain.models import Model


class SchedulingMode(StrEnum):
    EXPLICIT = "explicit"
    AI_ASSISTED = "ai_assisted"
    AUTO_BALANCED = "auto_balanced"


class BusyKind(StrEnum):
    CANDIDATE = "candidate"
    CANDIDATE_JOB_DAY = "candidate_job_day"
    INTERVIEWER = "interviewer"
    CALENDAR = "calendar"
    CAPACITY = "capacity"


class DailyWorkingHours(Model):
    start: time
    end: time

    @model_validator(mode="after")
    def end_must_follow_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError("working hours end must be later than start")
        return self


class SchedulingPolicy(Model):
    max_parallel_ai_interviews: int = Field(ge=1, le=100)
    backend_max_parallel_ai_interviews: int = Field(ge=1, le=100)
    slot_increment_minutes: int = Field(default=15, ge=1, le=60)
    minimum_lead_minutes: int = Field(default=15, ge=0, le=1440)
    default_interview_day_start: time = time(7)
    default_interview_day_end: time = time(17)
    allowed_weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1)
    working_hours: dict[int, DailyWorkingHours] = Field(default_factory=dict)
    enforce_working_hours: bool = False

    @field_validator("allowed_weekdays")
    @classmethod
    def valid_weekdays(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(day < 0 or day > 6 for day in value):
            raise ValueError("allowed_weekdays must contain unique values from 0 through 6")
        return value

    @field_validator("working_hours")
    @classmethod
    def valid_working_hour_weekdays(
        cls, value: dict[int, DailyWorkingHours]
    ) -> dict[int, DailyWorkingHours]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("working_hours keys must be weekdays 0 through 6")
        return value

    @model_validator(mode="after")
    def default_day_range_is_valid(self) -> Self:
        if self.default_interview_day_end <= self.default_interview_day_start:
            raise ValueError("default interview day end must be later than start")
        return self

    @property
    def effective_parallelism(self) -> int:
        return min(
            self.max_parallel_ai_interviews,
            self.backend_max_parallel_ai_interviews,
        )


class CompanySchedulingSettings(Model):
    timezone: str | None = Field(default=None, max_length=64)
    allow_parallel_ai_interviews: bool = False
    max_parallel_ai_interviews: int = Field(default=1, ge=1, le=100)
    default_duration_minutes: int | None = Field(default=None, ge=5, le=180)
    default_buffer_minutes: int = Field(default=0, ge=0, le=120)
    slot_increment_minutes: int = Field(default=15, ge=1, le=60)
    minimum_lead_minutes: int = Field(default=15, ge=0, le=1440)
    allowed_weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1)
    working_hours: dict[int, DailyWorkingHours] = Field(default_factory=dict)
    enforce_working_hours: bool = False

    @field_validator("timezone")
    @classmethod
    def optional_timezone_must_be_iana(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("company scheduling timezone must be IANA") from exc
        return value

    @field_validator("allowed_weekdays")
    @classmethod
    def company_weekdays_valid(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(day < 0 or day > 6 for day in value):
            raise ValueError("company allowed_weekdays are invalid")
        return value

    @field_validator("working_hours")
    @classmethod
    def company_working_hour_weekdays_valid(
        cls, value: dict[int, DailyWorkingHours]
    ) -> dict[int, DailyWorkingHours]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("company working_hours keys must be weekdays 0 through 6")
        return value

    def to_policy(
        self,
        backend_limit: int,
        *,
        default_day_start: time,
        default_day_end: time,
    ) -> SchedulingPolicy:
        return SchedulingPolicy(
            max_parallel_ai_interviews=(
                self.max_parallel_ai_interviews if self.allow_parallel_ai_interviews else 1
            ),
            backend_max_parallel_ai_interviews=backend_limit,
            slot_increment_minutes=self.slot_increment_minutes,
            minimum_lead_minutes=self.minimum_lead_minutes,
            default_interview_day_start=default_day_start,
            default_interview_day_end=default_day_end,
            allowed_weekdays=self.allowed_weekdays,
            working_hours=self.working_hours,
            enforce_working_hours=self.enforce_working_hours,
        )


class SchedulingRequest(Model):
    company_id: UUID
    job_id: UUID
    prompt_template_id: UUID | None = None
    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)
    interviewer_ids: list[UUID] = Field(default_factory=list, max_length=20)
    timezone: str = Field(min_length=1, max_length=64)
    start_date: date
    end_date: date
    window_start: time | None = None
    window_end: time | None = None
    duration_minutes: int = Field(ge=5, le=180)
    buffer_minutes: int = Field(default=0, ge=0, le=120)
    language: str = Field(default="en", min_length=2, max_length=50)
    mode: SchedulingMode
    parallelism_limit: int | None = Field(default=None, ge=1, le=100)
    notes: str = Field(default="", max_length=4000)

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("candidate_ids", "interviewer_ids")
    @classmethod
    def identifiers_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("identifiers must be unique")
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
            if len(self.candidate_ids) != 1:
                raise ValueError("explicit scheduling requires exactly one candidate")
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
        if self.mode == SchedulingMode.AUTO_BALANCED and self.start_date != self.end_date:
            raise ValueError("auto-balanced scheduling requires a single date")
        return self


class BusyInterval(Model):
    start: datetime
    end: datetime
    kind: BusyKind
    owner_id: UUID | None = None
    source: str = Field(default="talentflow", min_length=1, max_length=80)
    reference_id: UUID | None = None
    buffer_minutes: int = Field(default=0, ge=0, le=120)

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("busy intervals must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("busy interval end must be after start")
        if self.kind in {
            BusyKind.CANDIDATE,
            BusyKind.CANDIDATE_JOB_DAY,
            BusyKind.INTERVIEWER,
            BusyKind.CALENDAR,
        }:
            if self.owner_id is None:
                raise ValueError("owner_id is required for person-scoped busy intervals")
        return self


class SchedulingConflict(Model):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=500)
    conflicting_reference_ids: list[UUID] = Field(default_factory=list)


class ProposalItemDraft(Model):
    candidate_id: UUID
    job_id: UUID
    proposed_start: datetime | None = None
    proposed_end: datetime | None = None
    timezone: str
    language: str
    interviewer_ids: list[UUID] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[SchedulingConflict] = Field(default_factory=list)
    alternatives: list[datetime] = Field(default_factory=list, max_length=3)

    @property
    def schedulable(self) -> bool:
        return self.proposed_start is not None and self.proposed_end is not None


class FreeInterval(Model):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("free intervals must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("free interval end must be after start")
        return self


class ProposalDraft(Model):
    company_id: UUID
    job_id: UUID
    timezone: str
    effective_parallelism: int = Field(ge=1, le=100)
    items: list[ProposalItemDraft] = Field(min_length=1, max_length=100)
    scheduled_candidates: int = Field(default=0, ge=0, le=100)
    unscheduled_candidates: int = Field(default=0, ge=0, le=100)
    reason: str | None = Field(default=None, max_length=80)
    remaining_free_intervals: list[FreeInterval] = Field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return any(not item.schedulable for item in self.items)
