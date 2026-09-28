"""Async PostgreSQL pool using the orchestrator-specific least-privilege role."""

from __future__ import annotations
import json
from collections.abc import Collection, Mapping
from typing import Final

import asyncpg

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError

REQUIRED_SCHEMA: Final[dict[str, frozenset[str]]] = {
    "interviews": frozenset(
        {
            "id", "company_id", "job_id", "candidate_id", "prompt_template_id", "status",
            "scheduled_at", "scheduled_end_at", "scheduling_timezone", "schedule_version",
            "scheduling_buffer_minutes", "cancelled_at", "cancelled_by", "cancellation_reason",
        }
    ),
    "job_candidates": frozenset({"company_id", "job_id", "candidate_id", "added_by"}),
    "scheduling_proposals": frozenset(
        {
            "id", "company_id", "job_id", "created_by", "mode", "status", "timezone",
            "structured_constraints", "request_fingerprint", "idempotency_key", "correlation_id",
            "expires_at", "confirmed_at", "confirmed_by", "version",
        }
    ),
    "scheduling_proposal_items": frozenset(
        {
            "id", "proposal_id", "company_id", "job_id", "candidate_id",
            "proposed_start_at", "proposed_end_at", "item_status", "warnings", "conflicts",
            "alternatives", "interview_id",
        }
    ),
    "interview_interviewers": frozenset({"company_id", "interview_id", "interviewer_id"}),
    "integration_actions": frozenset(
        {
            "id", "company_id", "interview_id", "provider_account_id", "provider", "action",
            "schedule_version", "idempotency_key", "status", "provider_resource_id",
            "correlation_id", "attempt", "error_code",
        }
    ),
    "scheduling_events": frozenset(
        {"id", "company_id", "proposal_id", "interview_id", "actor_id", "correlation_id", "event_type", "idempotency_key", "request_fingerprint", "data"}
    ),
    "worker_jobs": frozenset(
        {
            "id", "kind", "session_id", "correlation_id", "idempotency_key", "attempt",
            "max_attempts", "lease_token", "status", "payload", "delay_until", "leased_until",
            "last_error", "executor_service", "company_id", "interview_id", "proposal_id",
            "integration_action_id", "expected_schedule_version",
        }
    ),
    "interview_private.provider_accounts": frozenset(
        {
            "id", "company_id", "provider", "provider_subject", "account_email", "scopes",
            "access_token_ciphertext", "access_token_nonce", "access_token_expires_at",
            "refresh_token_ciphertext", "refresh_token_nonce", "encryption_key_version",
            "status", "connected_by", "revoked_at",
        }
    ),
    "interview_private.oauth_states": frozenset(
        {
            "state_digest", "company_id", "actor_id", "provider",
            "code_verifier_ciphertext", "code_verifier_nonce", "encryption_key_version",
            "expires_at", "consumed_at",
        }
    ),
    "interview_private.interview_invitations": frozenset(
        {
            "id", "company_id", "interview_id", "candidate_id", "token_digest",
            "token_ciphertext", "token_nonce", "encryption_key_version", "expires_at",
            "revoked_at", "created_by",
        }
    ),
}


class Database:
    def __init__(self, settings: Settings, *, pool_max: int | None = None) -> None:
        self.settings = settings
        self.pool_max = pool_max or settings.database_pool_max
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None

    @property
    def pool(self) -> asyncpg.Pool[asyncpg.Record]:
        if self._pool is None:
            raise ServiceError("database_unavailable")
        return self._pool

    async def open(self) -> None:
        async def init(connection: asyncpg.Connection[asyncpg.Record]) -> None:
            for name in ("json", "jsonb"):
                await connection.set_type_codec(
                    name, schema="pg_catalog", encoder=json.dumps, decoder=json.loads, format="text"
                )

        async def setup(connection: asyncpg.Connection[asyncpg.Record]) -> None:
            await connection.execute("SET ROLE talentflow_orchestrator")

        self._pool = await asyncpg.create_pool(  # type: ignore[call-overload]
            dsn=self.settings.supabase_db_url.get_secret_value(),
            min_size=self.settings.database_pool_min,
            max_size=self.pool_max,
            timeout=float(self.settings.provider_timeout_seconds),
            command_timeout=float(self.settings.provider_timeout_seconds),
            statement_cache_size=0,
            init=init,
            setup=setup,
            server_settings={
                "application_name": "talentflow-orchestrator",
                "timezone": "UTC",
                "statement_timeout": "30000",
                "idle_in_transaction_session_timeout": "60000",
            },
        )

    async def validate_schema(
        self, required: Mapping[str, Collection[str]] | None = None
    ) -> None:
        contract = required or REQUIRED_SCHEMA
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT table_schema,table_name,column_name FROM information_schema.columns
                WHERE table_schema IN ('public','interview_private')
                  AND table_name=ANY($1::text[])
                """,
                sorted(key.rsplit(".", 1)[-1] for key in contract),
            )
        actual: dict[str, set[str]] = {}
        for row in rows:
            schema = str(row["table_schema"])
            table = str(row["table_name"])
            key = table if schema == "public" else f"{schema}.{table}"
            actual.setdefault(key, set()).add(str(row["column_name"]))
        missing = [
            f"{table if '.' in table else 'public.' + table}.{column}"
            for table, columns in contract.items()
            for column in sorted(set(columns) - actual.get(table, set()))
        ]
        if missing:
            raise ServiceError("database_schema_invalid", retryable=False)

    async def ready(self) -> bool:
        return self._pool is not None

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
