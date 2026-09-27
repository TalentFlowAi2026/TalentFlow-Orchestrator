from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.persistence.repository import SchedulingRepository
from talentflow_orchestrator.scheduling.models import BusyKind, SchedulingMode, SchedulingRequest


class Acquisition:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def __aenter__(self) -> object:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        del args


class Pool:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    def acquire(self) -> Acquisition:
        return Acquisition(self.connection)


def repository(connection: object) -> SchedulingRepository:
    settings = Settings.model_validate({})
    database = Database(settings)
    database._pool = cast(Any, Pool(connection))
    return SchedulingRepository(database, settings)


class BusyConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        del args
        if "FROM interviews i" in query:
            return self.rows
        return []


@pytest.mark.asyncio
async def test_cross_tenant_busy_interview_reference_is_redacted() -> None:
    company_id = uuid4()
    own_id = uuid4()
    foreign_id = uuid4()
    candidate_id = uuid4()
    starts = datetime(2026, 10, 1, 7, tzinfo=UTC)
    rows = [
        {
            "id": own_id,
            "company_id": company_id,
            "candidate_id": candidate_id,
            "scheduled_at": starts,
            "scheduled_end_at": starts + timedelta(minutes=30),
            "scheduling_buffer_minutes": 10,
        },
        {
            "id": foreign_id,
            "company_id": uuid4(),
            "candidate_id": candidate_id,
            "scheduled_at": starts + timedelta(hours=1),
            "scheduled_end_at": starts + timedelta(hours=1, minutes=30),
            "scheduling_buffer_minutes": 0,
        },
    ]
    request = SchedulingRequest(
        company_id=company_id,
        job_id=uuid4(),
        candidate_ids=[candidate_id],
        timezone="Asia/Gaza",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
        window_start=time(9),
        window_end=time(17),
        duration_minutes=30,
        mode=SchedulingMode.AI_ASSISTED,
    )

    intervals = await repository(BusyConnection(rows)).busy_intervals(request)
    candidate_intervals = [
        interval for interval in intervals if interval.kind == BusyKind.CANDIDATE
    ]
    assert [interval.reference_id for interval in candidate_intervals] == [own_id, None]
    assert candidate_intervals[0].end == starts + timedelta(minutes=30)
    assert candidate_intervals[0].buffer_minutes == 10


class InvitationConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.query = ""

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        del args
        self.query = query
        return self.row


@pytest.mark.asyncio
async def test_invitation_returns_server_authoritative_waiting_room_state() -> None:
    now = datetime(2026, 10, 1, 6, tzinfo=UTC)
    starts = now + timedelta(minutes=20)
    connection = InvitationConnection(
        {
            "interview_id": uuid4(),
            "candidate_id": uuid4(),
            "status": "scheduled",
            "scheduled_at": starts,
            "scheduled_end_at": starts + timedelta(minutes=30),
            "timezone": "Asia/Gaza",
            "position_title": "Backend Engineer",
            "company_name": "TalentFlow",
            "server_time": now,
        }
    )
    result = await repository(connection).resolve_invitation("opaque-token", uuid4())
    assert result["scheduled_start"] == starts
    assert result["server_time"] == now
    assert result["duration_minutes"] == 30
    assert result["timezone"] == "Asia/Gaza"
    assert result["start_allowed"] is False
    assert result["seconds_until_start"] == 1200
    assert "i.candidate_id=inv.candidate_id" in connection.query


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["wrong-account", "expired", "revoked"])
async def test_unusable_invitation_is_not_disclosed(token: str) -> None:
    with pytest.raises(ServiceError) as error:
        await repository(InvitationConnection(None)).resolve_invitation(token, uuid4())
    assert error.value.code == "invitation_not_found"
    assert error.value.status == 404
