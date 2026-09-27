# Scheduling Orchestrator API contract

All `/v1` endpoints except the Google OAuth callback require a Supabase end-user bearer token. Scheduling reads and mutations require an active `company_admin` or `interviewer` membership; Google account administration remains `company_admin` only. Proposal creation, proposal confirmation, reschedule, and cancellation calls require a UUID `Idempotency-Key` header. Candidate assignment is naturally idempotent through its tenant/job/candidate key. Responses include `X-Correlation-ID` and are `Cache-Control: no-store`.

The service never accepts tenant authority from JWT custom metadata. `company_id` is checked against `company_members` in PostgreSQL for every request.

## Scheduling

- `POST /v1/jobs/{job_id}/candidates` idempotently associates company-visible candidate profiles with a non-archived position. Visibility must already exist through that company's interview/job history; arbitrary global profile IDs are rejected without revealing whether they exist.
- `POST /v1/scheduling/availability` validates a `SchedulingRequest` and returns a non-persisted deterministic proposal.
- `POST /v1/scheduling/proposals` validates and persists a draft proposal.
- `GET /v1/scheduling/proposals/{proposal_id}?company_id=...` returns one tenant-scoped proposal.
- `POST /v1/scheduling/proposals/{proposal_id}/confirm?company_id=...` revalidates availability inside a transaction, creates canonical `interviews` rows, creates encrypted invitation tokens, and enqueues provider outbox jobs.
- `POST /v1/interviews/{interview_id}/reschedule/availability` previews date-only, explicit-window, or exact reschedule availability without mutation or provider side effects. The interview being moved is excluded from TalentFlow conflicts and its exact current Calendar interval is excluded from FreeBusy results.
- `POST /v1/interviews/{interview_id}/reschedule` uses optimistic schedule versions and revalidates all conflicts.
- `POST /v1/interviews/{interview_id}/cancel` transitions only a scheduled interview and revokes its invitation.
- `GET /v1/interviews/{interview_id}/integration-status?company_id=...` returns the latest authorized Calendar/email action state, safe error code, attempt count, and update time.
- `GET /v1/invitations/{opaque_token}` resolves an unexpired invitation only for the authenticated candidate.

`SchedulingRequest` uses IANA timezone names and local date/time windows. Explicit mode accepts exactly one candidate and a window exactly equal to the requested duration. Bulk/AI-assisted mode proposes deterministic first-fit slots; Gemini never chooses database records or performs mutations.

When both `window_start` and `window_end` are omitted, each requested local date is searched inside the application-level `DEFAULT_INTERVIEW_DAY_START` to `DEFAULT_INTERVIEW_DAY_END` range in the request's validated IANA timezone. The defaults are configurable and are not company working hours. Proposal responses include `scheduled_candidates`, `unscheduled_candidates`, `reason`, and `remaining_free_intervals`. A partial proposal can be confirmed and creates interviews only for its schedulable items.

Scheduling range precedence is exact time, then an HR-provided window, then the configurable date-only default range. Company policy supports `timezone`, `allowed_weekdays`, `default_duration_minutes`, `default_buffer_minutes`, `slot_increment_minutes`, `allow_parallel_ai_interviews`, and `max_parallel_ai_interviews`. Existing per-weekday `working_hours` are ignored unless `enforce_working_hours` is explicitly enabled, in which case they are an additional intersection constraint. Backend capacity is an independent upper bound.

Invitation resolution is deliberately candidate-scoped and returns `scheduled_start`, authoritative `server_time`, `timezone`, `duration_minutes`, `status`, `start_allowed`, and `seconds_until_start`. Invalid, expired, revoked, wrong-account, and mismatched interview/candidate bindings do not disclose invitation details.

## Intent and integrations

- `POST /v1/orchestrator/interpret` returns strict structured intent plus tenant-scoped entity matches or clarification fields.
- `POST /v1/integrations/google/authorize` creates a one-time encrypted OAuth state with PKCE.
- `GET /v1/integrations/google/callback` consumes the one-time state and stores encrypted tokens.
- `DELETE /v1/integrations/google/accounts/{account_id}?company_id=...` revokes and disconnects an account.

## Errors

Errors use this stable envelope:

```json
{
  "error": {
    "code": "schedule_conflict",
    "message": "schedule conflict",
    "retryable": false,
    "correlation_id": "00000000-0000-0000-0000-000000000000"
  }
}
```

Provider calls never run in API request transactions. The API writes `integration_actions` and orchestrator-partitioned `worker_jobs`; the separate background process performs the external side effects.
