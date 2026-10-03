"""Tenant-safe scheduling persistence and transactional outbox workflows."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import Actor, JobKind, ServiceError
from talentflow_orchestrator.intent.models import SchedulingIntent
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.scheduling.engine import SchedulingEngine
from talentflow_orchestrator.scheduling.models import (
    BusyInterval,
    BusyKind,
    CompanySchedulingSettings,
    ProposalDraft,
    SchedulingMode,
    SchedulingPolicy,
    SchedulingRequest,
)
from talentflow_orchestrator.security.crypto import (
    EncryptedValue,
    SecretCipher,
    new_opaque_token,
    token_digest,
)

if TYPE_CHECKING:
    DbConnection = asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record]
else:
    DbConnection = Any


class SchedulingRepository:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.engine = SchedulingEngine()
        self.invitation_cipher: SecretCipher | None = None
        if settings.data_encryption_key.get_secret_value():
            self.invitation_cipher = SecretCipher(settings, "invitation")

    async def require_scheduling_manager(self, user_id: UUID, company_id: UUID) -> Actor:
        async with self.db.pool.acquire() as conn:
            return await self._require_scheduling_manager(conn, user_id, company_id)

    async def require_company_admin(self, user_id: UUID, company_id: UUID) -> Actor:
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT cm.role::text AS role
                FROM company_members cm
                WHERE cm.company_id=$1 AND cm.user_id=$2 AND cm.role='company_admin'
                """,
                company_id,
                user_id,
            )
        if row is None:
            raise ServiceError("unauthorized", status=403, retryable=False)
        return Actor(user_id=user_id, company_id=company_id, role=str(row["role"]))

    async def _require_scheduling_manager(
        self,
        conn: DbConnection,
        user_id: UUID,
        company_id: UUID,
    ) -> Actor:
        row = await conn.fetchrow(
            """
            SELECT cm.role::text AS role
            FROM company_members cm
            WHERE cm.company_id=$1 AND cm.user_id=$2
              AND cm.role IN ('company_admin','interviewer')
            """,
            company_id,
            user_id,
        )
        if row is None:
            raise ServiceError("unauthorized", status=403, retryable=False)
        return Actor(user_id=user_id, company_id=company_id, role=str(row["role"]))

    async def policy(self, company_id: UUID) -> SchedulingPolicy:
        async with self.db.pool.acquire() as conn:
            return await self._policy(conn, company_id)

    async def company_timezone(self, company_id: UUID) -> str | None:
        async with self.db.pool.acquire() as conn:
            scheduling = await self._company_settings(conn, company_id)
        return scheduling.timezone

    async def _company_settings(
        self,
        conn: DbConnection,
        company_id: UUID,
    ) -> CompanySchedulingSettings:
        settings = await conn.fetchval("SELECT settings FROM companies WHERE id=$1", company_id)
        if settings is None:
            raise ServiceError("company_not_found", status=404, retryable=False)
        document = json.loads(settings) if isinstance(settings, str) else dict(settings)
        try:
            return CompanySchedulingSettings.model_validate(
                document.get("scheduling", {})
            )
        except Exception as exc:
            raise ServiceError(
                "company_scheduling_policy_invalid", status=422, retryable=False
            ) from exc

    async def _policy(
        self,
        conn: DbConnection,
        company_id: UUID,
    ) -> SchedulingPolicy:
        company_policy = await self._company_settings(conn, company_id)
        return company_policy.to_policy(
            self.settings.backend_max_parallel_ai_interviews,
            default_day_start=self.settings.default_interview_day_start,
            default_day_end=self.settings.default_interview_day_end,
        )

    async def validate_scope(self, actor: Actor, request: SchedulingRequest) -> None:
        async with self.db.pool.acquire() as conn:
            await self._validate_scope(conn, actor, request)

    async def resolve_job_posting_for_role(
        self,
        *,
        actor: Actor,
        job_role_id: UUID,
        candidate_id: UUID,
    ) -> UUID:
        """Resolve the newest company posting for a role and ensure candidate assignment."""
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            role_exists = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM job_roles
                  WHERE id=$1 AND (company_id IS NULL OR company_id=$2) AND is_active=true
                )
                """,
                job_role_id,
                actor.company_id,
            )
            if not role_exists:
                raise ServiceError("job_role_not_found", status=404, retryable=False)

            job_id = await conn.fetchval(
                """
                SELECT id
                FROM job_postings
                WHERE company_id=$1 AND job_role_id=$2 AND status<>'archived'
                ORDER BY created_at DESC,id DESC
                LIMIT 1
                """,
                actor.company_id,
                job_role_id,
            )
            if job_id is None:
                raise ServiceError("position_not_found", status=404, retryable=False)

            candidate_exists = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1
                  FROM profiles p
                  WHERE p.id=$1 AND p.role='candidate'
                    AND (
                      EXISTS (
                        SELECT 1 FROM company_candidates cc
                        WHERE cc.company_id=$2 AND cc.candidate_id=p.id AND cc.is_active=true
                      )
                      OR EXISTS (
                        SELECT 1 FROM job_candidates jc
                        WHERE jc.company_id=$2 AND jc.candidate_id=p.id
                      )
                      OR EXISTS (
                        SELECT 1 FROM interviews i
                        WHERE i.company_id=$2 AND i.candidate_id=p.id
                      )
                    )
                )
                """,
                candidate_id,
                actor.company_id,
            )
            if not candidate_exists:
                raise ServiceError("candidate_not_found", status=404, retryable=False)

            await conn.execute(
                """
                INSERT INTO job_candidates(company_id,job_id,candidate_id,added_by)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (company_id,job_id,candidate_id) DO NOTHING
                """,
                actor.company_id,
                job_id,
                candidate_id,
                actor.user_id,
            )
            return UUID(str(job_id))


    async def assign_candidates(
        self,
        *,
        actor: Actor,
        job_id: UUID,
        candidate_ids: list[UUID],
    ) -> list[UUID]:
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            job_exists = await conn.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM job_postings
                  WHERE company_id=$1 AND id=$2 AND status<>'archived'
                )
                """,
                actor.company_id,
                job_id,
            )
            if not job_exists:
                raise ServiceError("position_not_found", status=404, retryable=False)
            rows = await conn.fetch(
                """
                SELECT p.id FROM profiles p
                WHERE p.role='candidate' AND p.id=ANY($1::uuid[])
                  AND (
                    EXISTS (
                      SELECT 1 FROM interviews i
                      WHERE i.company_id=$2 AND i.candidate_id=p.id
                    )
                    OR EXISTS (
                      SELECT 1 FROM job_candidates known
                      WHERE known.company_id=$2 AND known.candidate_id=p.id
                    )
                  )
                """,
                candidate_ids,
                actor.company_id,
            )
            found = {UUID(str(row["id"])) for row in rows}
            if found != set(candidate_ids):
                raise ServiceError("candidate_not_found", status=404, retryable=False)
            for candidate_id in candidate_ids:
                await conn.execute(
                    """
                    INSERT INTO job_candidates(company_id,job_id,candidate_id,added_by)
                    VALUES ($1,$2,$3,$4)
                    ON CONFLICT (company_id,job_id,candidate_id) DO NOTHING
                    """,
                    actor.company_id,
                    job_id,
                    candidate_id,
                    actor.user_id,
                )
        return candidate_ids

    async def _validate_scope(
        self,
        conn: DbConnection,
        actor: Actor,
        request: SchedulingRequest,
    ) -> None:
        await self._require_scheduling_manager(conn, actor.user_id, request.company_id)
        company_settings = await self._company_settings(conn, request.company_id)
        if company_settings.timezone and company_settings.timezone != request.timezone:
            raise ServiceError("company_timezone_mismatch", status=422, retryable=False)
        job = await conn.fetchrow(
            "SELECT id FROM job_postings WHERE company_id=$1 AND id=$2 AND status <> 'archived'",
            request.company_id,
            request.job_id,
        )
        if job is None:
            raise ServiceError("position_not_found", status=404, retryable=False)

        candidate_rows = await conn.fetch(
            """
            SELECT jc.candidate_id
            FROM job_candidates jc
            JOIN profiles p ON p.id=jc.candidate_id AND p.role='candidate'
            WHERE jc.company_id=$1 AND jc.job_id=$2 AND jc.candidate_id=ANY($3::uuid[])
            """,
            request.company_id,
            request.job_id,
            request.candidate_ids,
        )
        authorized_candidates = {UUID(str(row["candidate_id"])) for row in candidate_rows}
        if authorized_candidates != set(request.candidate_ids):
            raise ServiceError("candidate_not_found", status=404, retryable=False)

        if request.interviewer_ids:
            interviewer_rows = await conn.fetch(
                """
                SELECT user_id FROM company_members
                WHERE company_id=$1 AND role IN ('company_admin','interviewer')
                  AND user_id=ANY($2::uuid[])
                """,
                request.company_id,
                request.interviewer_ids,
            )
            authorized_interviewers = {UUID(str(row["user_id"])) for row in interviewer_rows}
            if authorized_interviewers != set(request.interviewer_ids):
                raise ServiceError("interviewer_not_found", status=404, retryable=False)

        if request.prompt_template_id is not None:
            template = await conn.fetchrow(
                """
                SELECT id FROM prompt_templates
                WHERE id=$1
                  AND (
                    company_id IS NULL
                    OR (company_id=$2 AND (job_id IS NULL OR job_id=$3))
                  )
                """,
                request.prompt_template_id,
                request.company_id,
                request.job_id,
            )
            if template is None:
                raise ServiceError("interview_configuration_not_found", status=404, retryable=False)

    async def busy_intervals(
        self, request: SchedulingRequest, *, exclude_interview_id: UUID | None = None
    ) -> list[BusyInterval]:
        async with self.db.pool.acquire() as conn:
            return await self._busy_intervals(conn, request, exclude_interview_id=exclude_interview_id)

    async def calendar_availability_context(
        self,
        actor: Actor,
        request: SchedulingRequest,
    ) -> tuple[UUID, dict[UUID, str]] | None:
        if not self.settings.google_calendar_enabled or not request.interviewer_ids:
            return None
        async with self.db.pool.acquire() as conn:
            await self._validate_scope(conn, actor, request)
            account_id = await self._provider_account_id(conn, request.company_id)
            if account_id is None:
                return None
            rows = await conn.fetch(
                """
                SELECT cm.user_id,p.email FROM company_members cm
                JOIN profiles p ON p.id=cm.user_id
                WHERE cm.company_id=$1 AND cm.user_id=ANY($2::uuid[])
                """,
                request.company_id,
                request.interviewer_ids,
            )
        calendars: dict[UUID, str] = {}
        for row in rows:
            if not row["email"]:
                raise ServiceError("interviewer_calendar_unavailable", status=422, retryable=False)
            calendars[UUID(str(row["user_id"]))] = str(row["email"])
        if set(calendars) != set(request.interviewer_ids):
            raise ServiceError("interviewer_not_found", status=404, retryable=False)
        return account_id, calendars

    async def _provider_account_id(
        self,
        conn: DbConnection,
        company_id: UUID,
        interview_id: UUID | None = None,
    ) -> UUID | None:
        if interview_id is not None:
            existing = await conn.fetchval(
                """
                SELECT pa.id
                FROM integration_actions ia
                JOIN interview_private.provider_accounts pa
                  ON pa.id=ia.provider_account_id AND pa.company_id=ia.company_id
                WHERE ia.company_id=$1 AND ia.interview_id=$2
                  AND pa.provider='google' AND pa.status='active'
                ORDER BY ia.schedule_version DESC,ia.created_at DESC LIMIT 1
                """,
                company_id,
                interview_id,
            )
            if existing is not None:
                return UUID(str(existing))
        latest = await conn.fetchval(
            """
            SELECT id FROM interview_private.provider_accounts
            WHERE company_id=$1 AND provider='google' AND status='active'
            ORDER BY updated_at DESC,id LIMIT 1
            """,
            company_id,
        )
        return UUID(str(latest)) if latest is not None else None

    async def _busy_intervals(
        self,
        conn: DbConnection,
        request: SchedulingRequest,
        *,
        exclude_interview_id: UUID | None = None,
    ) -> list[BusyInterval]:
        zone = ZoneInfo(request.timezone)
        timezone_start = datetime.combine(
            request.start_date, time.min, tzinfo=zone
        ).astimezone(UTC)
        timezone_end = datetime.combine(
            request.end_date + timedelta(days=1), time.min, tzinfo=zone
        ).astimezone(UTC)
        rows = await conn.fetch(
            """
            SELECT i.id,i.company_id,i.candidate_id,i.scheduled_at,
              COALESCE(
                i.scheduled_end_at,
                i.scheduled_at + make_interval(mins => COALESCE(
                  NULLIF(wic.config->>'duration_minutes','')::integer,
                  pt.time_limit_minutes,
                  180
                ))
              ) AS scheduled_end_at,
              i.scheduling_buffer_minutes
            FROM interviews i
            LEFT JOIN worker_interview_configs wic ON wic.interview_id=i.id
            LEFT JOIN prompt_templates pt ON pt.id=i.prompt_template_id
            WHERE i.status IN ('scheduled','in_progress')
              AND i.company_id IS NOT NULL
              AND i.scheduled_at < $2 AND COALESCE(i.scheduled_end_at,i.scheduled_at + interval '180 minutes') > $1
              AND ($3::uuid IS NULL OR i.id<>$3)
            """,
            timezone_start,
            timezone_end,
            exclude_interview_id,
        )
        result: list[BusyInterval] = []
        interview_ids: list[UUID] = []
        windows: dict[UUID, tuple[datetime, datetime, int, bool]] = {}
        for row in rows:
            interview_id = UUID(str(row["id"]))
            interview_ids.append(interview_id)
            start = row["scheduled_at"].astimezone(UTC)
            end = row["scheduled_end_at"].astimezone(UTC)
            stored_buffer = max(0, int(row["scheduling_buffer_minutes"]))
            same_company = UUID(str(row["company_id"])) == request.company_id
            safe_reference = interview_id if same_company else None
            windows[interview_id] = (start, end, stored_buffer, same_company)
            result.append(
                BusyInterval(
                    start=start,
                    end=end,
                    kind=BusyKind.CAPACITY,
                    source="backend_capacity",
                    reference_id=safe_reference,
                    buffer_minutes=stored_buffer,
                )
            )
            if same_company:
                result.append(
                    BusyInterval(
                        start=start,
                        end=end,
                        kind=BusyKind.CAPACITY,
                        source="company_capacity",
                        reference_id=interview_id,
                        buffer_minutes=stored_buffer,
                    )
                )
            result.append(
                BusyInterval(
                    start=start,
                    end=end,
                    kind=BusyKind.CANDIDATE,
                    owner_id=UUID(str(row["candidate_id"])),
                    reference_id=safe_reference,
                    buffer_minutes=stored_buffer,
                )
            )
        # Same company + same candidate + same job + same local date is a hard
        # duplicate-prevention rule, even when the interview times do not overlap.
        # Failed/cancelled interviews are intentionally excluded so HR can reschedule.
        same_job_day_rows = await conn.fetch(
            """
            SELECT i.id,i.candidate_id,i.scheduled_at
            FROM interviews i
            WHERE i.company_id=$1
              AND i.job_id=$2
              AND i.candidate_id=ANY($3::uuid[])
              AND i.status IN ('scheduled','in_progress','completed')
              AND i.scheduled_at >= $4
              AND i.scheduled_at < $5
              AND ($6::uuid IS NULL OR i.id<>$6)
            """,
            request.company_id,
            request.job_id,
            request.candidate_ids,
            timezone_start,
            timezone_end,
            exclude_interview_id,
        )
        for row in same_job_day_rows:
            scheduled_local_date = row["scheduled_at"].astimezone(zone).date()
            day_start = datetime.combine(
                scheduled_local_date, time.min, tzinfo=zone
            ).astimezone(UTC)
            day_end = datetime.combine(
                scheduled_local_date + timedelta(days=1), time.min, tzinfo=zone
            ).astimezone(UTC)
            result.append(
                BusyInterval(
                    start=day_start,
                    end=day_end,
                    kind=BusyKind.CANDIDATE_JOB_DAY,
                    owner_id=UUID(str(row["candidate_id"])),
                    source="candidate_job_day",
                    reference_id=UUID(str(row["id"])),
                    buffer_minutes=0,
                )
            )

        if interview_ids and request.interviewer_ids:
            assignments = await conn.fetch(
                """
                SELECT ii.interview_id,ii.interviewer_id
                FROM interview_interviewers ii
                WHERE ii.interview_id=ANY($1::uuid[]) AND ii.interviewer_id=ANY($2::uuid[])
                """,
                interview_ids,
                request.interviewer_ids,
            )
            for row in assignments:
                interview_id = UUID(str(row["interview_id"]))
                start, end, stored_buffer, same_company = windows[interview_id]
                result.append(
                    BusyInterval(
                        start=start,
                        end=end,
                        kind=BusyKind.INTERVIEWER,
                        owner_id=UUID(str(row["interviewer_id"])),
                        reference_id=interview_id if same_company else None,
                        buffer_minutes=stored_buffer,
                    )
                )
        return result

    async def create_proposal(
        self,
        *,
        actor: Actor,
        request: SchedulingRequest,
        draft: ProposalDraft,
        idempotency_key: UUID,
        correlation_id: UUID,
        request_fingerprint: str,
    ) -> tuple[UUID, bool]:
        proposal_id = uuid5(
            NAMESPACE_URL,
            f"talentflow:proposal:{request.company_id}:{idempotency_key}",
        )
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=self.settings.proposal_expiry_minutes)
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._validate_scope(conn, actor, request)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"proposal:{request.company_id}:{idempotency_key}",
            )
            existing = await conn.fetchrow(
                """
                SELECT id,request_fingerprint FROM scheduling_proposals
                WHERE company_id=$1 AND idempotency_key=$2
                """,
                request.company_id,
                idempotency_key,
            )
            if existing is not None:
                if str(existing["request_fingerprint"]) != request_fingerprint:
                    raise ServiceError("idempotency_conflict", status=409, retryable=False)
                return UUID(str(existing["id"])), False

            await conn.execute(
                """
                INSERT INTO scheduling_proposals
                  (id,company_id,job_id,created_by,mode,status,timezone,structured_constraints,
                   request_fingerprint,idempotency_key,correlation_id,expires_at)
                VALUES ($1,$2,$3,$4,$5,'draft',$6,$7,$8,$9,$10,$11)
                """,
                proposal_id,
                request.company_id,
                request.job_id,
                actor.user_id,
                (
                    SchedulingMode.AI_ASSISTED.value
                    if request.mode == SchedulingMode.AUTO_BALANCED
                    else request.mode.value
                ),
                request.timezone,
                request.model_dump(mode="json"),
                request_fingerprint,
                idempotency_key,
                correlation_id,
                expires_at,
            )
            for item in draft.items:
                item_id = uuid5(NAMESPACE_URL, f"talentflow:proposal-item:{proposal_id}:{item.candidate_id}")
                await conn.execute(
                    """
                    INSERT INTO scheduling_proposal_items
                      (id,proposal_id,company_id,job_id,candidate_id,proposed_start_at,
                       proposed_end_at,item_status,warnings,conflicts,alternatives)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    """,
                    item_id,
                    proposal_id,
                    request.company_id,
                    request.job_id,
                    item.candidate_id,
                    item.proposed_start,
                    item.proposed_end,
                    "proposed" if item.schedulable else "conflict",
                    item.warnings,
                    [entry.model_dump(mode="json") for entry in item.conflicts],
                    [value.isoformat() for value in item.alternatives],
                )
            await self._event(
                conn,
                company_id=request.company_id,
                proposal_id=proposal_id,
                interview_id=None,
                actor_id=actor.user_id,
                correlation_id=correlation_id,
                event_type="scheduling_proposal_created",
                data={"candidate_count": len(request.candidate_ids), "mode": request.mode.value},
            )
        return proposal_id, True

    async def proposal(self, proposal_id: UUID, actor: Actor) -> dict[str, Any]:
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM scheduling_proposals WHERE id=$1 AND company_id=$2",
                proposal_id,
                actor.company_id,
            )
            if row is None:
                raise ServiceError("proposal_not_found", status=404, retryable=False)
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            items = await conn.fetch(
                """
                SELECT spi.*,p.full_name AS candidate_name,jp.title AS position_title,
                  CASE WHEN spi.interview_id IS NULL THEN 'not_started'
                    ELSE COALESCE(calendar.status,'not_connected') END AS calendar_status,
                  calendar.error_code AS calendar_error_code,
                  CASE WHEN spi.interview_id IS NULL THEN 'not_started'
                    ELSE COALESCE(email.status,'not_connected') END AS email_status,
                  email.error_code AS email_error_code
                FROM scheduling_proposal_items spi
                JOIN profiles p ON p.id=spi.candidate_id
                JOIN job_postings jp ON jp.id=spi.job_id
                LEFT JOIN LATERAL (
                  SELECT ia.status,ia.error_code FROM integration_actions ia
                  WHERE ia.interview_id=spi.interview_id AND ia.company_id=spi.company_id
                    AND ia.provider='google_calendar'
                  ORDER BY ia.schedule_version DESC,ia.created_at DESC LIMIT 1
                ) calendar ON true
                LEFT JOIN LATERAL (
                  SELECT ia.status,ia.error_code FROM integration_actions ia
                  WHERE ia.interview_id=spi.interview_id AND ia.company_id=spi.company_id
                    AND ia.provider='gmail'
                  ORDER BY ia.schedule_version DESC,ia.created_at DESC LIMIT 1
                ) email ON true
                WHERE spi.proposal_id=$1 ORDER BY spi.proposed_start_at NULLS LAST,spi.id
                """,
                proposal_id,
            )
            return {"proposal": dict(row), "items": [dict(item) for item in items]}

    async def confirm(
        self,
        proposal_id: UUID,
        actor: Actor,
        *,
        idempotency_key: UUID,
        external_busy: list[BusyInterval] | None = None,
    ) -> list[dict[str, Any]]:
        if self.invitation_cipher is None:
            raise ServiceError("data_encryption_key_missing", status=503, retryable=False)
        async with self.db.pool.acquire() as conn, conn.transaction():
            proposal = await conn.fetchrow(
                "SELECT * FROM scheduling_proposals WHERE id=$1 FOR UPDATE",
                proposal_id,
            )
            if proposal is None or UUID(str(proposal["company_id"])) != actor.company_id:
                raise ServiceError("proposal_not_found", status=404, retryable=False)
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            confirmation_fingerprint = hashlib.sha256(str(proposal_id).encode()).hexdigest()
            replay = await conn.fetchrow(
                """
                SELECT proposal_id,request_fingerprint FROM scheduling_events
                WHERE company_id=$1 AND event_type='schedule_confirmed' AND idempotency_key=$2
                """,
                actor.company_id,
                idempotency_key,
            )
            if replay is not None:
                if (
                    UUID(str(replay["proposal_id"])) != proposal_id
                    or str(replay["request_fingerprint"]) != confirmation_fingerprint
                ):
                    raise ServiceError("idempotency_conflict", status=409, retryable=False)
                return await self._confirmed_items(conn, proposal_id)
            if str(proposal["status"]) == "confirmed":
                return await self._confirmed_items(conn, proposal_id)
            if str(proposal["status"]) != "draft":
                raise ServiceError("proposal_not_confirmable", status=409, retryable=False)
            if proposal["expires_at"].astimezone(UTC) <= datetime.now(UTC):
                await conn.execute(
                    "UPDATE scheduling_proposals SET status='expired',version=version+1 WHERE id=$1",
                    proposal_id,
                )
                raise ServiceError("proposal_expired", status=409, retryable=False)

            request = SchedulingRequest.model_validate(proposal["structured_constraints"])
            await self._validate_scope(conn, actor, request)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                "talentflow:orchestrator:global-capacity",
            )
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                str(request.company_id),
            )
            for candidate_id in sorted(request.candidate_ids, key=str):
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    f"candidate:{candidate_id}",
                )
            policy = await self._policy(conn, request.company_id)
            busy = await self._busy_intervals(conn, request)
            busy.extend(external_busy or [])
            try:
                recalculated = self.engine.propose(
                    request,
                    policy,
                    busy,
                    max_horizon_days=self.settings.max_scheduling_horizon_days,
                )
            except ValueError as exc:
                if str(exc) == "scheduling_horizon_exceeded":
                    raise ServiceError(
                        str(exc), status=422, retryable=False
                    ) from exc
                raise
            stored_items = await conn.fetch(
                "SELECT * FROM scheduling_proposal_items WHERE proposal_id=$1 ORDER BY id FOR UPDATE",
                proposal_id,
            )
            expected = {item.candidate_id: item for item in recalculated.items}
            schedulable_items = 0
            for stored in stored_items:
                candidate_id = UUID(str(stored["candidate_id"]))
                current = expected[candidate_id]
                if stored["proposed_start_at"] is None:
                    continue
                if (
                    not current.schedulable
                    or stored["proposed_start_at"] != current.proposed_start
                    or stored["proposed_end_at"] != current.proposed_end
                ):
                    raise ServiceError("schedule_conflict", status=409, retryable=False)
                schedulable_items += 1
            if schedulable_items == 0:
                raise ServiceError("no_schedulable_candidates", status=409, retryable=False)

            job = await conn.fetchrow(
                """
                SELECT jp.title,jp.description,pt.system_prompt
                FROM job_postings jp
                LEFT JOIN prompt_templates pt
                  ON pt.id=$3
                  AND (
                    pt.company_id IS NULL
                    OR (pt.company_id=jp.company_id AND (pt.job_id IS NULL OR pt.job_id=jp.id))
                  )
                WHERE jp.company_id=$1 AND jp.id=$2
                """,
                request.company_id,
                request.job_id,
                request.prompt_template_id,
            )
            if job is None:
                raise ServiceError("position_not_found", status=404, retryable=False)
            results: list[dict[str, Any]] = []
            provider_account_id = await self._provider_account_id(
                conn, request.company_id
            )
            for stored in stored_items:
                if stored["proposed_start_at"] is None:
                    continue
                item_id = UUID(str(stored["id"]))
                candidate_id = UUID(str(stored["candidate_id"]))
                interview_id = uuid5(NAMESPACE_URL, f"talentflow:scheduled-interview:{item_id}")
                start = stored["proposed_start_at"].astimezone(UTC)
                end = stored["proposed_end_at"].astimezone(UTC)
                await conn.execute(
                    """
                    INSERT INTO interviews
                      (id,company_id,job_id,candidate_id,prompt_template_id,status,scheduled_at,
                       scheduled_end_at,scheduling_timezone,scheduling_buffer_minutes,schedule_version)
                    VALUES ($1,$2,$3,$4,$5,'scheduled',$6,$7,$8,$9,1)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    interview_id,
                    request.company_id,
                    request.job_id,
                    candidate_id,
                    request.prompt_template_id,
                    start,
                    end,
                    request.timezone,
                    request.buffer_minutes,
                )
                await conn.execute(
                    """
                    INSERT INTO worker_interview_configs
                      (interview_id,schema_version,config,updated_by)
                    VALUES ($1,1,$2,$3)
                    ON CONFLICT (interview_id) DO NOTHING
                    """,
                    interview_id,
                    self._interview_config(
                        request,
                        str(job["title"]),
                        str(job["description"]),
                        str(job["system_prompt"] or ""),
                    ),
                    actor.user_id,
                )
                for interviewer_id in request.interviewer_ids:
                    await conn.execute(
                        """
                        INSERT INTO interview_interviewers
                          (company_id,interview_id,interviewer_id,assigned_by)
                        VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING
                        """,
                        request.company_id,
                        interview_id,
                        interviewer_id,
                        actor.user_id,
                    )
                invitation_url = await self._create_invitation(
                    conn,
                    company_id=request.company_id,
                    interview_id=interview_id,
                    candidate_id=candidate_id,
                    created_by=actor.user_id,
                    expires_at=end + timedelta(days=1),
                )
                await conn.execute(
                    """
                    UPDATE scheduling_proposal_items
                    SET item_status='confirmed',interview_id=$2 WHERE id=$1
                    """,
                    item_id,
                    interview_id,
                )
                if provider_account_id is not None:
                    await self._create_provider_actions(
                        conn,
                        company_id=request.company_id,
                        proposal_id=proposal_id,
                        interview_id=interview_id,
                        provider_account_id=provider_account_id,
                        schedule_version=1,
                        correlation_id=UUID(str(proposal["correlation_id"])),
                        kinds=(JobKind.CREATE_CALENDAR_EVENT, JobKind.SEND_INVITATION),
                    )
                await self._event(
                    conn,
                    company_id=request.company_id,
                    proposal_id=proposal_id,
                    interview_id=interview_id,
                    actor_id=actor.user_id,
                    correlation_id=UUID(str(proposal["correlation_id"])),
                    event_type="interview_scheduled",
                    data={"candidate_id": str(candidate_id), "schedule_version": 1},
                )
                results.append(
                    {
                        "interview_id": interview_id,
                        "candidate_id": candidate_id,
                        "scheduled_at": start,
                        "scheduled_end_at": end,
                        "timezone": request.timezone,
                        "invitation_url": invitation_url,
                        "calendar_status": (
                            "pending"
                            if provider_account_id and self.settings.google_calendar_enabled
                            else "not_connected"
                        ),
                        "email_status": (
                            "pending"
                            if provider_account_id and self.settings.gmail_enabled
                            else "not_connected"
                        ),
                    }
                )
            await conn.execute(
                """
                UPDATE scheduling_proposals
                SET status='confirmed',confirmed_at=clock_timestamp(),confirmed_by=$2,
                    version=version+1,updated_at=clock_timestamp()
                WHERE id=$1
                """,
                proposal_id,
                actor.user_id,
            )
            await self._event(
                conn,
                company_id=request.company_id,
                proposal_id=proposal_id,
                interview_id=None,
                actor_id=actor.user_id,
                correlation_id=UUID(str(proposal["correlation_id"])),
                event_type="schedule_confirmed",
                idempotency_key=idempotency_key,
                fingerprint=confirmation_fingerprint,
                data={"scheduled_candidates": len(results)},
            )
            return results

    async def _confirmed_items(
        self,
        conn: DbConnection,
        proposal_id: UUID,
    ) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT spi.interview_id,spi.candidate_id,i.scheduled_at,i.scheduled_end_at,
                   i.scheduling_timezone,
                   COALESCE(calendar.status,'not_connected') AS calendar_status,
                   calendar.error_code AS calendar_error_code,
                   COALESCE(email.status,'not_connected') AS email_status,
                   email.error_code AS email_error_code
            FROM scheduling_proposal_items spi
            JOIN interviews i ON i.id=spi.interview_id
            LEFT JOIN LATERAL (
              SELECT ia.status,ia.error_code FROM integration_actions ia
              WHERE ia.interview_id=spi.interview_id AND ia.company_id=spi.company_id
                AND ia.provider='google_calendar'
              ORDER BY ia.schedule_version DESC,ia.created_at DESC LIMIT 1
            ) calendar ON true
            LEFT JOIN LATERAL (
              SELECT ia.status,ia.error_code FROM integration_actions ia
              WHERE ia.interview_id=spi.interview_id AND ia.company_id=spi.company_id
                AND ia.provider='gmail'
              ORDER BY ia.schedule_version DESC,ia.created_at DESC LIMIT 1
            ) email ON true
            WHERE spi.proposal_id=$1 AND spi.item_status='confirmed' ORDER BY spi.id
            """,
            proposal_id,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                invitation_url: str | None = await self._invitation_url(
                    conn, UUID(str(row["interview_id"]))
                )
            except ServiceError as exc:
                if exc.code != "invitation_not_found":
                    raise
                invitation_url = None
            result.append(
                {
                    "interview_id": UUID(str(row["interview_id"])),
                    "candidate_id": UUID(str(row["candidate_id"])),
                    "scheduled_at": row["scheduled_at"],
                    "scheduled_end_at": row["scheduled_end_at"],
                    "timezone": str(row["scheduling_timezone"]),
                    "invitation_url": invitation_url,
                    "calendar_status": str(row["calendar_status"]),
                    "calendar_error_code": row["calendar_error_code"],
                    "email_status": str(row["email_status"]),
                    "email_error_code": row["email_error_code"],
                }
            )
        return result

    async def integration_status(
        self,
        actor: Actor,
        interview_id: UUID,
    ) -> dict[str, Any]:
        async with self.db.pool.acquire() as conn:
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM interviews WHERE company_id=$1 AND id=$2)",
                actor.company_id,
                interview_id,
            )
            if not exists:
                raise ServiceError("interview_not_found", status=404, retryable=False)
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (provider)
                  provider,status,error_code,attempt,updated_at
                FROM integration_actions
                WHERE company_id=$1 AND interview_id=$2
                ORDER BY provider,schedule_version DESC,created_at DESC
                """,
                actor.company_id,
                interview_id,
            )
        statuses: dict[str, dict[str, Any]] = {
            "calendar": {
                "status": (
                    "not_connected" if self.settings.google_calendar_enabled else "disabled"
                ),
                "error_code": None,
                "attempt": 0,
            },
            "email": {
                "status": "not_connected" if self.settings.gmail_enabled else "disabled",
                "error_code": None,
                "attempt": 0,
            },
        }
        for row in rows:
            key = "calendar" if str(row["provider"]) == "google_calendar" else "email"
            statuses[key] = {
                "status": str(row["status"]),
                "error_code": row["error_code"],
                "attempt": int(row["attempt"]),
                "updated_at": row["updated_at"],
            }
        return {"interview_id": interview_id, **statuses}

    def _interview_config(
        self,
        request: SchedulingRequest,
        title: str,
        description: str,
        system_prompt: str,
    ) -> dict[str, Any]:
        instructions = system_prompt.strip()
        if request.notes.strip():
            instructions = "\n\n".join(
                part for part in (instructions, request.notes.strip()) if part
            )
        cleaned_title = title.strip()
        if not cleaned_title or len(cleaned_title) > 160:
            raise ServiceError(
                "interview_configuration_invalid", status=422, retryable=False
            )
        if len(description) > 20000 or len(instructions) > 8000:
            raise ServiceError(
                "interview_configuration_invalid", status=422, retryable=False
            )
        return {
            "schema_version": 1,
            "role": cleaned_title,
            "language": request.language,
            "difficulty": "intermediate",
            "seniority": "unspecified",
            "duration_minutes": request.duration_minutes,
            "join_policy": "scheduled",
            "competencies": [
                {
                    "name": "Core Technical Competency",
                    "criterion": "Demonstrate domain knowledge, problem decomposition, and tradeoffs",
                    "weight": 1,
                },
                {
                    "name": "Communication & Collaboration",
                    "criterion": "Articulate the thought process clearly and respond to follow-up questions",
                    "weight": 1,
                },
            ],
            "interviewer_instructions": instructions,
            "job_description": description,
            "include_cv": False,
            "video_enabled": False,
            "practice_feedback": False,
            "max_follow_ups": 3,
        }

    async def _create_invitation(
        self,
        conn: DbConnection,
        *,
        company_id: UUID,
        interview_id: UUID,
        candidate_id: UUID,
        created_by: UUID,
        expires_at: datetime,
    ) -> str:
        if self.invitation_cipher is None:
            raise ServiceError("data_encryption_key_missing", retryable=False)
        existing = await conn.fetchrow(
            """
            SELECT token_ciphertext,token_nonce,encryption_key_version,revoked_at
            FROM interview_private.interview_invitations WHERE interview_id=$1
            """,
            interview_id,
        )
        context = interview_id.bytes
        if existing is not None:
            if existing["revoked_at"] is not None:
                raise ServiceError("invitation_revoked", status=409, retryable=False)
            token = self.invitation_cipher.decrypt(
                EncryptedValue(
                    bytes(existing["token_ciphertext"]),
                    bytes(existing["token_nonce"]),
                    str(existing["encryption_key_version"]),
                ),
                context=context,
            )
            await conn.execute(
                """
                UPDATE interview_private.interview_invitations
                SET expires_at=$1 WHERE interview_id=$2 AND revoked_at IS NULL
                """,
                expires_at,
                interview_id,
            )
        else:
            token = new_opaque_token()
            encrypted = self.invitation_cipher.encrypt(token, context=context)
            await conn.execute(
                """
                INSERT INTO interview_private.interview_invitations
                  (id,company_id,interview_id,candidate_id,token_digest,token_ciphertext,
                   token_nonce,encryption_key_version,expires_at,created_by)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                uuid4(),
                company_id,
                interview_id,
                candidate_id,
                token_digest(token),
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.key_version,
                expires_at,
                created_by,
            )
        return self.settings.public_interview_base_url.rstrip("/") + "/" + token

    async def _invitation_url(
        self,
        conn: DbConnection,
        interview_id: UUID,
    ) -> str:
        if self.invitation_cipher is None:
            raise ServiceError("data_encryption_key_missing", retryable=False)
        row = await conn.fetchrow(
            """
            SELECT token_ciphertext,token_nonce,encryption_key_version
            FROM interview_private.interview_invitations
            WHERE interview_id=$1 AND revoked_at IS NULL AND expires_at>clock_timestamp()
            """,
            interview_id,
        )
        if row is None:
            raise ServiceError("invitation_not_found", status=404, retryable=False)
        token = self.invitation_cipher.decrypt(
            EncryptedValue(
                bytes(row["token_ciphertext"]),
                bytes(row["token_nonce"]),
                str(row["encryption_key_version"]),
            ),
            context=interview_id.bytes,
        )
        return self.settings.public_interview_base_url.rstrip("/") + "/" + token

    async def _create_provider_actions(
        self,
        conn: DbConnection,
        *,
        company_id: UUID,
        proposal_id: UUID | None,
        interview_id: UUID,
        provider_account_id: UUID,
        schedule_version: int,
        correlation_id: UUID,
        kinds: tuple[JobKind, ...],
    ) -> None:
        mapping = {
            JobKind.CREATE_CALENDAR_EVENT: ("google_calendar", "create_event"),
            JobKind.UPDATE_CALENDAR_EVENT: ("google_calendar", "update_event"),
            JobKind.CANCEL_CALENDAR_EVENT: ("google_calendar", "cancel_event"),
            JobKind.SEND_INVITATION: ("gmail", "send_invitation"),
            JobKind.SEND_RESCHEDULE: ("gmail", "send_reschedule"),
            JobKind.SEND_CANCELLATION: ("gmail", "send_cancellation"),
        }
        for kind in kinds:
            if kind in {
                JobKind.CREATE_CALENDAR_EVENT,
                JobKind.UPDATE_CALENDAR_EVENT,
                JobKind.CANCEL_CALENDAR_EVENT,
            } and not self.settings.google_calendar_enabled:
                continue
            if kind in {
                JobKind.SEND_INVITATION,
                JobKind.SEND_RESCHEDULE,
                JobKind.SEND_CANCELLATION,
            } and not self.settings.gmail_enabled:
                continue
            provider, action = mapping[kind]
            logical_key = f"{kind.value}:{interview_id}:v{schedule_version}"
            action_id = uuid5(NAMESPACE_URL, "talentflow:integration:" + logical_key)
            job_id = uuid5(NAMESPACE_URL, "talentflow:orchestrator-job:" + logical_key)
            await conn.execute(
                """
                INSERT INTO integration_actions
                  (id,company_id,interview_id,provider_account_id,provider,action,
                   schedule_version,idempotency_key,status,correlation_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending',$9)
                ON CONFLICT (company_id,idempotency_key) DO NOTHING
                """,
                action_id,
                company_id,
                interview_id,
                provider_account_id,
                provider,
                action,
                schedule_version,
                logical_key,
                correlation_id,
            )
            await conn.execute(
                """
                INSERT INTO worker_jobs
                  (id,kind,session_id,correlation_id,idempotency_key,attempt,max_attempts,
                   status,payload,delay_until,created_at,executor_service,company_id,
                   interview_id,proposal_id,integration_action_id,expected_schedule_version)
                VALUES ($1,$2,NULL,$3,$4,0,$5,'orchestrator_pending','{}',clock_timestamp(),
                        clock_timestamp(),'scheduling_orchestrator',$6,$7,$8,$9,$10)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                job_id,
                kind.value,
                correlation_id,
                "orchestrator:" + logical_key,
                self.settings.job_max_attempts,
                company_id,
                interview_id,
                proposal_id,
                action_id,
                schedule_version,
            )

    async def _event(
        self,
        conn: DbConnection,
        *,
        company_id: UUID,
        proposal_id: UUID | None,
        interview_id: UUID | None,
        actor_id: UUID | None,
        correlation_id: UUID,
        event_type: str,
        data: dict[str, Any],
        idempotency_key: UUID | None = None,
        fingerprint: str | None = None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO scheduling_events
              (id,company_id,proposal_id,interview_id,actor_id,correlation_id,event_type,
               idempotency_key,request_fingerprint,data)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            uuid4(),
            company_id,
            proposal_id,
            interview_id,
            actor_id,
            correlation_id,
            event_type,
            idempotency_key,
            fingerprint,
            data,
        )

    async def reschedule(
        self,
        *,
        actor: Actor,
        interview_id: UUID,
        start: datetime,
        duration_minutes: int,
        timezone: str,
        buffer_minutes: int,
        interviewer_ids: list[UUID],
        idempotency_key: UUID,
        correlation_id: UUID,
        external_busy: list[BusyInterval] | None = None,
    ) -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        if start.tzinfo is None:
            raise ServiceError("timezone_required", status=422, retryable=False)
        local_start = start.astimezone(ZoneInfo(timezone))
        local_end = local_start + timedelta(minutes=duration_minutes)
        payload = {
            "interview_id": str(interview_id),
            "start": start.isoformat(),
            "duration_minutes": duration_minutes,
            "timezone": timezone,
            "buffer_minutes": buffer_minutes,
            "interviewer_ids": sorted(str(item) for item in interviewer_ids),
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"reschedule:{actor.company_id}:{idempotency_key}",
            )
            replay = await conn.fetchrow(
                """
                SELECT request_fingerprint FROM scheduling_events
                WHERE company_id=$1 AND event_type='interview_rescheduled' AND idempotency_key=$2
                """,
                actor.company_id,
                idempotency_key,
            )
            if replay is not None:
                if str(replay["request_fingerprint"]) != fingerprint:
                    raise ServiceError("idempotency_conflict", status=409, retryable=False)
                current = await conn.fetchrow(
                    """
                    SELECT id,scheduled_at,scheduled_end_at,scheduling_timezone,schedule_version,status::text
                    FROM interviews WHERE company_id=$1 AND id=$2
                    """,
                    actor.company_id,
                    interview_id,
                )
                if current is None:
                    raise ServiceError("interview_not_found", status=404, retryable=False)
                result = dict(current)
                result["invitation_url"] = await self._invitation_url(
                    conn, interview_id
                )
                return result

            interview = await conn.fetchrow(
                """
                SELECT id,company_id,job_id,candidate_id,prompt_template_id,status::text,
                       schedule_version FROM interviews
                WHERE company_id=$1 AND id=$2 FOR UPDATE
                """,
                actor.company_id,
                interview_id,
            )
            if interview is None:
                raise ServiceError("interview_not_found", status=404, retryable=False)
            if str(interview["status"]) != "scheduled":
                raise ServiceError("interview_not_reschedulable", status=409, retryable=False)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                "talentflow:orchestrator:global-capacity",
            )
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(actor.company_id)
            )
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"candidate:{interview['candidate_id']}",
            )
            request = SchedulingRequest(
                company_id=actor.company_id,
                job_id=UUID(str(interview["job_id"])),
                prompt_template_id=(
                    UUID(str(interview["prompt_template_id"]))
                    if interview["prompt_template_id"]
                    else None
                ),
                candidate_ids=[UUID(str(interview["candidate_id"]))],
                interviewer_ids=interviewer_ids,
                timezone=timezone,
                start_date=local_start.date(),
                end_date=local_start.date(),
                window_start=local_start.time().replace(tzinfo=None),
                window_end=local_end.time().replace(tzinfo=None),
                duration_minutes=duration_minutes,
                buffer_minutes=buffer_minutes,
                mode=SchedulingMode.EXPLICIT,
            )
            await self._validate_scope(conn, actor, request)
            policy = await self._policy(conn, actor.company_id)
            draft = self.engine.propose(
                request,
                policy,
                await self._busy_intervals(
                    conn, request, exclude_interview_id=interview_id
                ) + (external_busy or []),
                max_horizon_days=self.settings.max_scheduling_horizon_days,
            )
            item = draft.items[0]
            if not item.schedulable:
                raise ServiceError("schedule_conflict", status=409, retryable=False)
            if item.proposed_start is None or item.proposed_end is None:
                raise ServiceError("schedule_engine_invalid_result", retryable=False)
            version = int(interview["schedule_version"]) + 1
            updated = await conn.execute(
                """
                UPDATE interviews SET scheduled_at=$1,scheduled_end_at=$2,scheduling_timezone=$3,
                  scheduling_buffer_minutes=$4,schedule_version=$5,updated_at=clock_timestamp()
                WHERE id=$6 AND company_id=$7 AND status='scheduled'
                """,
                item.proposed_start,
                item.proposed_end,
                timezone,
                buffer_minutes,
                version,
                interview_id,
                actor.company_id,
            )
            if updated != "UPDATE 1":
                raise ServiceError("interview_not_reschedulable", status=409, retryable=False)
            await conn.execute(
                "DELETE FROM interview_interviewers WHERE interview_id=$1", interview_id
            )
            config_updated = await conn.execute(
                """
                UPDATE worker_interview_configs
                SET config=jsonb_set(config,'{duration_minutes}',to_jsonb($1::integer),true),
                    updated_by=$2,updated_at=clock_timestamp()
                WHERE interview_id=$3
                """,
                duration_minutes,
                actor.user_id,
                interview_id,
            )
            if config_updated != "UPDATE 1":
                raise ServiceError(
                    "interview_configuration_not_found", status=422, retryable=False
                )
            for interviewer_id in interviewer_ids:
                await conn.execute(
                    """
                    INSERT INTO interview_interviewers(company_id,interview_id,interviewer_id,assigned_by)
                    VALUES ($1,$2,$3,$4)
                    """,
                    actor.company_id,
                    interview_id,
                    interviewer_id,
                    actor.user_id,
                )
            invitation_url = await self._create_invitation(
                conn,
                company_id=actor.company_id,
                interview_id=interview_id,
                candidate_id=UUID(str(interview["candidate_id"])),
                created_by=actor.user_id,
                expires_at=item.proposed_end + timedelta(days=1),
            )
            provider_account_id = await self._provider_account_id(
                conn, actor.company_id, interview_id
            )
            if provider_account_id:
                await self._create_provider_actions(
                    conn,
                    company_id=actor.company_id,
                    proposal_id=None,
                    interview_id=interview_id,
                    provider_account_id=provider_account_id,
                    schedule_version=version,
                    correlation_id=correlation_id,
                    kinds=(JobKind.UPDATE_CALENDAR_EVENT, JobKind.SEND_RESCHEDULE),
                )
            await self._event(
                conn,
                company_id=actor.company_id,
                proposal_id=None,
                interview_id=interview_id,
                actor_id=actor.user_id,
                correlation_id=correlation_id,
                event_type="interview_rescheduled",
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                data={"schedule_version": version},
            )
            return {
                "id": interview_id,
                "scheduled_at": item.proposed_start,
                "scheduled_end_at": item.proposed_end,
                "scheduling_timezone": timezone,
                "schedule_version": version,
                "status": "scheduled",
                "invitation_url": invitation_url,
            }

    async def reschedule_request(
        self,
        *,
        actor: Actor,
        interview_id: UUID,
        start: datetime,
        duration_minutes: int,
        timezone: str,
        buffer_minutes: int,
        interviewer_ids: list[UUID],
    ) -> tuple[SchedulingRequest, tuple[datetime, datetime] | None]:
        from zoneinfo import ZoneInfo

        local_start = start.astimezone(ZoneInfo(timezone))
        local_end = local_start + timedelta(minutes=duration_minutes)
        return await self.reschedule_search_request(
            actor=actor,
            interview_id=interview_id,
            timezone=timezone,
            start_date=local_start.date(),
            end_date=local_start.date(),
            window_start=local_start.time().replace(tzinfo=None),
            window_end=local_end.time().replace(tzinfo=None),
            duration_minutes=duration_minutes,
            buffer_minutes=buffer_minutes,
            interviewer_ids=interviewer_ids,
            mode=SchedulingMode.EXPLICIT,
        )

    async def reschedule_search_request(
        self,
        *,
        actor: Actor,
        interview_id: UUID,
        timezone: str,
        start_date: date,
        end_date: date,
        window_start: time | None,
        window_end: time | None,
        duration_minutes: int,
        buffer_minutes: int,
        interviewer_ids: list[UUID],
        mode: SchedulingMode,
    ) -> tuple[SchedulingRequest, tuple[datetime, datetime] | None]:
        async with self.db.pool.acquire() as conn:
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            interview = await conn.fetchrow(
                """
                SELECT job_id,candidate_id,prompt_template_id,status::text,
                       scheduled_at,scheduled_end_at
                FROM interviews WHERE company_id=$1 AND id=$2
                """,
                actor.company_id,
                interview_id,
            )
        if interview is None:
            raise ServiceError("interview_not_found", status=404, retryable=False)
        if str(interview["status"]) != "scheduled":
            raise ServiceError("interview_not_reschedulable", status=409, retryable=False)
        request = SchedulingRequest(
            company_id=actor.company_id,
            job_id=UUID(str(interview["job_id"])),
            prompt_template_id=(
                UUID(str(interview["prompt_template_id"]))
                if interview["prompt_template_id"]
                else None
            ),
            candidate_ids=[UUID(str(interview["candidate_id"]))],
            interviewer_ids=interviewer_ids,
            timezone=timezone,
            start_date=start_date,
            end_date=end_date,
            window_start=window_start,
            window_end=window_end,
            duration_minutes=duration_minutes,
            buffer_minutes=buffer_minutes,
            mode=mode,
        )
        current_window: tuple[datetime, datetime] | None = None
        if interview["scheduled_at"] is not None and interview["scheduled_end_at"] is not None:
            current_window = (
                interview["scheduled_at"].astimezone(UTC),
                interview["scheduled_end_at"].astimezone(UTC),
            )
        return request, current_window

    async def reschedule_busy_intervals(
        self,
        *,
        actor: Actor,
        interview_id: UUID,
        request: SchedulingRequest,
    ) -> list[BusyInterval]:
        """Load conflicts while excluding only the interview being moved."""
        async with self.db.pool.acquire() as conn:
            await self._validate_scope(conn, actor, request)
            return await self._busy_intervals(
                conn,
                request,
                exclude_interview_id=interview_id,
            )

    async def cancel(
        self,
        *,
        actor: Actor,
        interview_id: UUID,
        reason: str,
        idempotency_key: UUID,
        correlation_id: UUID,
    ) -> dict[str, Any]:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"interview_id": str(interview_id), "reason": reason},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                f"cancel:{actor.company_id}:{idempotency_key}",
            )
            replay = await conn.fetchrow(
                """
                SELECT request_fingerprint FROM scheduling_events
                WHERE company_id=$1 AND event_type='interview_cancelled' AND idempotency_key=$2
                """,
                actor.company_id,
                idempotency_key,
            )
            if replay is not None:
                if str(replay["request_fingerprint"]) != fingerprint:
                    raise ServiceError("idempotency_conflict", status=409, retryable=False)
                current = await conn.fetchrow(
                    "SELECT id,status::text,schedule_version,cancelled_at FROM interviews WHERE company_id=$1 AND id=$2",
                    actor.company_id,
                    interview_id,
                )
                if current is None:
                    raise ServiceError("interview_not_found", status=404, retryable=False)
                return dict(current)
            interview = await conn.fetchrow(
                """
                SELECT status::text,schedule_version FROM interviews
                WHERE company_id=$1 AND id=$2 FOR UPDATE
                """,
                actor.company_id,
                interview_id,
            )
            if interview is None:
                raise ServiceError("interview_not_found", status=404, retryable=False)
            if str(interview["status"]) == "cancelled":
                return {
                    "id": interview_id,
                    "status": "cancelled",
                    "schedule_version": int(interview["schedule_version"]),
                }
            if str(interview["status"]) != "scheduled":
                raise ServiceError("interview_not_cancellable", status=409, retryable=False)
            version = int(interview["schedule_version"]) + 1
            updated = await conn.execute(
                """
                UPDATE interviews SET status='cancelled',cancelled_at=clock_timestamp(),
                  cancelled_by=$1,cancellation_reason=$2,schedule_version=$3,
                  updated_at=clock_timestamp()
                WHERE company_id=$4 AND id=$5 AND status='scheduled'
                """,
                actor.user_id,
                reason,
                version,
                actor.company_id,
                interview_id,
            )
            if updated != "UPDATE 1":
                raise ServiceError("interview_not_cancellable", status=409, retryable=False)
            await conn.execute(
                """
                UPDATE interview_private.interview_invitations
                SET revoked_at=clock_timestamp() WHERE interview_id=$1 AND revoked_at IS NULL
                """,
                interview_id,
            )
            provider_account_id = await self._provider_account_id(
                conn, actor.company_id, interview_id
            )
            if provider_account_id:
                await self._create_provider_actions(
                    conn,
                    company_id=actor.company_id,
                    proposal_id=None,
                    interview_id=interview_id,
                    provider_account_id=provider_account_id,
                    schedule_version=version,
                    correlation_id=correlation_id,
                    kinds=(JobKind.CANCEL_CALENDAR_EVENT, JobKind.SEND_CANCELLATION),
                )
            await self._event(
                conn,
                company_id=actor.company_id,
                proposal_id=None,
                interview_id=interview_id,
                actor_id=actor.user_id,
                correlation_id=correlation_id,
                event_type="interview_cancelled",
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                data={"schedule_version": version},
            )
            return {
                "id": interview_id,
                "status": "cancelled",
                "schedule_version": version,
            }

    async def resolve_invitation(self, token: str, user_id: UUID) -> dict[str, Any]:
        if len(token) > 128:
            raise ServiceError("invitation_not_found", status=404, retryable=False)
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT i.id AS interview_id,i.candidate_id,i.status::text,i.scheduled_at,
                       i.scheduled_end_at,i.scheduling_timezone AS timezone,
                       jp.title AS position_title,
                       c.name AS company_name,clock_timestamp() AS server_time
                FROM interview_private.interview_invitations inv
                JOIN interviews i ON i.id=inv.interview_id
                  AND i.company_id=inv.company_id AND i.candidate_id=inv.candidate_id
                JOIN companies c ON c.id=i.company_id
                LEFT JOIN job_postings jp ON jp.id=i.job_id
                WHERE inv.token_digest=$1 AND inv.revoked_at IS NULL
                  AND inv.expires_at>clock_timestamp() AND inv.candidate_id=$2
                  AND i.status <> 'cancelled'
                """,
                token_digest(token),
                user_id,
            )
            if row is None:
                raise ServiceError("invitation_not_found", status=404, retryable=False)
            result = dict(row)
            scheduled_start = row["scheduled_at"].astimezone(UTC)
            scheduled_end = row["scheduled_end_at"].astimezone(UTC)
            server_time = row["server_time"].astimezone(UTC)
            result.update(
                {
                    "scheduled_start": scheduled_start,
                    "server_time": server_time,
                    "duration_minutes": int(
                        (scheduled_end - scheduled_start).total_seconds() / 60
                    ),
                    "start_allowed": str(row["status"]) in {"scheduled", "in_progress"}
                    and server_time >= scheduled_start,
                    "seconds_until_start": max(
                        0,
                        math.ceil((scheduled_start - server_time).total_seconds()),
                    ),
                }
            )
            return result

    async def resolve_intent_entities(
        self,
        actor: Actor,
        intent: SchedulingIntent,
    ) -> dict[str, Any]:
        """Resolve names only within the authorized tenant; ambiguity never picks a row."""
        async with self.db.pool.acquire() as conn:
            await self._require_scheduling_manager(conn, actor.user_id, actor.company_id)
            company_settings = await self._company_settings(conn, actor.company_id)
            clarification = list(intent.clarification_fields)
            updates: dict[str, Any] = {}
            needs_schedule = intent.action.value != "CANCEL"
            if needs_schedule and intent.timezone is None:
                if company_settings.timezone:
                    updates["timezone"] = company_settings.timezone
                elif "timezone" not in clarification:
                    clarification.append("timezone")
            if needs_schedule and intent.duration_minutes is None:
                if company_settings.default_duration_minutes is not None:
                    updates["duration_minutes"] = company_settings.default_duration_minutes
                elif "duration_minutes" not in clarification:
                    clarification.append("duration_minutes")
            if needs_schedule and intent.buffer_minutes is None:
                updates["buffer_minutes"] = company_settings.default_buffer_minutes
            if needs_schedule and intent.start_date is None and "date" not in clarification:
                clarification.append("date")
            if (
                needs_schedule
                and intent.window_start is not None
                and intent.window_end is None
                and (intent.duration_minutes or updates.get("duration_minutes")) is not None
                and intent.start_date is not None
            ):
                duration = int(intent.duration_minutes or updates["duration_minutes"])
                local_start = datetime.combine(intent.start_date, intent.window_start)
                local_end = local_start + timedelta(minutes=duration)
                if local_end.date() == local_start.date():
                    updates["window_end"] = local_end.time()
                elif "window_end" not in clarification:
                    clarification.append("window_end")
            elif needs_schedule and (
                (intent.window_start is None) != (intent.window_end is None)
            ):
                missing = "window_start" if intent.window_start is None else "window_end"
                if missing not in clarification:
                    clarification.append(missing)
            if intent.start_date is not None and intent.end_date is None:
                updates["end_date"] = intent.start_date
            if updates:
                intent = intent.model_copy(update=updates)
            position_id: UUID | None = None
            if intent.position_name:
                positions = await conn.fetch(
                    """
                    SELECT id,title FROM job_postings
                    WHERE company_id=$1 AND status<>'archived' AND lower(title)=lower($2)
                    ORDER BY id LIMIT 3
                    """,
                    actor.company_id,
                    intent.position_name.strip(),
                )
                if len(positions) == 1:
                    position_id = UUID(str(positions[0]["id"]))
                elif "position" not in clarification:
                    clarification.append("position")
            elif intent.action.value not in {"CANCEL", "RESCHEDULE"} and "position" not in clarification:
                clarification.append("position")

            resolved_candidates: list[dict[str, Any]] = []
            for name in intent.candidate_names:
                if position_id is not None:
                    candidates = await conn.fetch(
                        """
                        SELECT p.id,p.full_name FROM job_candidates jc
                        JOIN profiles p ON p.id=jc.candidate_id
                        WHERE jc.company_id=$1 AND jc.job_id=$2
                          AND lower(p.full_name)=lower($3)
                        ORDER BY p.id LIMIT 3
                        """,
                        actor.company_id,
                        position_id,
                        name.strip(),
                    )
                else:
                    candidates = await conn.fetch(
                        """
                        SELECT DISTINCT p.id,p.full_name FROM job_candidates jc
                        JOIN profiles p ON p.id=jc.candidate_id
                        WHERE jc.company_id=$1 AND lower(p.full_name)=lower($2)
                        ORDER BY p.id LIMIT 3
                        """,
                        actor.company_id,
                        name.strip(),
                    )
                if len(candidates) == 1:
                    resolved_candidates.append(
                        {"candidate_id": UUID(str(candidates[0]["id"])), "name": str(candidates[0]["full_name"])}
                    )
                else:
                    field = f"candidate:{name}"
                    if field not in clarification:
                        clarification.append(field)
            if not intent.candidate_names and "candidates" not in clarification:
                clarification.append("candidates")
            return {
                "intent": intent.model_dump(mode="json"),
                "position_id": position_id,
                "candidate_matches": resolved_candidates,
                "clarification_required": bool(clarification),
                "clarification_fields": clarification,
            }


def request_fingerprint(request: SchedulingRequest) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
