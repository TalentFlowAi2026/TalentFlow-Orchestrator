"""Independent Scheduling Orchestrator background process."""

from __future__ import annotations

import asyncio
import logging
import signal
from uuid import UUID

from talentflow_orchestrator.config.logging import configure_logging
from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import OrchestratorJob, ServiceError
from talentflow_orchestrator.persistence.integrations import IntegrationRepository
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.providers.disabled import (
    DisabledCalendarProvider,
    DisabledEmailProvider,
)
from talentflow_orchestrator.providers.google.calendar import GoogleCalendarProvider
from talentflow_orchestrator.providers.google.gmail import GmailProvider
from talentflow_orchestrator.providers.google.oauth import GoogleOAuthProvider
from talentflow_orchestrator.queue.postgres import OrchestratorQueue
from talentflow_orchestrator.workers.handlers import AccessTokenManager, SchedulingJobHandlers

logger = logging.getLogger(__name__)


class OrchestratorConsumer:
    def __init__(
        self,
        *,
        queue: OrchestratorQueue,
        handlers: SchedulingJobHandlers,
        settings: Settings,
    ) -> None:
        self.queue = queue
        self.handlers = handlers
        self.settings = settings
        self._stopping = False

    async def _record_failure(
        self,
        job: OrchestratorJob,
        *,
        action_id: UUID,
        code: str,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        try:
            will_retry = retryable and job.attempt < job.max_attempts
            await self.handlers.integrations.action_failed(
                action_id, code, will_retry
            )
            await self.queue.fail(
                job,
                code,
                retryable=retryable,
                retry_after=retry_after,
            )
        except Exception:
            # Leave the lease to expire. Another consumer can safely reclaim the
            # idempotent action after persistence becomes available again.
            logger.exception(
                "orchestrator_failure_recording_failed",
                extra={
                    "job_id": str(job.id),
                    "correlation_id": str(job.correlation_id),
                    "error_code": code,
                },
            )

    async def step(self) -> bool:
        job = await self.queue.claim()
        if job is None:
            return False
        logger.info(
            "orchestrator_job_claimed",
            extra={
                "job_id": str(job.id),
                "company_id": str(job.company_id),
                "interview_id": str(job.interview_id),
                "correlation_id": str(job.correlation_id),
                "action": job.kind.value,
                "attempt": job.attempt,
            },
        )
        try:
            async with asyncio.timeout(self.settings.job_timeout_seconds):
                await self.handlers.handle(job)
            completed = await self.queue.complete(job)
            if completed:
                logger.info(
                    "orchestrator_job_completed",
                    extra={
                        "job_id": str(job.id),
                        "correlation_id": str(job.correlation_id),
                        "action": job.kind.value,
                    },
                )
            return True
        except ServiceError as exc:
            await self._record_failure(
                job,
                action_id=job.integration_action_id,
                code=exc.code,
                retryable=exc.retryable,
                retry_after=exc.retry_after,
            )
            logger.warning(
                {
                    "create_calendar_event": "calendar_event_failed",
                    "update_calendar_event": "calendar_event_update_failed",
                    "cancel_calendar_event": "calendar_event_cancellation_failed",
                    "send_interview_invitation": "invitation_email_failed",
                    "send_reschedule_email": "reschedule_email_failed",
                    "send_cancellation_email": "cancellation_email_failed",
                }.get(job.kind.value, "orchestrator_job_failed"),
                extra={
                    "job_id": str(job.id),
                    "correlation_id": str(job.correlation_id),
                    "action": job.kind.value,
                    "error_code": exc.code,
                    "attempt": job.attempt,
                },
            )
            return True
        except TimeoutError:
            await self._record_failure(
                job,
                action_id=job.integration_action_id,
                code="provider_timeout",
                retryable=True,
            )
            logger.warning(
                "orchestrator_provider_timeout",
                extra={
                    "job_id": str(job.id),
                    "correlation_id": str(job.correlation_id),
                    "action": job.kind.value,
                },
            )
            return True
        except Exception:
            logger.exception(
                "orchestrator_job_unexpected_failure",
                extra={
                    "job_id": str(job.id),
                    "correlation_id": str(job.correlation_id),
                    "action": job.kind.value,
                },
            )
            await self._record_failure(
                job,
                action_id=job.integration_action_id,
                code="unexpected_handler_failure",
                retryable=True,
            )
            return True

    async def run(self, stop_event: asyncio.Event) -> None:
        async def loop() -> None:
            while not self._stopping and not stop_event.is_set():
                try:
                    processed = await self.step()
                except Exception:
                    logger.exception("orchestrator_consumer_loop_failure")
                    processed = False
                if not processed:
                    await asyncio.sleep(self.settings.worker_poll_seconds)

        tasks = [asyncio.create_task(loop()) for _ in range(self.settings.worker_concurrency)]
        try:
            await stop_event.wait()
        finally:
            self._stopping = True
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.settings.shutdown_grace_seconds,
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stopping = True


async def async_main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    missing = settings.missing("background")
    if missing:
        logger.error("orchestrator_configuration_missing", extra={"error_code": "config_missing"})
        raise RuntimeError("Missing required background configuration: " + ", ".join(missing))
    database = Database(settings, pool_max=settings.background_database_pool_max)
    await database.open()
    await database.validate_schema()
    queue = OrchestratorQueue(database)
    await queue.ensure_ready()
    integrations = IntegrationRepository(database, settings)
    oauth = GoogleOAuthProvider(settings)
    calendar = (
        GoogleCalendarProvider(settings)
        if settings.google_calendar_enabled
        else DisabledCalendarProvider()
    )
    email = GmailProvider(settings) if settings.gmail_enabled else DisabledEmailProvider()
    handlers = SchedulingJobHandlers(
        queue=queue,
        integrations=integrations,
        tokens=AccessTokenManager(integrations, oauth),
        calendar=calendar,
        email=email,
    )
    consumer = OrchestratorConsumer(queue=queue, handlers=handlers, settings=settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stop() -> None:
        consumer.stop()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            pass
    try:
        await consumer.run(stop_event)
    finally:
        await calendar.aclose()
        await email.aclose()
        await oauth.aclose()
        await database.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
