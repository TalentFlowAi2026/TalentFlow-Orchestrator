"""Authenticated scheduling proposal and interview mutation endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, status

from talentflow_orchestrator.api.dependencies import (
    authenticated_user,
    correlation_id,
    idempotency_key,
)
from talentflow_orchestrator.api.schemas import (
    AutoScheduleRequest,
    CancelRequest,
    CandidateAssignmentRequest,
    RescheduleAvailabilityRequest,
    RescheduleRequest,
)
from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import Actor, ServiceError
from talentflow_orchestrator.persistence.repository import SchedulingRepository, request_fingerprint
from talentflow_orchestrator.providers.base import CalendarProvider
from talentflow_orchestrator.scheduling.models import (
    BusyInterval,
    BusyKind,
    ProposalDraft,
    SchedulingMode,
    SchedulingPolicy,
    SchedulingRequest,
)
from talentflow_orchestrator.workers.handlers import AccessTokenManager

router = APIRouter(prefix="/v1", tags=["scheduling"])
logger = logging.getLogger(__name__)
UserId = Annotated[UUID, Depends(authenticated_user)]
IdempotencyKey = Annotated[UUID, Depends(idempotency_key)]


def _propose(
    request: Request,
    repository: SchedulingRepository,
    body: SchedulingRequest,
    policy: SchedulingPolicy,
    busy: list[BusyInterval],
) -> ProposalDraft:
    try:
        return repository.engine.propose(
            body,
            policy,
            busy,
            max_horizon_days=cast(
                Settings, request.app.state.settings
            ).max_scheduling_horizon_days,
        )
    except ValueError as exc:
        if str(exc) == "scheduling_horizon_exceeded":
            raise ServiceError(
                str(exc), status=422, retryable=False
            ) from exc
        raise


async def _busy_with_connected_calendar(
    request: Request,
    repository: SchedulingRepository,
    actor: Actor,
    body: SchedulingRequest,
) -> list[BusyInterval]:
    busy = await repository.busy_intervals(body)
    busy.extend(await _connected_calendar_busy(request, repository, actor, body))
    return busy


async def _connected_calendar_busy(
    request: Request,
    repository: SchedulingRepository,
    actor: Actor,
    body: SchedulingRequest,
    *,
    exclude_exact_interval: tuple[datetime, datetime] | None = None,
) -> list[BusyInterval]:
    calendar_context = await repository.calendar_availability_context(actor, body)
    if calendar_context is None:
        return []
    account_id, calendars = calendar_context
    token_manager = cast(AccessTokenManager, request.app.state.access_token_manager)
    provider = cast(CalendarProvider, request.app.state.calendar_provider)
    access_token, _ = await token_manager.token(account_id, body.company_id)
    zone = ZoneInfo(body.timezone)
    time_min = datetime.combine(body.start_date, time.min, tzinfo=zone).astimezone(UTC)
    time_max = datetime.combine(
        body.end_date + timedelta(days=1), time.min, tzinfo=zone
    ).astimezone(UTC)
    provider_busy = await provider.free_busy(
        access_token,
        time_min=time_min,
        time_max=time_max,
        calendar_ids=list(calendars.values()),
    )
    result: list[BusyInterval] = []
    for interviewer_id, calendar_id in calendars.items():
        for interval in provider_busy.get(calendar_id, []):
            if exclude_exact_interval is not None and (
                interval.start.astimezone(UTC) == exclude_exact_interval[0]
                and interval.end.astimezone(UTC) == exclude_exact_interval[1]
            ):
                # FreeBusy has no event identifiers. The exact current TalentFlow
                # event is excluded so it cannot conflict with its own reschedule.
                continue
            result.append(
                BusyInterval(
                    start=interval.start,
                    end=interval.end,
                    kind=BusyKind.CALENDAR,
                    owner_id=interviewer_id,
                    source="google_calendar",
                )
            )
    return result


@router.post("/jobs/{job_id}/candidates")
async def assign_candidates(
    job_id: UUID,
    body: CandidateAssignmentRequest,
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, body.company_id)
    assigned = await repository.assign_candidates(
        actor=actor,
        job_id=job_id,
        candidate_ids=body.candidate_ids,
    )
    return {"job_id": job_id, "candidate_ids": assigned}


@router.post("/scheduling/auto", status_code=status.HTTP_201_CREATED)
async def auto_schedule(
    body: AutoScheduleRequest,
    request: Request,
    user_id: UserId,
    key: IdempotencyKey,
) -> dict[str, Any]:
    """Resolve job role, choose the least-loaded slot, confirm, and enqueue integrations."""
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, body.company_id)
    job_id = await repository.resolve_job_posting_for_role(
        actor=actor,
        job_role_id=body.job_role_id,
        candidate_id=body.candidate_id,
    )
    scheduling_request = SchedulingRequest(
        company_id=body.company_id,
        job_id=job_id,
        prompt_template_id=body.prompt_template_id,
        candidate_ids=[body.candidate_id],
        interviewer_ids=body.interviewer_ids,
        timezone=body.timezone,
        start_date=body.date,
        end_date=body.date,
        duration_minutes=body.duration_minutes,
        buffer_minutes=body.buffer_minutes,
        language=body.language,
        mode=SchedulingMode.AUTO_BALANCED,
        notes=body.notes,
    )
    await repository.validate_scope(actor, scheduling_request)
    policy = await repository.policy(body.company_id)
    draft = _propose(
        request,
        repository,
        scheduling_request,
        policy,
        await _busy_with_connected_calendar(
            request, repository, actor, scheduling_request
        ),
    )
    if not any(item.schedulable for item in draft.items):
        raise ServiceError("no_schedulable_candidates", status=409, retryable=False)

    fingerprint = request_fingerprint(scheduling_request)
    proposal_id, _ = await repository.create_proposal(
        actor=actor,
        request=scheduling_request,
        draft=draft,
        idempotency_key=key,
        correlation_id=correlation_id(request),
        request_fingerprint=fingerprint,
    )
    external_busy = await _connected_calendar_busy(
        request, repository, actor, scheduling_request
    )
    interviews = await repository.confirm(
        proposal_id,
        actor,
        idempotency_key=key,
        external_busy=external_busy,
    )
    return {
        "proposal_id": proposal_id,
        "job_id": job_id,
        "job_role_id": body.job_role_id,
        "interviews": interviews,
    }


@router.post("/scheduling/availability")
async def availability(
    body: SchedulingRequest,
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, body.company_id)
    await repository.validate_scope(actor, body)
    policy = await repository.policy(body.company_id)
    busy = await _busy_with_connected_calendar(request, repository, actor, body)
    draft = _propose(request, repository, body, policy, busy)
    if draft.has_conflicts:
        logger.info(
            "schedule_conflict_detected",
            extra={
                "company_id": str(body.company_id),
                "correlation_id": str(correlation_id(request)),
            },
        )
    return draft.model_dump(mode="json")


@router.post("/scheduling/proposals", status_code=status.HTTP_201_CREATED)
async def create_proposal(
    body: SchedulingRequest,
    request: Request,
    user_id: UserId,
    key: IdempotencyKey,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, body.company_id)
    await repository.validate_scope(actor, body)
    policy = await repository.policy(body.company_id)
    draft = _propose(
        request,
        repository,
        body,
        policy,
        await _busy_with_connected_calendar(request, repository, actor, body),
    )
    proposal_id, created = await repository.create_proposal(
        actor=actor,
        request=body,
        draft=draft,
        idempotency_key=key,
        correlation_id=correlation_id(request),
        request_fingerprint=request_fingerprint(body),
    )
    result = await repository.proposal(proposal_id, actor)
    result["created"] = created
    logger.info(
        "scheduling_proposal_created" if created else "scheduling_proposal_replayed",
        extra={
            "company_id": str(body.company_id),
            "proposal_id": str(proposal_id),
            "correlation_id": str(correlation_id(request)),
        },
    )
    return result


@router.get("/scheduling/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: UUID,
    company_id: Annotated[UUID, Query()],
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, company_id)
    return await repository.proposal(proposal_id, actor)


@router.post("/scheduling/proposals/{proposal_id}/confirm")
async def confirm_proposal(
    proposal_id: UUID,
    company_id: Annotated[UUID, Query()],
    request: Request,
    user_id: UserId,
    key: IdempotencyKey,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, company_id)
    proposal = await repository.proposal(proposal_id, actor)
    constraints = SchedulingRequest.model_validate(
        proposal["proposal"]["structured_constraints"]
    )
    external_busy = await _connected_calendar_busy(
        request, repository, actor, constraints
    )
    interviews = await repository.confirm(
        proposal_id,
        actor,
        idempotency_key=key,
        external_busy=external_busy,
    )
    logger.info(
        "schedule_confirmed",
        extra={
            "company_id": str(company_id),
            "proposal_id": str(proposal_id),
            "interview_count": len(interviews),
            "correlation_id": str(correlation_id(request)),
        },
    )
    return {"proposal_id": proposal_id, "interviews": interviews}


@router.post("/interviews/{interview_id}/reschedule/availability")
async def reschedule_availability(
    interview_id: UUID,
    body: RescheduleAvailabilityRequest,
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    """Preview date-only, window, or exact rescheduling without side effects."""
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, body.company_id)
    search, current_window = await repository.reschedule_search_request(
        actor=actor,
        interview_id=interview_id,
        timezone=body.timezone,
        start_date=body.start_date,
        end_date=body.end_date,
        window_start=body.window_start,
        window_end=body.window_end,
        duration_minutes=body.duration_minutes,
        buffer_minutes=body.buffer_minutes,
        interviewer_ids=body.interviewer_ids,
        mode=body.mode,
    )
    policy = await repository.policy(body.company_id)
    busy = await repository.reschedule_busy_intervals(
        actor=actor,
        interview_id=interview_id,
        request=search,
    )
    busy.extend(
        await _connected_calendar_busy(
            request,
            repository,
            actor,
            search,
            exclude_exact_interval=current_window,
        )
    )
    draft = _propose(request, repository, search, policy, busy)
    return draft.model_dump(mode="json")


@router.post("/interviews/{interview_id}/reschedule")
async def reschedule_interview(
    interview_id: UUID,
    body: RescheduleRequest,
    request: Request,
    user_id: UserId,
    key: IdempotencyKey,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor: Actor = await repository.require_scheduling_manager(user_id, body.company_id)
    preview, current_window = await repository.reschedule_request(
        actor=actor,
        interview_id=interview_id,
        start=body.start,
        duration_minutes=body.duration_minutes,
        timezone=body.timezone,
        buffer_minutes=body.buffer_minutes,
        interviewer_ids=body.interviewer_ids,
    )
    external_busy = await _connected_calendar_busy(
        request,
        repository,
        actor,
        preview,
        exclude_exact_interval=current_window,
    )
    result = await repository.reschedule(
        actor=actor,
        interview_id=interview_id,
        start=body.start,
        duration_minutes=body.duration_minutes,
        timezone=body.timezone,
        buffer_minutes=body.buffer_minutes,
        interviewer_ids=body.interviewer_ids,
        idempotency_key=key,
        correlation_id=correlation_id(request),
        external_busy=external_busy,
    )
    logger.info(
        "interview_rescheduled",
        extra={
            "company_id": str(body.company_id),
            "interview_id": str(interview_id),
            "correlation_id": str(correlation_id(request)),
        },
    )
    return result


@router.get("/interviews/{interview_id}/integration-status")
async def interview_integration_status(
    interview_id: UUID,
    company_id: Annotated[UUID, Query()],
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor = await repository.require_scheduling_manager(user_id, company_id)
    return await repository.integration_status(actor, interview_id)


@router.post("/interviews/{interview_id}/cancel")
async def cancel_interview(
    interview_id: UUID,
    body: CancelRequest,
    request: Request,
    user_id: UserId,
    key: IdempotencyKey,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    actor: Actor = await repository.require_scheduling_manager(user_id, body.company_id)
    result = await repository.cancel(
        actor=actor,
        interview_id=interview_id,
        reason=body.reason,
        idempotency_key=key,
        correlation_id=correlation_id(request),
    )
    logger.info(
        "interview_cancelled",
        extra={
            "company_id": str(body.company_id),
            "interview_id": str(interview_id),
            "correlation_id": str(correlation_id(request)),
        },
    )
    return result


@router.get("/invitations/{token}")
async def resolve_invitation(
    token: str,
    request: Request,
    user_id: UserId,
) -> dict[str, Any]:
    repository = cast(SchedulingRepository, request.app.state.scheduling_repository)
    return await repository.resolve_invitation(token, user_id)
