"""Regression tests for automatic scheduling of a role without a posting."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import Actor, ServiceError
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.persistence.repository import SchedulingRepository


class FakeConnection:
    def __init__(self, *, has_posting: bool, has_role: bool = True) -> None:
        self.existing_posting = uuid4() if has_posting else None
        self.created_posting: UUID | None = None
        self.has_role = has_role
        self.insert_count = 0
        self.assignments: list[tuple[object, ...]] = []
        self.lock_count = 0

    def transaction(self) -> "FakeConnection":
        return self

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def fetchrow(self, query: str, *_args: object) -> dict[str, object] | None:
        if "FROM company_members" in query:
            return {"role": "company_admin"}
        if "FROM job_roles" in query:
            return {"title": "Social Media Manger", "description": "Manage company social accounts"} if self.has_role else None
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: object) -> object:
        if "SELECT id" in query and "FROM job_postings" in query:
            return self.existing_posting or self.created_posting
        if "INSERT INTO job_postings" in query:
            self.insert_count += 1
            assert args[2] == "Social Media Manger"
            assert args[3] == "Manage company social accounts"
            self.created_posting = uuid4()
            return self.created_posting
        if "FROM profiles p" in query:
            return True
        raise AssertionError(query)

    async def execute(self, query: str, *args: object) -> str:
        if "pg_advisory_xact_lock" in query:
            self.lock_count += 1
            return "SELECT 1"
        if "INSERT INTO job_candidates" in query:
            self.assignments.append(args)
            return "INSERT 0 1"
        raise AssertionError(query)


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self) -> FakeConnection:
        return self.conn


def make_repository(conn: FakeConnection) -> SchedulingRepository:
    settings = Settings()
    database = Database(settings)
    database._pool = cast(Any, FakePool(conn))
    return SchedulingRepository(database, settings)


@pytest.mark.asyncio
async def test_auto_creates_posting_when_company_role_has_no_posting() -> None:
    conn = FakeConnection(has_posting=False)
    actor = Actor(user_id=uuid4(), company_id=uuid4(), role="company_admin")
    candidate_id = uuid4()
    repo = make_repository(conn)

    first = await repo.resolve_job_posting_for_role(
        actor=actor, job_role_id=uuid4(), candidate_id=candidate_id
    )
    assert first == conn.created_posting
    assert conn.insert_count == 1
    assert conn.assignments[0][1] == first
    assert conn.lock_count == 1

    second = await repo.resolve_job_posting_for_role(
        actor=actor, job_role_id=uuid4(), candidate_id=candidate_id
    )
    assert second == first
    assert conn.insert_count == 1


@pytest.mark.asyncio
async def test_auto_reuses_existing_posting() -> None:
    conn = FakeConnection(has_posting=True)
    actor = Actor(user_id=uuid4(), company_id=uuid4(), role="company_admin")
    result = await make_repository(conn).resolve_job_posting_for_role(
        actor=actor, job_role_id=uuid4(), candidate_id=uuid4()
    )
    assert result == conn.existing_posting
    assert conn.insert_count == 0


@pytest.mark.asyncio
async def test_unknown_role_does_not_create_posting() -> None:
    conn = FakeConnection(has_posting=False, has_role=False)
    actor = Actor(user_id=uuid4(), company_id=uuid4(), role="company_admin")
    with pytest.raises(ServiceError) as exc:
        await make_repository(conn).resolve_job_posting_for_role(
            actor=actor, job_role_id=uuid4(), candidate_id=uuid4()
        )
    assert exc.value.code == "job_role_not_found"
    assert conn.insert_count == 0
