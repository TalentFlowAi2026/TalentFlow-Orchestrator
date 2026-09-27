import base64
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import JobKind
from talentflow_orchestrator.persistence.integrations import ActionContext
from talentflow_orchestrator.providers.base import CalendarEvent, EmailMessage
from talentflow_orchestrator.providers.google.calendar import (
    GoogleCalendarProvider,
    deterministic_event_id,
)
from talentflow_orchestrator.providers.google.gmail import GmailProvider
from talentflow_orchestrator.workers.handlers import SchedulingJobHandlers


def settings() -> Settings:
    key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    return Settings.model_validate({"environment": "test", "data_encryption_key": key})


@pytest.mark.asyncio
async def test_calendar_create_retry_reconciles_the_deterministic_event() -> None:
    event = CalendarEvent(
        interview_id=uuid4(),
        schedule_version=2,
        title="Interview",
        description="Candidate interview",
        start=datetime(2026, 10, 1, 7, tzinfo=UTC),
        end=datetime(2026, 10, 1, 7, 30, tzinfo=UTC),
        timezone="Asia/Gaza",
        attendee_emails=["candidate@example.com"],
    )
    event_id = deterministic_event_id(event)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(409, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": event_id,
                "extendedProperties": {
                    "private": {
                        "talentflowInterviewId": str(event.interview_id),
                        "talentflowScheduleVersion": str(event.schedule_version),
                    }
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = GoogleCalendarProvider(settings(), client)
        assert await provider.create_event("access-token", event) == event_id

    assert [request.method for request in calls] == ["POST", "GET"]
    assert calls[1].url.path.endswith("/" + event_id)


@pytest.mark.asyncio
async def test_calendar_reschedule_updates_the_existing_provider_event() -> None:
    interview_id = uuid4()
    event = CalendarEvent(
        interview_id=interview_id,
        schedule_version=4,
        title="Interview rescheduled",
        description="Candidate interview",
        start=datetime(2026, 10, 2, 8, tzinfo=UTC),
        end=datetime(2026, 10, 2, 8, 30, tzinfo=UTC),
        timezone="Asia/Gaza",
        attendee_emails=["candidate@example.com"],
    )
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, request=request, json={"id": "existing-event-id"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = GoogleCalendarProvider(settings(), client)
        assert (
            await provider.update_event("access-token", "existing-event-id", event)
            == "existing-event-id"
        )

    assert [request.method for request in calls] == ["PUT"]
    assert calls[0].url.path.endswith("/existing-event-id")


@pytest.mark.asyncio
async def test_calendar_reschedule_recreates_an_event_deleted_outside_talentflow() -> None:
    event = CalendarEvent(
        interview_id=uuid4(),
        schedule_version=5,
        title="Interview rescheduled",
        description="Candidate interview",
        start=datetime(2026, 10, 2, 8, tzinfo=UTC),
        end=datetime(2026, 10, 2, 8, 30, tzinfo=UTC),
        timezone="Asia/Gaza",
        attendee_emails=["candidate@example.com"],
    )
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "PUT":
            return httpx.Response(404, request=request)
        return httpx.Response(
            201,
            request=request,
            json={"id": deterministic_event_id(event)},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = GoogleCalendarProvider(settings(), client)
        assert (
            await provider.update_event("access-token", "deleted-event-id", event)
            == deterministic_event_id(event)
        )

    assert [request.method for request in calls] == ["PUT", "POST"]


@pytest.mark.asyncio
async def test_gmail_retry_returns_existing_message_without_sending_again() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            request=request,
            json={"messages": [{"id": "existing-google-message"}]},
        )

    message = EmailMessage(
        logical_message_id="tf-action@example.test",
        from_email="hr@example.com",
        to="candidate@example.com",
        subject="Interview invitation",
        body="Your interview is ready.",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = GmailProvider(settings(), client)
        assert await provider.send("access-token", message) == "existing-google-message"

    assert [request.method for request in calls] == ["GET"]
    assert calls[0].url.params["q"] == "rfc822msgid:<tf-action@example.test>"


def test_calendar_event_invites_candidate_and_assigned_interviewers() -> None:
    now = datetime(2026, 10, 1, 7, tzinfo=UTC)
    context = ActionContext(
        action_id=uuid4(),
        company_id=uuid4(),
        interview_id=uuid4(),
        provider_account_id=uuid4(),
        action=JobKind.CREATE_CALENDAR_EVENT.value,
        schedule_version=1,
        correlation_id=uuid4(),
        interview_status="scheduled",
        scheduled_at=now,
        scheduled_end_at=now.replace(minute=30),
        timezone="Asia/Gaza",
        candidate_name="Candidate",
        candidate_email="candidate@example.com",
        interviewer_emails=["hr@example.com", "candidate@example.com"],
        company_name="TalentFlow",
        position_title="Backend Engineer",
        invitation_url="https://talant-flow.app/interview/token",
        prior_provider_resource_id=None,
    )
    handler = SchedulingJobHandlers.__new__(SchedulingJobHandlers)
    event = handler._calendar_event(context)
    assert event.attendee_emails == ["candidate@example.com", "hr@example.com"]
