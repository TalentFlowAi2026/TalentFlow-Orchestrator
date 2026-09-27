from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from talentflow_orchestrator.scheduling import (
    BusyInterval,
    BusyKind,
    DailyWorkingHours,
    SchedulingEngine,
    SchedulingMode,
    SchedulingPolicy,
    SchedulingRequest,
)


def request_for(
    candidates: list[UUID],
    *,
    mode: SchedulingMode = SchedulingMode.AI_ASSISTED,
    timezone: str = "Asia/Gaza",
    start: time | None = time(10),
    end: time | None = time(16),
    duration: int = 30,
    buffer: int = 10,
    parallelism: int = 1,
    interviewers: list[UUID] | None = None,
) -> SchedulingRequest:
    return SchedulingRequest(
        company_id=uuid4(),
        job_id=uuid4(),
        candidate_ids=candidates,
        interviewer_ids=interviewers or [],
        timezone=timezone,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
        window_start=start,
        window_end=end,
        duration_minutes=duration,
        buffer_minutes=buffer,
        language="ar",
        mode=mode,
        parallelism_limit=parallelism,
    )


def policy(
    max_parallel: int = 1,
    *,
    backend_parallel: int | None = None,
    increment: int = 5,
    default_start: time = time(9),
    default_end: time = time(18),
    working_hours: dict[int, DailyWorkingHours] | None = None,
    enforce_working_hours: bool = False,
) -> SchedulingPolicy:
    return SchedulingPolicy(
        max_parallel_ai_interviews=max_parallel,
        backend_max_parallel_ai_interviews=backend_parallel or max_parallel,
        slot_increment_minutes=increment,
        default_interview_day_start=default_start,
        default_interview_day_end=default_end,
        working_hours=working_hours or {},
        enforce_working_hours=enforce_working_hours,
    )


def test_explicit_single_scheduling_converts_intended_timezone_to_utc() -> None:
    request = request_for(
        [uuid4()],
        mode=SchedulingMode.EXPLICIT,
        start=time(10),
        end=time(10, 30),
        duration=30,
        buffer=0,
    )
    item = SchedulingEngine().propose(request, policy()).items[0]
    assert item.proposed_start == datetime(2026, 10, 1, 7, tzinfo=UTC)
    assert item.proposed_end == datetime(2026, 10, 1, 7, 30, tzinfo=UTC)


def test_bulk_allocation_honors_buffer() -> None:
    proposal = SchedulingEngine().propose(request_for([uuid4(), uuid4(), uuid4()]), policy())
    starts = [item.proposed_start for item in proposal.items]
    assert starts[0] is not None and starts[1] is not None and starts[2] is not None
    assert starts[1] - starts[0] == timedelta(minutes=40)
    assert starts[2] - starts[1] == timedelta(minutes=40)


def test_interviewer_buffer_is_not_double_counted_between_adjacent_slots() -> None:
    proposal = SchedulingEngine().propose(
        request_for([uuid4(), uuid4()], interviewers=[uuid4()]),
        policy(),
    )
    starts = [item.proposed_start for item in proposal.items]
    assert starts[0] is not None and starts[1] is not None
    assert starts[1] - starts[0] == timedelta(minutes=40)


def test_candidate_and_interviewer_conflicts_are_grouped_separately() -> None:
    candidate = uuid4()
    interviewer = uuid4()
    candidate_reference = uuid4()
    interviewer_reference = uuid4()
    request = request_for(
        [candidate],
        mode=SchedulingMode.EXPLICIT,
        start=time(10),
        end=time(10, 30),
        duration=30,
        interviewers=[interviewer],
    )
    slot_start = datetime(2026, 10, 1, 7, tzinfo=UTC)
    item = SchedulingEngine().propose(
        request,
        policy(),
        [
            BusyInterval(
                start=slot_start,
                end=slot_start + timedelta(minutes=30),
                kind=BusyKind.CANDIDATE,
                owner_id=candidate,
                reference_id=candidate_reference,
            ),
            BusyInterval(
                start=slot_start,
                end=slot_start + timedelta(minutes=30),
                kind=BusyKind.CALENDAR,
                owner_id=interviewer,
                source="google_calendar",
                reference_id=interviewer_reference,
            ),
        ],
    ).items[0]
    conflicts = {entry.code: entry.conflicting_reference_ids for entry in item.conflicts}
    assert conflicts == {
        "candidate_conflict": [candidate_reference],
        "interviewer_conflict": [interviewer_reference],
    }


