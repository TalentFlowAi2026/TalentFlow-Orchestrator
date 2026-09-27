from datetime import date, time
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import Actor, ServiceError
from talentflow_orchestrator.intent.models import IntentAction, SchedulingIntent
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.persistence.repository import SchedulingRepository


class FakeConnection:
    def __init__(
        self,
        *,
        membership: bool = True,
        role: str = "company_admin",
        positions: list[dict[str, object]] | None = None,
        candidates: list[dict[str, object]] | None = None,
        company_settings: dict[str, object] | None = None,
    ) -> None:
        self.membership = membership
        self.role = role
        self.positions = positions or []
        self.candidates = candidates or []
        self.company_settings = company_settings or {"scheduling": {}}

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        del args
        if (
            "FROM company_members" in query
            and self.membership
            and self.role in {"company_admin", "interviewer"}
        ):
            return {"role": self.role}
        return None

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        del args
        if "FROM job_postings" in query:
            return self.positions
        if "FROM job_candidates" in query:
            return self.candidates
        return []

    async def fetchval(self, query: str, *args: object) -> dict[str, object] | None:
        del args
        if "SELECT settings FROM companies" in query:
            return self.company_settings
        return None


class AcquireConnection:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        del args


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> AcquireConnection:
        return AcquireConnection(self.connection)


def repository(
    connection: FakeConnection,
    **settings_values: object,
) -> SchedulingRepository:
    settings = Settings.model_validate(settings_values)
    database = Database(settings)
    database._pool = cast(Any, FakePool(connection))
    return SchedulingRepository(database, settings)


@pytest.mark.asyncio
async def test_application_default_day_range_flows_into_company_policy() -> None:
    repo = repository(
        FakeConnection(),
        default_interview_day_start="08:30",
        default_interview_day_end="19:15",
    )

    result = await repo.policy(uuid4())

    assert result.default_interview_day_start == time(8, 30)
    assert result.default_interview_day_end == time(19, 15)


@pytest.mark.asyncio
async def test_cross_company_user_without_admin_membership_is_rejected() -> None:
    with pytest.raises(ServiceError) as error:
        await repository(FakeConnection(membership=False)).require_scheduling_manager(
            uuid4(), uuid4()
        )
    assert error.value.code == "unauthorized"
    assert error.value.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["company_admin", "interviewer"])
async def test_authorized_company_roles_can_manage_scheduling(role: str) -> None:
    company_id = uuid4()
    actor = await repository(FakeConnection(role=role)).require_scheduling_manager(
        uuid4(), company_id
    )
    assert actor.company_id == company_id
    assert actor.role == role


@pytest.mark.asyncio
async def test_candidate_role_cannot_manage_scheduling() -> None:
    with pytest.raises(ServiceError) as error:
        await repository(FakeConnection(role="candidate")).require_scheduling_manager(
            uuid4(), uuid4()
        )
    assert error.value.code == "unauthorized"
    assert error.value.status == 403


@pytest.mark.asyncio
async def test_ambiguous_position_requires_clarification() -> None:
    company_id = uuid4()
    repo = repository(
        FakeConnection(
            positions=[
                {"id": uuid4(), "title": "Backend Engineer"},
                {"id": uuid4(), "title": "Backend Engineer"},
            ]
        )
    )
    result = await repo.resolve_intent_entities(
        Actor(user_id=uuid4(), company_id=company_id, role="company_admin"),
        SchedulingIntent(
            action=IntentAction.CREATE_SINGLE,
            position_name="Backend Engineer",
            candidate_names=["Ahmed"],
        ),
    )
    assert result["position_id"] is None
    assert "position" in result["clarification_fields"]


@pytest.mark.asyncio
async def test_ambiguous_candidate_requires_clarification() -> None:
    company_id = uuid4()
    position_id = uuid4()
    repo = repository(
        FakeConnection(
            positions=[{"id": position_id, "title": "Flutter Developer"}],
            candidates=[
                {"id": uuid4(), "full_name": "Ahmed"},
                {"id": uuid4(), "full_name": "Ahmed"},
            ],
        )
    )
    result = await repo.resolve_intent_entities(
        Actor(user_id=uuid4(), company_id=company_id, role="company_admin"),
        SchedulingIntent(
            action=IntentAction.CREATE_SINGLE,
            position_name="Flutter Developer",
            candidate_names=["Ahmed"],
        ),
    )
    assert result["position_id"] == position_id
    assert result["candidate_matches"] == []
    assert "candidate:Ahmed" in result["clarification_fields"]


