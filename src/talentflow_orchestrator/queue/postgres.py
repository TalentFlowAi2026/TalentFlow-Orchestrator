"""Shared-table durable queue partitioned from Interview Engine jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from talentflow_orchestrator.domain.models import OrchestratorJob, ServiceError
from talentflow_orchestrator.persistence.postgres import Database


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(900.0, max(1.0, retry_after))
    return min(300.0, float(2 ** min(attempt, 8)))


class OrchestratorQueue:
    """Claim only orchestrator-prefixed states; the Interview Engine cannot see them."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def claim(self) -> OrchestratorJob | None:
        now = datetime.now(UTC)
        lease_token = uuid4()
        leased_until = now + timedelta(seconds=self.db.settings.job_lease_seconds)
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                WITH exhausted AS (
                  UPDATE worker_jobs
                  SET status='orchestrator_dead',completed_at=clock_timestamp(),
                      last_error=COALESCE(last_error,'lease_expired')
                  WHERE executor_service='scheduling_orchestrator'
                    AND attempt >= max_attempts
                    AND (
                      status='orchestrator_pending'
                      OR (status='orchestrator_leased' AND leased_until < $1)
                    )
                  RETURNING integration_action_id
                )
                UPDATE integration_actions ia
                SET status='failed',error_code='lease_expired',updated_at=clock_timestamp()
                FROM exhausted
                WHERE ia.id=exhausted.integration_action_id
                  AND ia.status IN ('pending','processing','retrying')
                """,
                now,
            )
            row = await conn.fetchrow(
                """
                WITH next_job AS (
                  SELECT id FROM worker_jobs
                  WHERE executor_service='scheduling_orchestrator'
                    AND attempt < max_attempts
                    AND (
                      (status='orchestrator_pending' AND delay_until <= $1)
                      OR (status='orchestrator_leased' AND leased_until < $1)
                    )
                  ORDER BY COALESCE(leased_until,delay_until),id
                  FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE worker_jobs w
                SET status='orchestrator_leased',lease_token=$2,leased_until=$3,
                    attempt=w.attempt+1,started_at=COALESCE(w.started_at,$1)
                FROM next_job WHERE w.id=next_job.id
                RETURNING w.*
                """,
                now,
                lease_token,
                leased_until,
            )
        if row is None:
            return None
        return OrchestratorJob(
            id=UUID(str(row["id"])),
            kind=str(row["kind"]),  # type: ignore[arg-type]
            company_id=UUID(str(row["company_id"])),
            interview_id=UUID(str(row["interview_id"])),
            proposal_id=UUID(str(row["proposal_id"])) if row["proposal_id"] else None,
            integration_action_id=UUID(str(row["integration_action_id"])),
            correlation_id=UUID(str(row["correlation_id"])),
            idempotency_key=str(row["idempotency_key"]),
            expected_schedule_version=int(row["expected_schedule_version"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            lease_token=UUID(str(row["lease_token"])),
            payload=dict(row["payload"] or {}),
        )

    async def complete(self, job: OrchestratorJob) -> bool:
        async with self.db.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE worker_jobs SET status='orchestrator_completed',completed_at=clock_timestamp()
                WHERE id=$1 AND lease_token=$2 AND status='orchestrator_leased'
                """,
                job.id,
                job.lease_token,
            )
        return result == "UPDATE 1"

    async def fail(
        self,
        job: OrchestratorJob,
        code: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
    ) -> bool:
        sanitized = code if code.isidentifier() and len(code) <= 120 else "provider_failure"
        async with self.db.pool.acquire() as conn:
            if retryable and job.attempt < job.max_attempts:
                result = await conn.execute(
                    """
                    UPDATE worker_jobs
                    SET status='orchestrator_pending',delay_until=$1,last_error=$2,
                        lease_token=NULL,leased_until=NULL
                    WHERE id=$3 AND lease_token=$4 AND status='orchestrator_leased'
                    """,
                    datetime.now(UTC) + timedelta(seconds=retry_delay(job.attempt, retry_after)),
                    sanitized,
                    job.id,
                    job.lease_token,
                )
            else:
                result = await conn.execute(
                    """
                    UPDATE worker_jobs
                    SET status='orchestrator_dead',completed_at=clock_timestamp(),last_error=$1
                    WHERE id=$2 AND lease_token=$3 AND status='orchestrator_leased'
                    """,
                    sanitized,
                    job.id,
                    job.lease_token,
                )
        return result == "UPDATE 1"

    async def cancel(self, job_id: UUID) -> bool:
        async with self.db.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE worker_jobs SET status='orchestrator_cancelled',completed_at=clock_timestamp()
                WHERE id=$1 AND executor_service='scheduling_orchestrator'
                  AND status IN ('orchestrator_pending','orchestrator_leased')
                """,
                job_id,
            )
        return result == "UPDATE 1"

    async def is_active_lease(self, job: OrchestratorJob) -> bool:
        async with self.db.pool.acquire() as conn:
            active = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM worker_jobs
                  WHERE id=$1 AND lease_token=$2 AND status='orchestrator_leased'
                    AND leased_until>clock_timestamp()
                )
                """,
                job.id,
                job.lease_token,
            )
        return bool(active)

    async def depth(self) -> int:
        async with self.db.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(*) FROM worker_jobs
                WHERE executor_service='scheduling_orchestrator'
                  AND status='orchestrator_pending'
                """
            )
        return int(value or 0)

    async def ensure_ready(self) -> None:
        try:
            await self.depth()
        except Exception as exc:
            raise ServiceError("orchestrator_queue_unavailable") from exc