def test_parallel_ai_interviews_are_bounded_by_company_and_backend_policy() -> None:
    proposal = SchedulingEngine().propose(
        request_for([uuid4(), uuid4(), uuid4()], parallelism=5),
        policy(max_parallel=2),
    )
    starts = [item.proposed_start for item in proposal.items]
    assert proposal.effective_parallelism == 2
    assert starts[0] == starts[1]
    assert starts[0] is not None
    assert starts[2] == starts[0] + timedelta(minutes=40)


def test_company_parallel_policy_applies_when_request_has_no_override() -> None:
    request = request_for([uuid4(), uuid4()]).model_copy(
        update={"parallelism_limit": None}
    )
    proposal = SchedulingEngine().propose(request, policy(max_parallel=2))
    assert proposal.items[0].proposed_start == proposal.items[1].proposed_start


def test_existing_capacity_blocks_only_when_limit_is_reached() -> None:
    slot_start = datetime(2026, 10, 1, 7, tzinfo=UTC)
    busy = [
        BusyInterval(
            start=slot_start,
            end=slot_start + timedelta(minutes=30),
            kind=BusyKind.CAPACITY,
            buffer_minutes=10,
        )
    ]
    request = request_for([uuid4()], parallelism=2)
    allowed = SchedulingEngine().propose(request, policy(max_parallel=2), busy)
    blocked = SchedulingEngine().propose(
        request.model_copy(update={"parallelism_limit": 1}), policy(), busy
    )
    assert allowed.items[0].proposed_start == slot_start
    assert blocked.items[0].proposed_start == slot_start + timedelta(minutes=40)


@pytest.mark.parametrize(
    ("source", "company_limit", "backend_limit", "expected_code"),
    [
        ("company_capacity", 1, 5, "company_capacity_exceeded"),
        ("backend_capacity", 5, 1, "backend_capacity_exceeded"),
    ],
)
def test_company_and_backend_capacity_are_enforced_independently(
    source: str,
    company_limit: int,
    backend_limit: int,
    expected_code: str,
) -> None:
    slot_start = datetime(2026, 10, 1, 7, tzinfo=UTC)
    request = request_for(
        [uuid4()],
        mode=SchedulingMode.EXPLICIT,
        start=time(10),
        end=time(10, 30),
        duration=30,
        buffer=0,
        parallelism=5,
    )
    item = SchedulingEngine().propose(
        request,
        SchedulingPolicy(
            max_parallel_ai_interviews=company_limit,
            backend_max_parallel_ai_interviews=backend_limit,
        ),
        [
            BusyInterval(
                start=slot_start,
                end=slot_start + timedelta(minutes=30),
                kind=BusyKind.CAPACITY,
                source=source,
            )
        ],
    ).items[0]
    assert not item.schedulable
    assert [conflict.code for conflict in item.conflicts] == [expected_code]


def test_invalid_timezone_duplicate_ids_and_unbounded_horizon_are_rejected() -> None:
    candidate = uuid4()
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        request_for([candidate], timezone="GMT+3-ish")
    with pytest.raises(ValidationError, match="identifiers must be unique"):
        request_for([candidate, candidate])
    request = request_for([candidate]).model_copy(update={"end_date": date(2026, 11, 1)})
    with pytest.raises(ValueError, match="scheduling_horizon_exceeded"):
        SchedulingEngine().propose(request, policy(), max_horizon_days=31)


@pytest.mark.parametrize(
    ("day", "start", "end", "expected_code"),
    [
        (date(2026, 3, 8), time(2, 30), time(3), "nonexistent_local_time"),
        (date(2026, 11, 1), time(1, 30), time(2), "ambiguous_local_time"),
    ],
)
def test_dst_invalid_exact_times_require_clarification(
    day: date, start: time, end: time, expected_code: str
) -> None:
    request = SchedulingRequest(
        company_id=uuid4(),
        job_id=uuid4(),
        candidate_ids=[uuid4()],
        timezone="America/New_York",
        start_date=day,
        end_date=day,
        window_start=start,
        window_end=end,
        duration_minutes=30,
        mode=SchedulingMode.EXPLICIT,
    )
    item = SchedulingEngine().propose(request, policy()).items[0]
    assert not item.schedulable
    assert item.conflicts[0].code == expected_code


