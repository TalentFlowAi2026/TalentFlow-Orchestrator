"""Google OAuth connection lifecycle endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from talentflow_orchestrator.api.dependencies import authenticated_user
from talentflow_orchestrator.api.schemas import GoogleAuthorizeRequest
from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.persistence.integrations import IntegrationRepository
from talentflow_orchestrator.persistence.repository import SchedulingRepository
from talentflow_orchestrator.providers.google.oauth import GoogleOAuthProvider
from talentflow_orchestrator.security.crypto import (
    new_opaque_token,
    new_pkce_verifier,
    pkce_challenge,
    token_digest,
)

router = APIRouter(prefix="/v1/integrations/google", tags=["integrations"])
UserId = Annotated[UUID, Depends(authenticated_user)]


def _ensure_enabled(request: Request) -> None:
    settings = cast(Settings, request.app.state.settings)
    if not (settings.google_calendar_enabled or settings.gmail_enabled):
        raise ServiceError("google_integration_disabled", status=409, retryable=False)


@router.post("/authorize")
async def authorize(
    body: GoogleAuthorizeRequest,
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    _ensure_enabled(request)
    scheduling_repository = cast(
        SchedulingRepository, request.app.state.scheduling_repository
    )
    integration_repository = cast(
        IntegrationRepository, request.app.state.integration_repository
    )
    oauth_provider = cast(GoogleOAuthProvider, request.app.state.oauth_provider)
    actor = await scheduling_repository.require_company_admin(
        user_id, body.company_id
    )
    state = new_opaque_token()
    verifier = new_pkce_verifier()
    await integration_repository.create_oauth_state(
        actor=actor,
        state_digest=token_digest(state),
        code_verifier=verifier,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    return {
        "authorization_url": oauth_provider.authorization_url(
            state=state,
            code_challenge=pkce_challenge(verifier),
        ),
        "expires_in": 600,
    }


@router.get("/callback")
async def callback(
    request: Request,
    state: Annotated[str, Query(min_length=20, max_length=256)],
    code: Annotated[str, Query(min_length=1, max_length=4096)],
) -> dict[str, Any]:
    _ensure_enabled(request)
    repository = cast(IntegrationRepository, request.app.state.integration_repository)
    provider = cast(GoogleOAuthProvider, request.app.state.oauth_provider)
    oauth_state = await repository.consume_oauth_state(
        token_digest(state)
    )
    tokens = await provider.exchange_code(code, oauth_state.code_verifier)
    account_id = await repository.save_google_account(oauth_state.actor, tokens)
    return {"status": "connected", "account_id": account_id, "company_id": oauth_state.actor.company_id}


@router.delete("/accounts/{account_id}")
async def revoke(
    account_id: UUID,
    company_id: Annotated[UUID, Query()],
    request: Request,
    user_id: UserId,
) -> dict[str, str]:
    scheduling_repository = cast(
        SchedulingRepository, request.app.state.scheduling_repository
    )
    integration_repository = cast(
        IntegrationRepository, request.app.state.integration_repository
    )
    provider = cast(GoogleOAuthProvider, request.app.state.oauth_provider)
    actor = await scheduling_repository.require_company_admin(user_id, company_id)
    account = await integration_repository.provider_account(account_id, company_id)
    token = account.refresh_token or account.access_token
    if token:
        await provider.revoke(token)
    await integration_repository.revoke_account(actor, account_id)
    return {"status": "revoked"}
