"""Idempotent external-side-effect handlers for orchestrator jobs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from talentflow_orchestrator.domain.models import JobKind, OrchestratorJob, ServiceError
from talentflow_orchestrator.persistence.integrations import (
    ActionContext,
    IntegrationRepository,
)
from talentflow_orchestrator.providers.base import (
    CalendarEvent,
    CalendarProvider,
    EmailMessage,
    EmailProvider,
    OAuthProvider,
    ProviderAccount,
)
from talentflow_orchestrator.queue.postgres import OrchestratorQueue

logger = logging.getLogger(__name__)


class AccessTokenManager:
    def __init__(self, integrations: IntegrationRepository, oauth: OAuthProvider) -> None:
        self.integrations = integrations
        self.oauth = oauth

    async def token(self, account_id: UUID, company_id: UUID) -> tuple[str, ProviderAccount]:
        account = await self.integrations.provider_account(account_id, company_id)
        now = datetime.now(UTC)
        if (
            account.access_token
            and account.access_token_expires_at
            and account.access_token_expires_at.astimezone(UTC) > now + timedelta(seconds=60)
        ):
            return account.access_token, account
        access_token, expires_at = await self.oauth.refresh(account.refresh_token)
        await self.integrations.update_access_token(account, access_token, expires_at)
        return access_token, account


class SchedulingJobHandlers:
    def __init__(
        self,
        *,
        queue: OrchestratorQueue,
        integrations: IntegrationRepository,
        tokens: AccessTokenManager,
        calendar: CalendarProvider,
        email: EmailProvider,
    ) -> None:
        self.queue = queue
        self.integrations = integrations
        self.tokens = tokens
        self.calendar = calendar
        self.email = email

    async def handle(self, job: OrchestratorJob) -> None:
        context = await self.integrations.begin_action(job)
        if context is None:
            return
        if not await self.queue.is_active_lease(job):
            raise ServiceError("job_cancelled", retryable=False)
        access_token, account = await self.tokens.token(
            context.provider_account_id, context.company_id
        )
        if not await self.queue.is_active_lease(job):
            raise ServiceError("job_cancelled", retryable=False)

        if job.kind in {
            JobKind.CREATE_CALENDAR_EVENT,
            JobKind.UPDATE_CALENDAR_EVENT,
            JobKind.SEND_INVITATION,
            JobKind.SEND_RESCHEDULE,
        } and context.invitation_url is None:
            raise ServiceError("invitation_unavailable", retryable=False)

        if job.kind == JobKind.CREATE_CALENDAR_EVENT:
            resource_id = await self.calendar.create_event(
                access_token, self._calendar_event(context)
            )
        elif job.kind == JobKind.UPDATE_CALENDAR_EVENT:
            event = self._calendar_event(context)
            if context.prior_provider_resource_id:
                resource_id = await self.calendar.update_event(
                    access_token, context.prior_provider_resource_id, event
                )
            else:
                resource_id = await self.calendar.create_event(access_token, event)
        elif job.kind == JobKind.CANCEL_CALENDAR_EVENT:
            if context.prior_provider_resource_id:
                await self.calendar.cancel_event(
                    access_token, context.prior_provider_resource_id
                )
                resource_id = context.prior_provider_resource_id
            else:
                resource_id = "no_event"
        elif job.kind in {
            JobKind.SEND_INVITATION,
            JobKind.SEND_RESCHEDULE,
            JobKind.SEND_CANCELLATION,
        }:
            resource_id = await self.email.send(
                access_token,
                self._email(context, job.kind, from_email=account.account_email),
            )
        else:
            raise ServiceError("unknown_job_kind", retryable=False)

        await self.integrations.action_succeeded(context, resource_id)
        event_name = {
            JobKind.CREATE_CALENDAR_EVENT: "calendar_event_created",
            JobKind.UPDATE_CALENDAR_EVENT: "calendar_event_updated",
            JobKind.CANCEL_CALENDAR_EVENT: "calendar_event_cancelled",
            JobKind.SEND_INVITATION: "invitation_email_sent",
            JobKind.SEND_RESCHEDULE: "reschedule_email_sent",
            JobKind.SEND_CANCELLATION: "cancellation_email_sent",
        }[job.kind]
        logger.info(
            event_name,
            extra={
                "company_id": str(context.company_id),
                "interview_id": str(context.interview_id),
                "correlation_id": str(context.correlation_id),
                "job_id": str(job.id),
            },
        )

    def _calendar_event(self, context: ActionContext) -> CalendarEvent:
        invite = context.invitation_url or ""
        attendees = list(
            dict.fromkeys(
                [
                    *([context.candidate_email] if context.candidate_email else []),
                    *context.interviewer_emails,
                ]
            )
        )
        try:
            return CalendarEvent(
                interview_id=context.interview_id,
                schedule_version=context.schedule_version,
                title=f"{context.company_name} — {context.position_title} interview",
                description=(
                    "TalentFlow interview invitation\n\n"
                    f"Open the waiting room: {invite}\n"
                    "Please sign in with the invited candidate account."
                ),
                start=context.scheduled_at,
                end=context.scheduled_end_at,
                timezone=context.timezone,
                attendee_emails=attendees,
            )
        except ValidationError as exc:
            raise ServiceError("calendar_payload_invalid", retryable=False) from exc

    def _email(
        self,
        context: ActionContext,
        kind: JobKind,
        *,
        from_email: str,
    ) -> EmailMessage:
        if context.candidate_email is None:
            raise ServiceError("candidate_email_missing", retryable=False)
        local_start = context.scheduled_at.astimezone(ZoneInfo(context.timezone))
        action_word = {
            JobKind.SEND_INVITATION: "Interview invitation",
            JobKind.SEND_RESCHEDULE: "Interview rescheduled",
            JobKind.SEND_CANCELLATION: "Interview cancelled",
        }[kind]
        if kind == JobKind.SEND_CANCELLATION:
            body = (
                f"Hello {context.candidate_name},\n\n"
                f"Your {context.position_title} interview with {context.company_name} has been cancelled.\n\n"
                "Please contact the company if you need assistance.\n"
            )
        else:
            body = (
                f"Hello {context.candidate_name},\n\n"
                f"Your {context.position_title} interview with {context.company_name} "
                f"is scheduled for {local_start:%Y-%m-%d %H:%M} ({context.timezone}).\n"
                f"Duration: {int((context.scheduled_end_at-context.scheduled_at).total_seconds()/60)} minutes.\n"
                f"Interview link: {context.invitation_url or ''}\n\n"
                "Please sign in with the invited account and join shortly before the start time.\n"
            )
        try:
            return EmailMessage(
                logical_message_id=(
                    f"tf-{context.action_id.hex}@notifications.talent-flow.app"
                ),
                from_email=from_email,
                to=context.candidate_email,
                subject=f"{action_word}: {context.position_title}",
                body=body,
            )
        except ValidationError as exc:
            raise ServiceError("email_payload_invalid", retryable=False) from exc
