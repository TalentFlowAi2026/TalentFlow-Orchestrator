from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import (
    JobKind,
    OrchestratorJob,
    ServiceError,
)
from talentflow_orchestrator.queue.postgres import OrchestratorQueue
from talentflow_orchestrator.workers.consumer import OrchestratorConsumer
from talentflow_orchestrator.workers.handlers import SchedulingJobHandlers


def test_orchestrator_queue_states_are_invisible_to_interview_engine_claim() -> None:
    root = Path(__file__).resolve().parents[3]
    interview_queue = (
        root / "python-worker" / "src" / "talentflow_worker" / "queue" / "pgmq.py"
    ).read_text(encoding="utf-8")
    orchestrator_migration = (
        root / "supabase" / "migrations" / "202609230005_scheduling_orchestrator.sql"
    ).read_text(encoding="utf-8")

    assert "status = 'pending'" in interview_queue
    assert "orchestrator_pending" not in interview_queue
    assert "'orchestrator_pending'" in orchestrator_migration
    assert "executor_service = 'scheduling_orchestrator'" in orchestrator_migration


def test_orchestrator_completion_requires_an_active_lease() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "talentflow_orchestrator"
        / "queue"
        / "postgres.py"
    ).read_text(encoding="utf-8")
    assert "status='orchestrator_leased'" in source
    assert "lease_token=$2" in source
    assert "attempt < max_attempts" in source
    assert "last_error=COALESCE(last_error,'lease_expired')" in source
    assert "leased_until>clock_timestamp()" in source


class FakeIntegrations:
    def __init__(self) -> None:
        self.failed: tuple[object, str, bool] | None = None

    async def action_failed(
        self, action_id: object, code: str, retryable: bool
    ) -> None:
        self.failed = (action_id, code, retryable)


class FailingHandlers:
    def __init__(self) -> None:
        self.integrations = FakeIntegrations()

    async def handle(self, job: OrchestratorJob) -> None:
        del job
        raise ServiceError("gmail_unavailable", retryable=True)


class FakeQueue:
    def __init__(self, job: OrchestratorJob) -> None:
        self.job = job
        self.failure: tuple[str, bool] | None = None

    async def claim(self) -> OrchestratorJob:
        return self.job

    async def complete(self, job: OrchestratorJob) -> bool:
        del job
        return True

    async def fail(
        self,
        job: OrchestratorJob,
        code: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
    ) -> bool:
        del job, retry_after
        self.failure = (code, retryable)
        return True


@pytest.mark.asyncio
async def test_partial_provider_failure_is_recorded_and_requeued() -> None:
    action_id = uuid4()
    job = OrchestratorJob(
        id=uuid4(),
        kind=JobKind.SEND_INVITATION,
        company_id=uuid4(),
        interview_id=uuid4(),
        integration_action_id=action_id,
        correlation_id=uuid4(),
        idempotency_key="retryable-email",
        expected_schedule_version=1,
        attempt=1,
        lease_token=uuid4(),
    )
    queue = FakeQueue(job)
    handlers = FailingHandlers()
    consumer = OrchestratorConsumer(
        queue=cast(OrchestratorQueue, cast(Any, queue)),
        handlers=cast(SchedulingJobHandlers, cast(Any, handlers)),
        settings=Settings.model_validate({}),
    )

    assert await consumer.step()
    assert handlers.integrations.failed == (action_id, "gmail_unavailable", True)
    assert queue.failure == ("gmail_unavailable", True)


@pytest.mark.asyncio
async def test_final_retry_marks_integration_action_terminal() -> None:
    action_id = uuid4()
    job = OrchestratorJob(
        id=uuid4(),
        kind=JobKind.SEND_INVITATION,
        company_id=uuid4(),
        interview_id=uuid4(),
        integration_action_id=action_id,
        correlation_id=uuid4(),
        idempotency_key="terminal-email",
        expected_schedule_version=1,
        attempt=5,
        max_attempts=5,
        lease_token=uuid4(),
    )
    queue = FakeQueue(job)
    handlers = FailingHandlers()
    consumer = OrchestratorConsumer(
        queue=cast(OrchestratorQueue, cast(Any, queue)),
        handlers=cast(SchedulingJobHandlers, cast(Any, handlers)),
        settings=Settings.model_validate({}),
    )

    assert await consumer.step()
    assert handlers.integrations.failed == (action_id, "gmail_unavailable", False)
    assert queue.failure == ("gmail_unavailable", True)
