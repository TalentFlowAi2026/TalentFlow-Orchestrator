"""Provider-neutral contracts and DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import Field, StringConstraints

from talentflow_orchestrator.domain.models import Model

EmailAddress = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=320,
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
    ),
]


class OAuthTokens(Model):
    access_token: str = Field(min_length=1, max_length=8192)
    refresh_token: str | None = Field(default=None, max_length=8192)
    expires_at: datetime
    scopes: list[str] = Field(min_length=1, max_length=20)
    provider_subject: str = Field(min_length=1, max_length=255)
    account_email: EmailAddress


class ProviderAccount(Model):
    id: UUID
    company_id: UUID
    account_email: EmailAddress
    scopes: list[str]
    access_token: str | None = None
    refresh_token: str
    access_token_expires_at: datetime | None = None


class CalendarEvent(Model):
    interview_id: UUID
    schedule_version: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(max_length=4000)
    start: datetime
    end: datetime
    timezone: str
    attendee_emails: list[EmailAddress] = Field(default_factory=list, max_length=20)


class CalendarBusy(Model):
    start: datetime
    end: datetime


class EmailMessage(Model):
    logical_message_id: str = Field(min_length=1, max_length=200)
    from_email: EmailAddress
    to: EmailAddress
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


class OAuthProvider(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str) -> str: ...
    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens: ...
    async def refresh(self, refresh_token: str) -> tuple[str, datetime]: ...
    async def revoke(self, access_or_refresh_token: str) -> None: ...
    async def aclose(self) -> None: ...


class CalendarProvider(Protocol):
    async def free_busy(
        self,
        access_token: str,
        *,
        time_min: datetime,
        time_max: datetime,
        calendar_ids: list[str],
    ) -> dict[str, list[CalendarBusy]]: ...
    async def create_event(self, access_token: str, event: CalendarEvent) -> str: ...
    async def update_event(
        self, access_token: str, event_id: str, event: CalendarEvent
    ) -> str: ...
    async def cancel_event(self, access_token: str, event_id: str) -> None: ...
    async def aclose(self) -> None: ...


class EmailProvider(Protocol):
    async def send(self, access_token: str, message: EmailMessage) -> str: ...
    async def aclose(self) -> None: ...
