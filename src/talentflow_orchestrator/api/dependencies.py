"""Authentication and request metadata dependencies."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.security.auth import SupabaseAuth

_bearer = HTTPBearer(auto_error=False)


async def authenticated_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ],
) -> UUID:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ServiceError("authentication_required", status=401, retryable=False)
    auth = cast(SupabaseAuth, request.app.state.auth)
    return await auth.verify(credentials.credentials)


def idempotency_key(
    value: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> UUID:
    if value is None:
        raise ServiceError("idempotency_key_required", status=400, retryable=False)
    try:
        return UUID(value)
    except ValueError as exc:
        raise ServiceError("idempotency_key_invalid", status=400, retryable=False) from exc


def correlation_id(request: Request) -> UUID:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, UUID) else uuid4()
