"""System SMTP sends interview mail without per-company Google OAuth."""

from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import JobKind, OrchestratorJob
from talentflow_orchestrator.persistence.integrations import ActionContext
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.persistence.repository import SchedulingRepository
from talentflow_orchestrator.providers.base import EmailMessage
from talentflow_orchestrator.providers.smtp import SMTPProvider
from talentflow_orchestrator.workers.handlers import SchedulingJobHandlers


def smtp_settings() -> Settings:
    return Settings.model_validate({
        "smtp_enabled": True,
        "smtp_host": "smtp.test.invalid",
        "smtp_port": 587,
        "smtp_username": "system@test.invalid",
        "smtp_password": "fake-not-secret",
        "smtp_from_address": "system@test.invalid",
    })


@pytest.mark.asyncio
async def test_smtp_transport_builds_html_calendar_message_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SMTPProvider(smtp_settings())
    recorded = []
    monkeypatch.setattr(provider, "_deliver", lambda mime: recorded.append(mime.as_bytes()))
    msg = EmailMessage(
        logical_message_id="tf-stable-id@notifications.talent-flow.app",
        from_email="system@test.invalid",
        to="candidate@test.invalid",
        subject="Interview invitation",
        body="Your interview was scheduled.",
        html_body="<strong>TalentFlow AI</strong>",
        calendar_ics="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    )
    assert await provider.send("", msg) == msg.logical_message_id
    assert len(recorded) == 1
    parsed = BytesParser(policy=policy.default).parsebytes(recorded[0])
    assert parsed["Message-ID"] == "<tf-stable-id@notifications.talent-flow.app>"
    types = [part.get_content_type() for part in parsed.walk()]
    assert "text/plain" in types and "text/html" in types and "text/calendar" in types
    assert next(parsed.iter_attachments()).get_filename() == "talentflow-interview.ics"


class RecordingConnection:
    def __init__(self) -> None:
        self.commands = []

    async def execute(self, statement: str, *args: object) -> str:
        self.commands.append((statement, args))
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_confirm_outbox_enqueue_no_google_provider_account() -> None:
    repo = SchedulingRepository(Database(smtp_settings()), smtp_settings())
    conn = RecordingConnection()
    await repo._create_provider_actions(
        conn, company_id=uuid4(), interview_id=uuid4(), proposal_id=uuid4(),
        provider_account_id=None, schedule_version=1, correlation_id=uuid4(),
        kinds=(JobKind.CREATE_CALENDAR_EVENT, JobKind.SEND_INVITATION),
    )
    assert len(conn.commands) == 2  # one integration action, one queue job
    action_sql, action_args = conn.commands[0]
    assert "INSERT INTO integration_actions" in action_sql
    assert action_args[3] is None
    assert action_args[4:6] == ("smtp", "send_invitation")
    _, queue_args = conn.commands[1]
    assert queue_args[1] == JobKind.SEND_INVITATION.value


@pytest.mark.asyncio
async def test_smtp_handler_does_not_request_oauth_token() -> None:
    interview = uuid4()
    action = uuid4()
    company = uuid4()
    now = datetime(2026, 10, 29, 7, tzinfo=UTC)
    context = ActionContext(
        action_id=action, company_id=company, interview_id=interview,
        provider_account_id=None, provider="smtp", action="send_invitation",
        schedule_version=1, correlation_id=uuid4(), interview_status="scheduled",
        scheduled_at=now, scheduled_end_at=now + timedelta(minutes=45),
        timezone="UTC", candidate_name="Candidate", candidate_email="candidate@test.invalid",
        interviewer_emails=[], company_name="Company", position_title="Developer",
        invitation_url=None, prior_provider_resource_id=None,
    )

    class FakeRepo:
        def __init__(self) -> None:
            self.recorded = None
        async def begin_action(self, job: OrchestratorJob) -> ActionContext:
            return context
        async def action_succeeded(self, action_context: ActionContext, resource_id: str) -> None:
            self.recorded = (action_context, resource_id)

    class FakeQueue:
        async def is_active_lease(self, job: OrchestratorJob) -> bool:
            return True

    class FakeTokens:
        async def token(self, *args: object) -> None:
            raise AssertionError("OAuth requested for SMTP")

    class FakeEmail:
        def __init__(self) -> None:
            self.sent = []
        async def send(self, token: str, msg: EmailMessage) -> str:
            self.sent.append(msg)
            return msg.logical_message_id

    repo = FakeRepo()
    smtp = FakeEmail()
    handler = SchedulingJobHandlers(
        queue=FakeQueue(), integrations=repo, tokens=FakeTokens(),
        calendar=None, email=None, smtp_email=smtp, smtp_from_email="system@test.invalid",
    )
    job = OrchestratorJob(
        id=uuid4(), kind=JobKind.SEND_INVITATION, company_id=company,
        interview_id=interview, integration_action_id=action,
        correlation_id=uuid4(), idempotency_key="smtp-test", expected_schedule_version=1,
    )
    await handler.handle(job)
    assert len(smtp.sent) == 1
    assert smtp.sent[0].from_email == "system@test.invalid"
    assert smtp.sent[0].to == "candidate@test.invalid"
    assert "Add to Google Calendar" in smtp.sent[0].html_body
    assert repo.recorded is not None
