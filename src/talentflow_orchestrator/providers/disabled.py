"""Fail-closed adapters for intentionally disabled production integrations."""

from __future__ import annotations

from datetime import datetime

from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.providers.base import CalendarBusy, CalendarEvent, EmailMessage


class DisabledCalendarProvider:
    async def free_busy(
        self,
        access_token: str,
        *,
        time_min: datetime,
        time_max: datetime,
        calendar_ids: list[str],
    ) -> dict[str, list[CalendarBusy]]:
        raise ServiceError("calendar_integration_not_configured", retryable=False)

    async def create_event(self, access_token: str, event: CalendarEvent) -> str:
        raise ServiceError("calendar_integration_not_configured", retryable=False)

    async def update_event(
        self, access_token: str, event_id: str, event: CalendarEvent
    ) -> str:
        raise ServiceError("calendar_integration_not_configured", retryable=False)

    async def cancel_event(self, access_token: str, event_id: str) -> None:
        raise ServiceError("calendar_integration_not_configured", retryable=False)

    async def aclose(self) -> None:
        return None


class DisabledEmailProvider:
    async def send(self, access_token: str, message: EmailMessage) -> str:
        raise ServiceError("gmail_integration_not_configured", retryable=False)

    async def aclose(self) -> None:
        return None
