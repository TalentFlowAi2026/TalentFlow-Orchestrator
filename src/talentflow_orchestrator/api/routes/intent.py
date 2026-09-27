"""Natural-language parsing endpoint; output is non-executable until server validation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from talentflow_orchestrator.api.dependencies import authenticated_user
from talentflow_orchestrator.api.schemas import InterpretRequest
from talentflow_orchestrator.persistence.repository import SchedulingRepository
from talentflow_orchestrator.providers.gemini_intent import GeminiIntentProvider

router = APIRouter(prefix="/v1/orchestrator", tags=["intent"])
UserId = Annotated[UUID, Depends(authenticated_user)]


@router.post("/interpret")
async def interpret(
    body: InterpretRequest,
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, body.company_id)
    timezone = await repository.company_timezone(body.company_id)
    provider = cast(GeminiIntentProvider, request.app.state.intent_provider)
    intent = await provider.interpret(
        body.text,
        company_timezone=timezone,
        today=datetime.now(ZoneInfo(timezone) if timezone else UTC).date(),
    )
    return await repository.resolve_intent_entities(actor, intent)
