"""Explicitly gated real-provider smoke tests.

These tests create one temporary Calendar event and send one real email. They never
run unless the operator opts in and supplies a short-lived OAuth access token.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.providers.base import CalendarEvent, EmailMessage
from talentflow_orchestrator.providers.google.calendar import GoogleCalendarProvider
from talentflow_orchestrator.providers.google.gmail import GmailProvider

pytestmark = pytest.mark.live_google


def live_value(name: str) -> str:
    if os.getenv("RUN_LIVE_GOOGLE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_GOOGLE_TESTS=1 to permit real Google side effects")
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for the gated Google test")
    return value


def settings() -> Settings:
    return Settings.model_validate({"environment": "test", "provider_timeout_seconds": 30})


@pytest.mark.asyncio
async def test_live_calendar_create_update_cancel() -> None:
    access_token = live_value("GOOGLE_LIVE_ACCESS_TOKEN")
    start = (datetime.now(UTC) + timedelta(days=30)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    interview_id = uuid4()
    created_id: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        provider = GoogleCalendarProvider(settings(), client)
        event = CalendarEvent(
            interview_id=interview_id,
            schedule_version=1,
            title="TalentFlow gated integration test",
            description="Temporary test event; it will be deleted automatically.",
            start=start,
            end=start + timedelta(minutes=15),
            timezone="UTC",
        )
        try:
            created_id = await provider.create_event(access_token, event)
            updated = event.model_copy(
                update={
                    "schedule_version": 2,
                    "start": start + timedelta(minutes=30),
                    "end": start + timedelta(minutes=45),
                }
            )
            assert await provider.update_event(access_token, created_id, updated) == created_id
        finally:
            if created_id is not None:
                await provider.cancel_event(access_token, created_id)


@pytest.mark.asyncio
async def test_live_gmail_send_and_retry_reconciliation() -> None:
    access_token = live_value("GOOGLE_LIVE_ACCESS_TOKEN")
    sender = live_value("GOOGLE_LIVE_SENDER")
    recipient = live_value("GOOGLE_LIVE_RECIPIENT")
    logical_id = f"tf-live-{uuid4().hex}@notifications.talent-flow.app"
    message = EmailMessage(
        logical_message_id=logical_id,
        from_email=sender,
        to=recipient,
        subject="TalentFlow gated Gmail integration test",
        body="This is an explicitly authorized TalentFlow provider smoke test.",
    )
    async with httpx.AsyncClient(timeout=30) as client:
        provider = GmailProvider(settings(), client)
        first_id = await provider.send(access_token, message)

        # Wait for Gmail search indexing without calling send again. This prevents
        # the test itself from creating a duplicate while the message is indexing.
        found = False
        for _ in range(10):
            response = await client.get(
                provider._BASE + "/messages",
                headers={"Authorization": "Bearer " + access_token},
                params={"q": f"rfc822msgid:<{logical_id}>", "maxResults": 1},
            )
            response.raise_for_status()
            if response.json().get("messages"):
                found = True
                break
            await asyncio.sleep(2)
        assert found, "Gmail did not index the test message within 20 seconds"
        assert await provider.send(access_token, message) == first_id
