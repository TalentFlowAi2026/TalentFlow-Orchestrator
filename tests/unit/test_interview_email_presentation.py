"""Interview emails match Laravel styling and provide portable calendar actions."""

import base64
import json
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import JobKind
from talentflow_orchestrator.providers.base import EmailMessage
from talentflow_orchestrator.providers.google.gmail import GmailProvider
from talentflow_orchestrator.workers.interview_email import (
    calendar_ics,
    google_calendar_url,
    render_email_html,
)


def test_calendar_button_prefills_correct_time_and_escapes_dynamic_fields() -> None:
    start = datetime(2026, 10, 29, 7, tzinfo=UTC)
    end = start + timedelta(minutes=45)
    url = google_calendar_url(
        position="Flutter Developer", company="TalentFlow", start=start, end=end, timezone="Asia/Gaza"
    )
    params = parse_qs(urlsplit(url).query)
    assert params["action"] == ["TEMPLATE"]
    assert params["dates"] == ["20261029T070000Z/20261029T074500Z"]
    assert params["ctz"] == ["Asia/Gaza"]

    rendered = render_email_html(
        candidate='<script>alert(1)</script>',
        company="ACME & Sons",
        position="Flutter Developer",
        start=start,
        timezone="Asia/Gaza",
        kind=JobKind.SEND_INVITATION,
        calendar_url=url,
    )
    assert "Your interview has been scheduled" in rendered
    assert "Add to Google Calendar" in rendered
    assert "#246bfd" in rendered
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert "ACME &amp; Sons" in rendered
    assert "Please open the TalentFlow AI mobile app" in rendered
    assert 'href="https://calendar.google.com/calendar/render?' in rendered


def test_calendar_attachment_has_stable_uid_across_reschedules() -> None:
    interview_id = uuid4()
    start = datetime(2026, 10, 29, 7, tzinfo=UTC)
    original = calendar_ics(
        interview_id=interview_id,
        version=1,
        company="TalentFlow",
        position="Developer",
        start=start,
        end=start + timedelta(minutes=30),
    )
    cancelled = calendar_ics(
        interview_id=interview_id,
        version=2,
        company="TalentFlow",
        position="Developer",
        start=start,
        end=start + timedelta(minutes=30),
        cancelled=True,
    )
    uid = f"UID:talentflow-interview-{interview_id}@talant-flow.app"
    assert uid in original and uid in cancelled
    assert "METHOD:PUBLISH\r\n" in original
    assert "SEQUENCE:2\r\n" in cancelled
    assert "METHOD:CANCEL\r\n" in cancelled
    assert "STATUS:CANCELLED\r\n" in cancelled


def test_cancelled_email_does_not_offer_calendar_add() -> None:
    html = render_email_html(
        candidate="Candidate",
        company="TalentFlow",
        position="Developer",
        start=datetime(2026, 10, 29, 7, tzinfo=UTC),
        timezone="Asia/Gaza",
        kind=JobKind.SEND_CANCELLATION,
        calendar_url=None,
    )
    assert "Your interview has been cancelled" in html
    assert "Add to Google Calendar" not in html


@pytest.mark.asyncio
async def test_gmail_sends_multipart_html_and_calendar_without_changing_message_id() -> None:
    captured = []

    def respond(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": "gmail-123"})

    key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    settings = Settings.model_validate({"environment": "test", "data_encryption_key": key})
    message = EmailMessage(
        logical_message_id="tf-action@notifications.talent-flow.app",
        from_email="hr@example.com",
        to="candidate@example.com",
        subject="Interview invitation",
        body="Open TalentFlow AI mobile app.",
        html_body="<html><body><strong>TalentFlow AI</strong></body></html>",
        calendar_ics="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = GmailProvider(settings, client)
        assert await provider.send("access-token", message) == "gmail-123"
    assert [r.method for r in captured] == ["GET", "POST"]
    wire = json.loads(captured[1].content)["raw"]
    wire += "=" * (-len(wire) % 4)
    mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(wire))
    assert mime["Message-ID"] == "<tf-action@notifications.talent-flow.app>"
    assert mime.is_multipart()
    types = [part.get_content_type() for part in mime.walk()]
    assert "text/plain" in types
    assert "text/html" in types
    assert "text/calendar" in types
    attachments = list(mime.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "talentflow-interview.ics"