def test_model_intent_contract_has_names_but_no_authoritative_identifiers() -> None:
    fields = SchedulingIntent.model_fields
    assert "candidate_names" in fields
    assert "position_name" in fields
    assert "candidate_ids" not in fields
    assert "company_id" not in fields
    assert "job_id" not in fields
    assert all(not isinstance(value, UUID) for value in fields.values())


@pytest.mark.asyncio
async def test_configured_company_defaults_are_applied_without_model_invention() -> None:
    company_id = uuid4()
    position_id = uuid4()
    candidate_id = uuid4()
    repo = repository(
        FakeConnection(
            positions=[{"id": position_id, "title": "Flutter Developer"}],
            candidates=[{"id": candidate_id, "full_name": "Ahmed"}],
            company_settings={
                "scheduling": {
                    "timezone": "Asia/Gaza",
                    "default_duration_minutes": 30,
                    "default_buffer_minutes": 10,
                }
            },
        )
    )
    result = await repo.resolve_intent_entities(
        Actor(user_id=uuid4(), company_id=company_id, role="company_admin"),
        SchedulingIntent(
            action=IntentAction.CREATE_SINGLE,
            position_name="Flutter Developer",
            candidate_names=["Ahmed"],
        ),
    )
    parsed = result["intent"]
    assert parsed["timezone"] == "Asia/Gaza"
    assert parsed["duration_minutes"] == 30
    assert parsed["buffer_minutes"] == 10
    assert "timezone" not in result["clarification_fields"]
    assert "duration_minutes" not in result["clarification_fields"]


@pytest.mark.asyncio
async def test_date_only_intent_does_not_require_configured_weekday_hours() -> None:
    company_id = uuid4()
    repo = repository(
        FakeConnection(
            company_settings={
                "scheduling": {
                    "timezone": "Asia/Gaza",
                    "default_duration_minutes": 30,
                }
            }
        )
    )
    result = await repo.resolve_intent_entities(
        Actor(user_id=uuid4(), company_id=company_id, role="interviewer"),
        SchedulingIntent(
            action=IntentAction.FIND_AVAILABILITY,
            start_date=date(2026, 10, 1),
            candidate_names=["Ahmed"],
        ),
    )
    assert "missing_scheduling_window" not in result["clarification_fields"]
    assert "window_start" not in result["clarification_fields"]
    assert "window_end" not in result["clarification_fields"]


@pytest.mark.asyncio
async def test_date_only_intent_without_weekday_hours_needs_no_window_clarification() -> None:
    company_id = uuid4()
    result = await repository(
        FakeConnection(
            company_settings={
                "scheduling": {
                    "timezone": "Asia/Gaza",
                    "default_duration_minutes": 30,
                }
            }
        )
    ).resolve_intent_entities(
        Actor(user_id=uuid4(), company_id=company_id, role="company_admin"),
        SchedulingIntent(
            action=IntentAction.CREATE_SINGLE,
            start_date=date(2026, 10, 1),
            candidate_names=["Ahmed"],
        ),
    )
    assert "missing_scheduling_window" not in result["clarification_fields"]
    assert "window_start" not in result["clarification_fields"]
    assert "window_end" not in result["clarification_fields"]


@pytest.mark.asyncio
async def test_exact_intent_derives_end_from_duration() -> None:
    company_id = uuid4()
    result = await repository(
        FakeConnection(
            company_settings={"scheduling": {"timezone": "Asia/Gaza"}}
        )
    ).resolve_intent_entities(
        Actor(user_id=uuid4(), company_id=company_id, role="company_admin"),
        SchedulingIntent(
            action=IntentAction.CREATE_SINGLE,
            start_date=date(2026, 10, 1),
            window_start=time(13),
            duration_minutes=30,
            candidate_names=["Ahmed"],
        ),
    )
    assert result["intent"]["window_end"] == "13:30:00"
    assert "window_end" not in result["clarification_fields"]
