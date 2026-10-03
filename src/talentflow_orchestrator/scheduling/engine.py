"""Side-effect-free slot allocation. AI output never bypasses this engine."""



from __future__ import annotations



from collections import defaultdict

from collections.abc import Iterable, Iterator

from datetime import UTC, date, datetime, time, timedelta

from uuid import UUID

from zoneinfo import ZoneInfo



from talentflow_orchestrator.scheduling.models import (

    BusyInterval,

    BusyKind,

    FreeInterval,

    ProposalDraft,

    ProposalItemDraft,

    SchedulingConflict,

    SchedulingMode,

    SchedulingPolicy,

    SchedulingRequest,

)



_CONFLICT_MESSAGES = {

    "ambiguous_local_time": "The requested local time is ambiguous because of a DST transition.",

    "candidate_conflict": "The candidate already has a conflicting interview.",
    "candidate_job_same_day_conflict": (
        "The candidate already has an interview for this position on this date."
    ),

    "backend_capacity_exceeded": "The backend-wide parallel interview capacity is already in use.",

    "interviewer_conflict": "An assigned interviewer is unavailable.",

    "company_capacity_exceeded": "The company parallel interview capacity is already in use.",

    "nonexistent_local_time": "The requested local time does not exist because of a DST transition.",

    "outside_allowed_window": "No valid slot exists inside the requested scheduling window.",

}





def _overlaps(start: datetime, end: datetime, busy_start: datetime, busy_end: datetime) -> bool:

    return start < busy_end and busy_start < end





def _localize(local_value: datetime, timezone: ZoneInfo) -> tuple[datetime | None, str | None]:

    first = local_value.replace(tzinfo=timezone, fold=0)

    second = local_value.replace(tzinfo=timezone, fold=1)

    first_roundtrip = first.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)

    second_roundtrip = second.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)

    first_valid = first_roundtrip == local_value

    second_valid = second_roundtrip == local_value

    if not first_valid and not second_valid:

        return None, "nonexistent_local_time"

    if first_valid and second_valid and first.utcoffset() != second.utcoffset():

        return None, "ambiguous_local_time"

    return first if first_valid else second, None





