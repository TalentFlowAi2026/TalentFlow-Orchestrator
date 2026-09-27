"""Cross-layer domain primitives with no provider or framework dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


def utcnow() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, hide_input_in_errors=True)


class ServiceError(Exception):
    def __init__(
        self,
        code: str,
        *,
        status: int = 503,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(code)


class Actor(Model):
    user_id: UUID
    company_id: UUID
    role: str


class JobKind(StrEnum):
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    UPDATE_CALENDAR_EVENT = "update_calendar_event"
    CANCEL_CALENDAR_EVENT = "cancel_calendar_event"
    SEND_INVITATION = "send_interview_invitation"
    SEND_RESCHEDULE = "send_reschedule_email"
    SEND_CANCELLATION = "send_cancellation_email"


class OrchestratorJob(Model):
    id: UUID
    kind: JobKind
    company_id: UUID
    interview_id: UUID
    proposal_id: UUID | None = None
    integration_action_id: UUID
    correlation_id: UUID
    idempotency_key: str
    expected_schedule_version: int = Field(ge=0)
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1, le=10)
    lease_token: UUID | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