def test_date_only_uses_default_range_without_company_hours() -> None:
    candidates = [uuid4() for _ in range(5)]
    request = request_for(candidates, start=None, end=None)
    zone_start = datetime(2026, 10, 1, 6, tzinfo=UTC)  # 09:00 Asia/Gaza
    conflicts = [
        BusyInterval(
            start=zone_start + timedelta(minutes=30),
            end=zone_start + timedelta(minutes=90),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
        BusyInterval(
            start=zone_start + timedelta(hours=4),
            end=zone_start + timedelta(hours=5),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
    ]

    proposal = SchedulingEngine().propose(
        request,
        policy(increment=10, default_start=time(9), default_end=time(17)),
        conflicts,
    )

    assert proposal.scheduled_candidates == 5
    assert proposal.unscheduled_candidates == 0
    starts = [item.proposed_start for item in proposal.items]
    assert all(start is not None for start in starts)
    assert all(
        not (start < busy.end + timedelta(minutes=10) and busy.start - timedelta(minutes=10) < start + timedelta(minutes=30))
        for start in starts
        if start is not None
        for busy in conflicts
    )


def test_date_only_without_weekday_hours_schedules_successfully() -> None:
    proposal = SchedulingEngine().propose(
        request_for([uuid4()], start=None, end=None),
        policy(default_start=time(9), default_end=time(18)),
    )

    assert proposal.scheduled_candidates == 1
    assert proposal.unscheduled_candidates == 0
    assert proposal.items[0].proposed_start == datetime(2026, 10, 1, 6, tzinfo=UTC)


def test_date_only_partial_capacity_stays_on_requested_day() -> None:
    request = request_for(
        [uuid4(), uuid4(), uuid4()],
        start=None,
        end=None,
        duration=30,
        buffer=10,
    )
    proposal = SchedulingEngine().propose(
        request,
        policy(
            increment=10,
            default_start=time(9),
            default_end=time(10, 20),
        ),
    )

    assert proposal.scheduled_candidates == 2
    assert proposal.unscheduled_candidates == 1
    assert proposal.reason == "insufficient_same_day_capacity"
    assert all(
        item.proposed_start is None
        or item.proposed_start.astimezone(ZoneInfo("Asia/Gaza")).date()
        == date(2026, 10, 1)
        for item in proposal.items
    )


def test_raw_calendar_busy_interval_respects_requested_buffer() -> None:
    interviewer = uuid4()
    exact = request_for(
        [uuid4()],
        mode=SchedulingMode.EXPLICIT,
        start=time(10, 30),
        end=time(11),
        duration=30,
        buffer=10,
        interviewers=[interviewer],
    )
    calendar_busy = BusyInterval(
        start=datetime(2026, 10, 1, 7, tzinfo=UTC),
        end=datetime(2026, 10, 1, 7, 30, tzinfo=UTC),
        kind=BusyKind.CALENDAR,
        owner_id=interviewer,
        source="google_calendar",
    )

    item = SchedulingEngine().propose(
        exact,
        policy(increment=10, default_start=time(9), default_end=time(17)),
        [calendar_busy],
    ).items[0]

    assert not item.schedulable
    assert item.conflicts[0].code == "interviewer_conflict"
    assert item.alternatives
    assert item.alternatives[0] >= datetime(2026, 10, 1, 7, 40, tzinfo=UTC)


def test_capacity_uses_peak_concurrency_not_total_overlaps() -> None:
    request = request_for(
        [uuid4()],
        mode=SchedulingMode.EXPLICIT,
        start=time(10),
        end=time(11),
        duration=60,
        buffer=0,
        parallelism=2,
    )
    slot_start = datetime(2026, 10, 1, 7, tzinfo=UTC)
    busy = [
        BusyInterval(
            start=slot_start,
            end=slot_start + timedelta(minutes=20),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
        BusyInterval(
            start=slot_start + timedelta(minutes=40),
            end=slot_start + timedelta(minutes=60),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
    ]

    item = SchedulingEngine().propose(request, policy(max_parallel=2), busy).items[0]
    assert item.schedulable


def test_explicit_window_never_escapes_requested_boundaries() -> None:
    request = request_for(
        [uuid4(), uuid4()],
        start=time(11),
        end=time(12, 10),
        duration=30,
        buffer=10,
    )
    proposal = SchedulingEngine().propose(
        request,
        policy(default_start=time(14), default_end=time(18)),
    )
    local_starts = [
        item.proposed_start.astimezone(ZoneInfo("Asia/Gaza"))
        for item in proposal.items
        if item.proposed_start is not None
    ]
    local_ends = [
        item.proposed_end.astimezone(ZoneInfo("Asia/Gaza"))
        for item in proposal.items
        if item.proposed_end is not None
    ]
    assert all(start.time() >= time(11) for start in local_starts)
    assert all(end.time() <= time(12, 10) for end in local_ends)


def test_exact_time_overrides_the_date_only_default_range() -> None:
    request = request_for(
        [uuid4()],
        mode=SchedulingMode.EXPLICIT,
        start=time(20),
        end=time(20, 30),
        duration=30,
        buffer=0,
    )

    item = SchedulingEngine().propose(
        request,
        policy(default_start=time(9), default_end=time(18)),
    ).items[0]

    assert item.proposed_start == datetime(2026, 10, 1, 17, tzinfo=UTC)


def test_company_working_hours_are_only_applied_when_explicitly_enabled() -> None:
    request = request_for([uuid4()], start=None, end=None, duration=30, buffer=0)
    hours = {3: DailyWorkingHours(start=time(12), end=time(13))}

    unconstrained = SchedulingEngine().propose(
        request,
        policy(working_hours=hours, enforce_working_hours=False),
    )
    constrained = SchedulingEngine().propose(
        request,
        policy(working_hours=hours, enforce_working_hours=True),
    )

    assert unconstrained.items[0].proposed_start == datetime(
        2026, 10, 1, 6, tzinfo=UTC
    )
    assert constrained.items[0].proposed_start == datetime(
        2026, 10, 1, 9, tzinfo=UTC
    )


def test_date_only_skips_busy_first_and_last_slots() -> None:
    day_start = datetime(2026, 10, 1, 6, tzinfo=UTC)  # 09:00 Asia/Gaza
    request = request_for(
        [uuid4(), uuid4()],
        start=None,
        end=None,
        duration=30,
        buffer=0,
    )
    busy = [
        BusyInterval(
            start=day_start,
            end=day_start + timedelta(minutes=30),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
        BusyInterval(
            start=day_start + timedelta(minutes=90),
            end=day_start + timedelta(minutes=120),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
    ]

    proposal = SchedulingEngine().propose(
        request,
        policy(
            increment=30,
            default_start=time(9),
            default_end=time(11),
        ),
        busy,
    )

    assert [item.proposed_start for item in proposal.items] == [
        day_start + timedelta(minutes=30),
        day_start + timedelta(minutes=60),
    ]


def test_date_only_reports_no_capacity_when_the_working_day_is_busy() -> None:
    day_start = datetime(2026, 10, 1, 6, tzinfo=UTC)
    proposal = SchedulingEngine().propose(
        request_for([uuid4()], start=None, end=None, duration=30, buffer=0),
        policy(
            increment=10,
            default_start=time(9),
            default_end=time(10),
        ),
        [
            BusyInterval(
                start=day_start,
                end=day_start + timedelta(hours=1),
                kind=BusyKind.CAPACITY,
                source="company_capacity",
            )
        ],
    )

    assert proposal.scheduled_candidates == 0
    assert proposal.unscheduled_candidates == 1
    assert proposal.reason == "insufficient_same_day_capacity"
    assert proposal.remaining_free_intervals == []


def test_date_only_slot_search_honors_increment_after_a_conflict() -> None:
    day_start = datetime(2026, 10, 1, 6, tzinfo=UTC)
    candidate = uuid4()
    proposal = SchedulingEngine().propose(
        request_for([candidate], start=None, end=None, duration=30, buffer=0),
        policy(
            increment=15,
            default_start=time(9),
            default_end=time(11),
        ),
        [
            BusyInterval(
                start=day_start,
                end=day_start + timedelta(minutes=31),
                kind=BusyKind.CANDIDATE,
                owner_id=candidate,
            )
        ],
    )

    assert proposal.items[0].proposed_start == day_start + timedelta(minutes=45)


@pytest.mark.parametrize(
    ("day", "expected_utc_hour"),
    [
        (date(2026, 3, 9), 13),  # first Monday after the US spring transition
        (date(2026, 11, 2), 14),  # first Monday after the US fall transition
    ],
)
def test_date_only_default_range_follows_iana_dst_offsets(
    day: date,
    expected_utc_hour: int,
) -> None:
    request = SchedulingRequest(
        company_id=uuid4(),
        job_id=uuid4(),
        candidate_ids=[uuid4()],
        timezone="America/New_York",
        start_date=day,
        end_date=day,
        duration_minutes=30,
        mode=SchedulingMode.AI_ASSISTED,
    )
    proposal = SchedulingEngine().propose(
        request,
        policy(
            increment=30,
            default_start=time(9),
            default_end=time(17),
        ),
    )

    assert proposal.items[0].proposed_start == datetime(
        day.year,
        day.month,
        day.day,
        expected_utc_hour,
        tzinfo=UTC,
    )


def test_date_only_google_and_interviewer_conflicts_are_enforced() -> None:
    interviewer = uuid4()
    day_start = datetime(2026, 10, 1, 6, tzinfo=UTC)
    proposal = SchedulingEngine().propose(
        request_for(
            [uuid4()],
            start=None,
            end=None,
            duration=30,
            buffer=0,
            interviewers=[interviewer],
        ),
        policy(
            increment=30,
            default_start=time(9),
            default_end=time(11),
        ),
        [
            BusyInterval(
                start=day_start,
                end=day_start + timedelta(minutes=30),
                kind=BusyKind.CALENDAR,
                owner_id=interviewer,
                source="google_calendar",
            ),
            BusyInterval(
                start=day_start + timedelta(minutes=30),
                end=day_start + timedelta(minutes=60),
                kind=BusyKind.INTERVIEWER,
                owner_id=interviewer,
                source="talentflow",
            ),
        ],
    )

    assert proposal.items[0].proposed_start == day_start + timedelta(minutes=60)


def test_required_date_only_acceptance_scenario() -> None:
    """09:00-17:00, two gaps, five candidates, 30 minutes, 10-minute buffer."""
    candidates = [uuid4() for _ in range(5)]
    local_zone = ZoneInfo("Asia/Gaza")
    local_day = date(2026, 10, 1)
    request = SchedulingRequest(
        company_id=uuid4(),
        job_id=uuid4(),
        candidate_ids=candidates,
        timezone="Asia/Gaza",
        start_date=local_day,
        end_date=local_day,
        duration_minutes=30,
        buffer_minutes=10,
        mode=SchedulingMode.AI_ASSISTED,
    )
    conflicts = [
        BusyInterval(
            start=datetime.combine(local_day, time(9, 30), tzinfo=local_zone).astimezone(UTC),
            end=datetime.combine(local_day, time(10, 30), tzinfo=local_zone).astimezone(UTC),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
        BusyInterval(
            start=datetime.combine(local_day, time(13), tzinfo=local_zone).astimezone(UTC),
            end=datetime.combine(local_day, time(14), tzinfo=local_zone).astimezone(UTC),
            kind=BusyKind.CAPACITY,
            source="company_capacity",
        ),
    ]

    proposal = SchedulingEngine().propose(
        request,
        policy(
            increment=10,
            default_start=time(9),
            default_end=time(17),
        ),
        conflicts,
    )

    assert proposal.scheduled_candidates == 5
    assert proposal.unscheduled_candidates == 0
    assert proposal.reason is None
    for item in proposal.items:
        assert item.proposed_start is not None
        assert item.proposed_end is not None
        assert item.proposed_start.astimezone(local_zone).date() == local_day
        assert time(9) <= item.proposed_start.astimezone(local_zone).time() < time(17)
        assert item.proposed_end.astimezone(local_zone).time() <= time(17)
        assert not any(
            item.proposed_start < conflict.end + timedelta(minutes=10)
            and conflict.start - timedelta(minutes=10) < item.proposed_end
            for conflict in conflicts
        )