class SchedulingEngine:

    def propose(

        self,

        request: SchedulingRequest,

        policy: SchedulingPolicy,

        busy_intervals: Iterable[BusyInterval] = (),

        *,

        max_horizon_days: int = 31,

        now: datetime | None = None,

    ) -> ProposalDraft:

        scheduling_now = now or datetime.now(UTC)

        if scheduling_now.tzinfo is None:

            raise ValueError("now must be timezone-aware")

        scheduling_now = scheduling_now.astimezone(UTC)



        if (request.end_date - request.start_date).days + 1 > max_horizon_days:

            raise ValueError("scheduling_horizon_exceeded")



        company_parallelism = policy.max_parallel_ai_interviews

        if request.parallelism_limit is not None:

            company_parallelism = min(request.parallelism_limit, company_parallelism)

        backend_parallelism = policy.backend_max_parallel_ai_interviews

        effective_parallelism = min(company_parallelism, backend_parallelism)

        busy = list(busy_intervals)

        allocated: list[BusyInterval] = []

        items: list[ProposalItemDraft] = []



        for candidate_id in request.candidate_ids:

            chosen: tuple[datetime, datetime] | None = None

            alternatives: list[datetime] = []

            rejected: dict[str, set[UUID]] = defaultdict(set)



            if request.mode == SchedulingMode.AUTO_BALANCED:

                ranked_slots: list[tuple[int, int, datetime, datetime]] = []

                current_busy = [*busy, *allocated]

                for slot_start, slot_end, time_error in self._candidate_slots(request, policy, now=scheduling_now):

                    if time_error is not None:

                        rejected[time_error]

                        continue

                    if slot_start is None or slot_end is None:

                        continue



                    hard_conflicts = self._person_conflicts(

                        request=request,

                        candidate_id=candidate_id,

                        slot_start=slot_start,

                        slot_end=slot_end,

                        busy=current_busy,

                    )

                    if hard_conflicts:

                        for code, reference_id in hard_conflicts:

                            if reference_id is not None:

                                rejected[code].add(reference_id)

                            else:

                                rejected[code]

                        continue



                    company_load, backend_load = self._capacity_load(

                        request=request,

                        slot_start=slot_start,

                        slot_end=slot_end,

                        busy=current_busy,

                    )

                    ranked_slots.append((company_load, backend_load, slot_start, slot_end))



                if ranked_slots:

                    ranked_slots.sort(key=lambda item: (item[0], item[1], item[2]))

                    _, _, start, end = ranked_slots[0]

                    chosen = (start, end)

                    alternatives = [item[2] for item in ranked_slots[1:4]]

            else:

                for slot_start, slot_end, time_error in self._candidate_slots(request, policy, now=scheduling_now):

                    if time_error is not None:

                        rejected[time_error]

                        continue

                    if slot_start is None or slot_end is None:

                        continue

                    conflicts = self._conflicts(

                        request=request,

                        candidate_id=candidate_id,

                        slot_start=slot_start,

                        slot_end=slot_end,

                        company_parallelism=company_parallelism,

                        backend_parallelism=backend_parallelism,

                        busy=[*busy, *allocated],

                    )

                    if conflicts:

                        for code, reference_id in conflicts:

                            if reference_id is not None:

                                rejected[code].add(reference_id)

                            else:

                                rejected[code]

                        continue



                    if chosen is None:

                        chosen = (slot_start, slot_end)

                        if request.mode == SchedulingMode.EXPLICIT:

                            break

                    elif len(alternatives) < 3:

                        alternatives.append(slot_start)

                    if len(alternatives) == 3:

                        break



            if chosen is None:

                if request.mode == SchedulingMode.EXPLICIT:

                    alternatives = self._exact_alternatives(

                        request=request,

                        policy=policy,

                        candidate_id=candidate_id,

                        company_parallelism=company_parallelism,

                        backend_parallelism=backend_parallelism,

                        busy=[*busy, *allocated],

                        now=scheduling_now,

                    )

                codes = sorted(rejected or {"outside_allowed_window": set()})

                items.append(

                    ProposalItemDraft(

                        candidate_id=candidate_id,

                        job_id=request.job_id,

                        timezone=request.timezone,

                        language=request.language,

                        interviewer_ids=request.interviewer_ids,

                        conflicts=[

                            SchedulingConflict(

                                code=code,

                                message=_CONFLICT_MESSAGES[code],

                                conflicting_reference_ids=sorted(rejected[code], key=str),

                            )

                            for code in codes

                        ],

                        alternatives=alternatives,

                    )

                )

                continue



            start, end = chosen

            allocated.append(

                BusyInterval(

                    start=start,

                    end=end,

                    kind=BusyKind.CAPACITY,

                    source="company_capacity",

                    buffer_minutes=request.buffer_minutes,

                )

            )

            allocated.append(

                BusyInterval(

                    start=start,

                    end=end,

                    kind=BusyKind.CAPACITY,

                    source="backend_capacity",

                    buffer_minutes=request.buffer_minutes,

                )

            )

            allocated.append(

                BusyInterval(

                    start=start,

                    end=end,

                    kind=BusyKind.CANDIDATE,

                    owner_id=candidate_id,

                    source="proposal",

                    buffer_minutes=request.buffer_minutes,

                )

            )

            for interviewer_id in request.interviewer_ids:

                allocated.append(

                    BusyInterval(

                        start=start,

                        end=end,

                        kind=BusyKind.INTERVIEWER,

                        owner_id=interviewer_id,

                        source="proposal",

                        buffer_minutes=request.buffer_minutes,

                    )

                )

            items.append(

                ProposalItemDraft(

                    candidate_id=candidate_id,

                    job_id=request.job_id,

                    proposed_start=start,

                    proposed_end=end,

                    timezone=request.timezone,

                    language=request.language,

                    interviewer_ids=request.interviewer_ids,

                    alternatives=alternatives,

                )

            )



        scheduled = sum(item.schedulable for item in items)

        unscheduled = len(items) - scheduled

        reason: str | None = None

        if unscheduled:

            if request.mode == SchedulingMode.EXPLICIT:

                reason = "schedule_conflict"

            elif request.start_date == request.end_date:

                reason = "insufficient_same_day_capacity"

            else:

                reason = "insufficient_capacity"

        return ProposalDraft(

            company_id=request.company_id,

            job_id=request.job_id,

            timezone=request.timezone,

            effective_parallelism=effective_parallelism,

            items=items,

            scheduled_candidates=scheduled,

            unscheduled_candidates=unscheduled,

            reason=reason,

            remaining_free_intervals=self._remaining_free_intervals(

                request=request,

                policy=policy,

                candidate_id=request.candidate_ids[-1],

                company_parallelism=company_parallelism,

                backend_parallelism=backend_parallelism,

                busy=[*busy, *allocated],

                now=scheduling_now,

            ),

        )



    def _candidate_slots(

        self,

        request: SchedulingRequest,

        policy: SchedulingPolicy,

        *,

        now: datetime,

    ) -> Iterator[tuple[datetime | None, datetime | None, str | None]]:

        timezone = ZoneInfo(request.timezone)

        local_now = now.astimezone(timezone)

        local_today = local_now.date()

        minimum_start_utc = (

            local_now + timedelta(minutes=policy.minimum_lead_minutes)

        ).astimezone(UTC)



        current_day = request.start_date

        duration = timedelta(minutes=request.duration_minutes)

        increment = timedelta(minutes=policy.slot_increment_minutes)



        while current_day <= request.end_date:

            if current_day < local_today:

                current_day += timedelta(days=1)

                continue

            if current_day.weekday() not in policy.allowed_weekdays:

                current_day += timedelta(days=1)

                continue

            local_window = self._effective_local_window(current_day, request, policy)

            if local_window is None:

                current_day += timedelta(days=1)

                continue

            window_start, window_end = local_window

            cursor = datetime.combine(current_day, window_start)

            local_window_end = datetime.combine(current_day, window_end)

            while cursor + duration <= local_window_end:

                aware_start, start_error = _localize(cursor, timezone)

                aware_end, end_error = _localize(cursor + duration, timezone)

                time_error = start_error or end_error

                if time_error is not None:

                    yield None, None, time_error

                elif aware_start is not None and aware_end is not None:

                    slot_start_utc = aware_start.astimezone(UTC)

                    slot_end_utc = aware_end.astimezone(UTC)



                    # Same-day scheduling must never select a slot in the past.

                    # Keep at least policy.minimum_lead_minutes between "now"

                    # and the interview start. Because slots are generated on

                    # the configured increment, the next valid slot is

                    # naturally rounded up to that grid.

                    if current_day == local_today and slot_start_utc < minimum_start_utc:

                        pass

                    else:

                        yield slot_start_utc, slot_end_utc, None

                if request.mode == SchedulingMode.EXPLICIT:

                    break

                cursor += increment

            current_day += timedelta(days=1)



    @staticmethod

    def _effective_local_window(

        current_day: date,

        request: SchedulingRequest,

        policy: SchedulingPolicy,

    ) -> tuple[time, time] | None:

        if request.window_start is None or request.window_end is None:

            start = policy.default_interview_day_start

            end = policy.default_interview_day_end

        else:

            start = request.window_start

            end = request.window_end

        if policy.enforce_working_hours:

            configured = policy.working_hours.get(current_day.weekday())

            if configured is not None:

                start = max(start, configured.start)

                end = min(end, configured.end)

        return (start, end) if start < end else None



    def _exact_alternatives(

        self,

        *,

        request: SchedulingRequest,

        policy: SchedulingPolicy,

        candidate_id: UUID,

        company_parallelism: int,

        backend_parallelism: int,

        busy: list[BusyInterval],

        now: datetime,

    ) -> list[datetime]:

        if request.window_start is None:

            return []

        alternative_request = request.model_copy(

            update={

                "window_start": policy.default_interview_day_start,

                "window_end": policy.default_interview_day_end,

                "mode": SchedulingMode.AI_ASSISTED,

            }

        )

        requested_start = datetime.combine(request.start_date, request.window_start).replace(

            tzinfo=ZoneInfo(request.timezone)

        ).astimezone(UTC)

        valid: list[datetime] = []

        for slot_start, slot_end, time_error in self._candidate_slots(

            alternative_request,

            policy,

            now=now,

        ):

            if time_error or slot_start is None or slot_end is None:

                continue

            if slot_start == requested_start:

                continue

            if not self._conflicts(

                request=request,

                candidate_id=candidate_id,

                slot_start=slot_start,

                slot_end=slot_end,

                company_parallelism=company_parallelism,

                backend_parallelism=backend_parallelism,

                busy=busy,

            ):

                valid.append(slot_start)

        after = [slot for slot in valid if slot > requested_start]

        before = [slot for slot in valid if slot < requested_start]

        return [*after, *before][:3]



    def _remaining_free_intervals(

        self,

        *,

        request: SchedulingRequest,

        policy: SchedulingPolicy,

        candidate_id: UUID,

        company_parallelism: int,

        backend_parallelism: int,

        busy: list[BusyInterval],

        now: datetime,

    ) -> list[FreeInterval]:

        search_request = request.model_copy(update={"mode": SchedulingMode.AI_ASSISTED})

        free: list[FreeInterval] = []

        slots = self._candidate_slots(

            search_request,

            policy,

            now=now,

        )

        for slot_start, slot_end, time_error in slots:

            if time_error or slot_start is None or slot_end is None:

                continue

            if self._conflicts(

                request=request,

                candidate_id=candidate_id,

                slot_start=slot_start,

                slot_end=slot_end,

                company_parallelism=company_parallelism,

                backend_parallelism=backend_parallelism,

                busy=busy,

            ):

                continue

            if free and slot_start <= free[-1].end:

                free[-1] = FreeInterval(

                    start=free[-1].start,

                    end=max(free[-1].end, slot_end),

                )

            else:

                free.append(FreeInterval(start=slot_start, end=slot_end))

        return free



    def _person_conflicts(

        self,

        *,

        request: SchedulingRequest,

        candidate_id: UUID,

        slot_start: datetime,

        slot_end: datetime,

        busy: list[BusyInterval],

    ) -> list[tuple[str, UUID | None]]:

        """Hard conflicts that can never be solved by adding parallel capacity."""

        conflicts: list[tuple[str, UUID | None]] = []

        for interval in busy:

            if interval.kind == BusyKind.CANDIDATE and interval.owner_id == candidate_id:

                if self._buffered_overlap(request, slot_start, slot_end, interval):

                    conflicts.append(("candidate_conflict", interval.reference_id))

            elif (
                interval.kind == BusyKind.CANDIDATE_JOB_DAY
                and interval.owner_id == candidate_id
            ):

                # This interval represents the candidate's entire local scheduling day
                # for the same company + job. Do not apply interview buffers here,
                # otherwise a buffer could incorrectly spill into an adjacent day.
                if _overlaps(slot_start, slot_end, interval.start, interval.end):

                    conflicts.append(
                        ("candidate_job_same_day_conflict", interval.reference_id)
                    )

            elif interval.kind in {BusyKind.INTERVIEWER, BusyKind.CALENDAR}:

                if interval.owner_id in request.interviewer_ids and self._buffered_overlap(

                    request, slot_start, slot_end, interval

                ):

                    conflicts.append(("interviewer_conflict", interval.reference_id))

        return conflicts



    def _capacity_load(

        self,

        *,

        request: SchedulingRequest,

        slot_start: datetime,

        slot_end: datetime,

        busy: list[BusyInterval],

    ) -> tuple[int, int]:

        """Return current company/backend overlap load for soft load-balanced scheduling."""

        company_capacity: list[BusyInterval] = []

        backend_capacity: list[BusyInterval] = []

        for interval in busy:

            if interval.kind != BusyKind.CAPACITY:

                continue

            if interval.source in {"company_capacity", "talentflow"}:

                company_capacity.append(interval)

            if interval.source in {"backend_capacity", "talentflow"}:

                backend_capacity.append(interval)

        return (

            self._peak_capacity(request, slot_start, slot_end, company_capacity),

            self._peak_capacity(request, slot_start, slot_end, backend_capacity),

        )



    def _conflicts(

        self,

        *,

        request: SchedulingRequest,

        candidate_id: UUID,

        slot_start: datetime,

        slot_end: datetime,

        company_parallelism: int,

        backend_parallelism: int,

        busy: list[BusyInterval],

    ) -> list[tuple[str, UUID | None]]:

        company_capacity: list[BusyInterval] = []

        backend_capacity: list[BusyInterval] = []

        conflicts: list[tuple[str, UUID | None]] = []



        for interval in busy:

            if interval.kind == BusyKind.CAPACITY:

                if interval.source in {"company_capacity", "talentflow"}:

                    company_capacity.append(interval)

                if interval.source in {"backend_capacity", "talentflow"}:

                    backend_capacity.append(interval)

            elif interval.kind == BusyKind.CANDIDATE and interval.owner_id == candidate_id:

                if self._buffered_overlap(request, slot_start, slot_end, interval):

                    conflicts.append(("candidate_conflict", interval.reference_id))

            elif (
                interval.kind == BusyKind.CANDIDATE_JOB_DAY
                and interval.owner_id == candidate_id
            ):

                if _overlaps(slot_start, slot_end, interval.start, interval.end):

                    conflicts.append(
                        ("candidate_job_same_day_conflict", interval.reference_id)
                    )

            elif interval.kind in {BusyKind.INTERVIEWER, BusyKind.CALENDAR}:

                if interval.owner_id in request.interviewer_ids and self._buffered_overlap(

                    request, slot_start, slot_end, interval

                ):

                    conflicts.append(("interviewer_conflict", interval.reference_id))



        if self._peak_capacity(

            request, slot_start, slot_end, company_capacity

        ) >= company_parallelism:

            conflicts.append(("company_capacity_exceeded", None))

        if self._peak_capacity(

            request, slot_start, slot_end, backend_capacity

        ) >= backend_parallelism:

            conflicts.append(("backend_capacity_exceeded", None))

        return conflicts



    @staticmethod

    def _buffered_overlap(

        request: SchedulingRequest,

        slot_start: datetime,

        slot_end: datetime,

        interval: BusyInterval,

    ) -> bool:

        gap = timedelta(minutes=max(request.buffer_minutes, interval.buffer_minutes))

        return _overlaps(

            slot_start,

            slot_end,

            interval.start - gap,

            interval.end + gap,

        )



    @staticmethod

    def _peak_capacity(

        request: SchedulingRequest,

        slot_start: datetime,

        slot_end: datetime,

        intervals: list[BusyInterval],

    ) -> int:

        events: list[tuple[datetime, int]] = []

        for interval in intervals:

            gap = timedelta(minutes=max(request.buffer_minutes, interval.buffer_minutes))

            start = max(slot_start, interval.start - gap)

            end = min(slot_end, interval.end + gap)

            if start < end:

                events.append((start, 1))

                events.append((end, -1))

        active = 0

        peak = 0

        for _, delta in sorted(events, key=lambda event: (event[0], event[1])):

            active += delta

            peak = max(peak, active)

        return peak
