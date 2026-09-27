"""OAuth, invitation, and provider-action persistence isolated from business scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import Actor, OrchestratorJob, ServiceError
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.providers.base import OAuthTokens, ProviderAccount
from talentflow_orchestrator.security.crypto import EncryptedValue, SecretCipher

if TYPE_CHECKING:
    DbConnection = asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record]
else:
    DbConnection = Any


@dataclass(frozen=True)
class OAuthState:
    actor: Actor
    code_verifier: str


@dataclass(frozen=True)
class ActionContext:
    action_id: UUID
    company_id: UUID
    interview_id: UUID
    provider_account_id: UUID
    action: str
    schedule_version: int
    correlation_id: UUID
    interview_status: str
    scheduled_at: datetime
    scheduled_end_at: datetime
    timezone: str
    candidate_name: str
    candidate_email: str | None
    interviewer_emails: list[str]
    company_name: str
    position_title: str
    invitation_url: str | None
    prior_provider_resource_id: str | None


class IntegrationRepository:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.oauth_cipher = SecretCipher(settings, "google-oauth")
        self.invitation_cipher = SecretCipher(settings, "invitation")

    async def create_oauth_state(
        self,
        *,
        actor: Actor,
        state_digest: bytes,
        code_verifier: str,
        expires_at: datetime,
    ) -> None:
        context = state_digest + actor.company_id.bytes + actor.user_id.bytes
        encrypted = self.oauth_cipher.encrypt(code_verifier, context=context)
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._require_company_admin(conn, actor.user_id, actor.company_id)
            await conn.execute(
                """
                INSERT INTO interview_private.oauth_states
                  (state_digest,company_id,actor_id,provider,code_verifier_ciphertext,
                   code_verifier_nonce,encryption_key_version,expires_at)
                VALUES ($1,$2,$3,'google',$4,$5,$6,$7)
                """,
                state_digest,
                actor.company_id,
                actor.user_id,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.key_version,
                expires_at,
            )

    async def consume_oauth_state(self, state_digest: bytes) -> OAuthState:
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT * FROM interview_private.oauth_states
                WHERE state_digest=$1 AND consumed_at IS NULL AND expires_at>clock_timestamp()
                FOR UPDATE
                """,
                state_digest,
            )
            if row is None:
                raise ServiceError("oauth_state_invalid", status=400, retryable=False)
            actor = await self._require_company_admin(
                conn, UUID(str(row["actor_id"])), UUID(str(row["company_id"]))
            )
            context = state_digest + actor.company_id.bytes + actor.user_id.bytes
            verifier = self.oauth_cipher.decrypt(
                EncryptedValue(
                    bytes(row["code_verifier_ciphertext"]),
                    bytes(row["code_verifier_nonce"]),
                    str(row["encryption_key_version"]),
                ),
                context=context,
            )
            await conn.execute(
                """
                UPDATE interview_private.oauth_states SET consumed_at=clock_timestamp()
                WHERE state_digest=$1 AND consumed_at IS NULL
                """,
                state_digest,
            )
            return OAuthState(actor=actor, code_verifier=verifier)

    async def save_google_account(self, actor: Actor, tokens: OAuthTokens) -> UUID:
        account_id = uuid5(
            NAMESPACE_URL,
            f"talentflow:google-account:{actor.company_id}:{tokens.provider_subject}",
        )
        context = account_id.bytes + actor.company_id.bytes
        access = self.oauth_cipher.encrypt(tokens.access_token, context=context)
        refresh = (
            self.oauth_cipher.encrypt(tokens.refresh_token, context=context)
            if tokens.refresh_token
            else None
        )
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._require_company_admin(conn, actor.user_id, actor.company_id)
            existing = await conn.fetchrow(
                """
                SELECT refresh_token_ciphertext,refresh_token_nonce,encryption_key_version
                FROM interview_private.provider_accounts
                WHERE company_id=$1 AND id=$2 FOR UPDATE
                """,
                actor.company_id,
                account_id,
            )
            if refresh is not None:
                refresh_ciphertext = refresh.ciphertext
                refresh_nonce = refresh.nonce
                key_version = refresh.key_version
            else:
                if existing is None:
                    raise ServiceError(
                        "oauth_refresh_token_missing", status=422, retryable=False
                    )
                if (
                    existing["refresh_token_ciphertext"] is None
                    or existing["refresh_token_nonce"] is None
                ):
                    raise ServiceError(
                        "oauth_refresh_token_missing", status=422, retryable=False
                    )
                if str(existing["encryption_key_version"]) != self.oauth_cipher.key_version:
                    raise ServiceError(
                        "oauth_refresh_token_reauthorization_required",
                        status=409,
                        retryable=False,
                    )
                refresh_ciphertext = bytes(existing["refresh_token_ciphertext"])
                refresh_nonce = bytes(existing["refresh_token_nonce"])
                key_version = str(existing["encryption_key_version"])
            await conn.execute(
                """
                INSERT INTO interview_private.provider_accounts
                  (id,company_id,provider,provider_subject,account_email,scopes,
                   access_token_ciphertext,access_token_nonce,access_token_expires_at,
                   refresh_token_ciphertext,refresh_token_nonce,encryption_key_version,
                   status,connected_by)
                VALUES ($1,$2,'google',$3,$4,$5,$6,$7,$8,$9,$10,$11,'active',$12)
                ON CONFLICT (company_id,provider,provider_subject) DO UPDATE SET
                  account_email=EXCLUDED.account_email,scopes=EXCLUDED.scopes,
                  access_token_ciphertext=EXCLUDED.access_token_ciphertext,
                  access_token_nonce=EXCLUDED.access_token_nonce,
                  access_token_expires_at=EXCLUDED.access_token_expires_at,
                  refresh_token_ciphertext=EXCLUDED.refresh_token_ciphertext,
                  refresh_token_nonce=EXCLUDED.refresh_token_nonce,
                  encryption_key_version=EXCLUDED.encryption_key_version,
                  status='active',revoked_at=NULL,updated_at=clock_timestamp()
                """,
                account_id,
                actor.company_id,
                tokens.provider_subject,
                str(tokens.account_email),
                tokens.scopes,
                access.ciphertext,
                access.nonce,
                tokens.expires_at,
                refresh_ciphertext,
                refresh_nonce,
                key_version,
                actor.user_id,
            )
        return account_id

    async def provider_account(self, account_id: UUID, company_id: UUID) -> ProviderAccount:
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM interview_private.provider_accounts
                WHERE id=$1 AND company_id=$2 AND status='active'
                """,
                account_id,
                company_id,
            )
        if row is None:
            raise ServiceError("calendar_not_connected", status=409, retryable=False)
        context = account_id.bytes + company_id.bytes
        access_token = None
        if row["access_token_ciphertext"] is not None:
            access_token = self.oauth_cipher.decrypt(
                EncryptedValue(
                    bytes(row["access_token_ciphertext"]),
                    bytes(row["access_token_nonce"]),
                    str(row["encryption_key_version"]),
                ),
                context=context,
            )
        refresh_token = self.oauth_cipher.decrypt(
            EncryptedValue(
                bytes(row["refresh_token_ciphertext"]),
                bytes(row["refresh_token_nonce"]),
                str(row["encryption_key_version"]),
            ),
            context=context,
        )
        return ProviderAccount(
            id=account_id,
            company_id=company_id,
            account_email=str(row["account_email"]),
            scopes=list(row["scopes"]),
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=row["access_token_expires_at"],
        )

    async def update_access_token(
        self,
        account: ProviderAccount,
        access_token: str,
        expires_at: datetime,
    ) -> None:
        context = account.id.bytes + account.company_id.bytes
        encrypted = self.oauth_cipher.encrypt(access_token, context=context)
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE interview_private.provider_accounts
                SET access_token_ciphertext=$1,access_token_nonce=$2,access_token_expires_at=$3,
                    encryption_key_version=$4,updated_at=clock_timestamp()
                WHERE id=$5 AND company_id=$6 AND status='active'
                """,
                encrypted.ciphertext,
                encrypted.nonce,
                expires_at,
                encrypted.key_version,
                account.id,
                account.company_id,
            )

    async def revoke_account(self, actor: Actor, account_id: UUID) -> None:
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._require_company_admin(conn, actor.user_id, actor.company_id)
            result = await conn.execute(
                """
                UPDATE interview_private.provider_accounts
                SET status='revoked',revoked_at=clock_timestamp(),updated_at=clock_timestamp(),
                    access_token_ciphertext=NULL,access_token_nonce=NULL,access_token_expires_at=NULL,
                    refresh_token_ciphertext=NULL,refresh_token_nonce=NULL
                WHERE id=$1 AND company_id=$2
                """,
                account_id,
                actor.company_id,
            )
        if result != "UPDATE 1":
            raise ServiceError("calendar_not_connected", status=409, retryable=False)

    async def begin_action(self, job: OrchestratorJob) -> ActionContext | None:
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT ia.*,i.status::text AS interview_status,i.scheduled_at,i.scheduled_end_at,
                       i.scheduling_timezone,i.schedule_version,p.full_name AS candidate_name,
                       p.email AS candidate_email,c.name AS company_name,jp.title AS position_title,
                       COALESCE(
                         ARRAY(
                           SELECT interviewer.email::text
                           FROM interview_interviewers ii
                           JOIN profiles interviewer ON interviewer.id=ii.interviewer_id
                           WHERE ii.company_id=ia.company_id
                             AND ii.interview_id=ia.interview_id
                             AND interviewer.email IS NOT NULL
                           ORDER BY interviewer.email
                         ),
                         ARRAY[]::text[]
                       ) AS interviewer_emails
                FROM integration_actions ia
                JOIN interviews i ON i.id=ia.interview_id AND i.company_id=ia.company_id
                JOIN profiles p ON p.id=i.candidate_id
                JOIN companies c ON c.id=i.company_id
                LEFT JOIN job_postings jp ON jp.id=i.job_id
                WHERE ia.id=$1 AND ia.company_id=$2 AND ia.interview_id=$3
                FOR UPDATE OF ia,i
                """,
                job.integration_action_id,
                job.company_id,
                job.interview_id,
            )
            if row is None:
                raise ServiceError("integration_action_not_found", retryable=False)
            if str(row["status"]) == "succeeded":
                return None
            if int(row["schedule_version"]) != job.expected_schedule_version:
                raise ServiceError("stale_integration_action", retryable=False)
            current_version = await conn.fetchval(
                "SELECT schedule_version FROM interviews WHERE id=$1", job.interview_id
            )
            if int(current_version) != job.expected_schedule_version:
                await conn.execute(
                    "UPDATE integration_actions SET status='cancelled',updated_at=clock_timestamp() WHERE id=$1",
                    job.integration_action_id,
                )
                raise ServiceError("stale_integration_action", retryable=False)
            action = str(row["action"])
            status = str(row["interview_status"])
            if action in {"cancel_event", "send_cancellation"}:
                valid_state = status == "cancelled"
            else:
                valid_state = (
                    status == "scheduled"
                    and row["scheduled_end_at"].astimezone(UTC) > datetime.now(UTC)
                )
            if not valid_state:
                await conn.execute(
                    "UPDATE integration_actions SET status='cancelled',updated_at=clock_timestamp() WHERE id=$1",
                    job.integration_action_id,
                )
                raise ServiceError("stale_integration_action", retryable=False)
            updated = await conn.execute(
                """
                UPDATE integration_actions
                SET status='processing',attempt=attempt+1,started_at=COALESCE(started_at,clock_timestamp()),
                    updated_at=clock_timestamp(),error_code=NULL
                WHERE id=$1 AND status IN ('pending','retrying','processing')
                """,
                job.integration_action_id,
            )
            if updated != "UPDATE 1":
                return None
            invitation_url = await self._invitation_url(conn, job.interview_id)
            prior_resource = await conn.fetchval(
                """
                SELECT provider_resource_id FROM integration_actions
                WHERE company_id=$1 AND interview_id=$2 AND provider='google_calendar'
                  AND status='succeeded' AND provider_resource_id IS NOT NULL AND id<>$3
                ORDER BY schedule_version DESC,completed_at DESC NULLS LAST LIMIT 1
                """,
                job.company_id,
                job.interview_id,
                job.integration_action_id,
            )
            return ActionContext(
                action_id=job.integration_action_id,
                company_id=job.company_id,
                interview_id=job.interview_id,
                provider_account_id=UUID(str(row["provider_account_id"])),
                action=action,
                schedule_version=job.expected_schedule_version,
                correlation_id=job.correlation_id,
                interview_status=status,
                scheduled_at=row["scheduled_at"].astimezone(UTC),
                scheduled_end_at=row["scheduled_end_at"].astimezone(UTC),
                timezone=str(row["scheduling_timezone"]),
                candidate_name=str(row["candidate_name"]),
                candidate_email=str(row["candidate_email"]) if row["candidate_email"] else None,
                interviewer_emails=[str(value) for value in row["interviewer_emails"]],
                company_name=str(row["company_name"]),
                position_title=str(row["position_title"] or "Interview"),
                invitation_url=invitation_url,
                prior_provider_resource_id=str(prior_resource) if prior_resource else None,
            )

    async def action_succeeded(self, context: ActionContext, provider_resource_id: str) -> None:
        async with self.db.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE integration_actions
                SET status='succeeded',provider_resource_id=$1,completed_at=clock_timestamp(),
                    updated_at=clock_timestamp(),error_code=NULL
                WHERE id=$2 AND status='processing' AND schedule_version=$3
                """,
                provider_resource_id,
                context.action_id,
                context.schedule_version,
            )
        if result != "UPDATE 1":
            raise ServiceError("integration_action_state_changed", retryable=False)

    async def action_failed(self, action_id: UUID, code: str, will_retry: bool) -> None:
        status = "retrying" if will_retry else "failed"
        async with self.db.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE integration_actions SET status=$1,error_code=$2,updated_at=clock_timestamp()
                WHERE id=$3 AND status='processing'
                """,
                status,
                code,
                action_id,
            )

    async def _invitation_url(
        self, conn: DbConnection, interview_id: UUID
    ) -> str | None:
        row = await conn.fetchrow(
            """
            SELECT token_ciphertext,token_nonce,encryption_key_version
            FROM interview_private.interview_invitations
            WHERE interview_id=$1 AND revoked_at IS NULL AND expires_at>clock_timestamp()
            """,
            interview_id,
        )
        if row is None:
            return None
        token = self.invitation_cipher.decrypt(
            EncryptedValue(
                bytes(row["token_ciphertext"]),
                bytes(row["token_nonce"]),
                str(row["encryption_key_version"]),
            ),
            context=interview_id.bytes,
        )
        return self.settings.public_interview_base_url.rstrip("/") + "/" + token

    async def _require_company_admin(
        self,
        conn: DbConnection,
        user_id: UUID,
        company_id: UUID,
    ) -> Actor:
        role = await conn.fetchval(
            """
            SELECT role::text FROM company_members
            WHERE company_id=$1 AND user_id=$2 AND role='company_admin'
            """,
            company_id,
            user_id,
        )
        if role is None:
            raise ServiceError("unauthorized", status=403, retryable=False)
        return Actor(user_id=user_id, company_id=company_id, role=str(role))
