# TalentFlow Scheduling Orchestrator

This is an independent FastAPI API and background-job service for interview proposals, deterministic slot allocation, secure invitations, Google OAuth, Calendar synchronization, and Gmail notifications.

It shares only the documented Supabase contract with the Interview Engine. It never imports `talentflow_worker`, creates LiveKit rooms, mints participant tokens, conducts interviews, or evaluates candidates. The API and job consumer can be released and scaled independently from every existing TalentFlow Python Worker Container App.

## Processes

- `talentflow-orchestrator-api` serves the versioned HTTP API, `/health`, and `/ready`.
- `talentflow-orchestrator-worker` consumes only `executor_service='scheduling_orchestrator'` queue rows and performs Google side effects.
- `talentflow-orchestrator-doctor` validates configuration and, unless `--no-database` is used, the live database contract.

The API records interviews, integration actions, and outbox jobs atomically. Provider failures do not roll back the canonical interview. The worker updates each integration action independently and retries only the failed action.

## Configuration

Copy `.env.example` to an untracked `.env`. Required for both processes:

- `SUPABASE_DB_URL`: dedicated login with membership in the `talentflow_orchestrator` capability role.
- `DATA_ENCRYPTION_KEY`: base64url-encoded 32-byte master key held in Key Vault.

The API additionally requires `SUPABASE_URL`; `SUPABASE_PUBLISHABLE_KEY` is required only when `SUPABASE_AUTH_MODE=user_endpoint`. Google client ID, secret, redirect URI, and encryption key are mandatory when Calendar or Gmail is enabled. `GOOGLE_API_KEY` enables Gemini intent parsing; scheduling itself remains deterministic when intent parsing is unavailable.

Date-only requests use the configurable application-level `DEFAULT_INTERVIEW_DAY_START` and `DEFAULT_INTERVIEW_DAY_END` local-time range (07:00–17:00 by default). Exact times and HR-provided windows take precedence. Company policy is read from the existing `companies.settings.scheduling` JSON object and supports `timezone`, `allow_parallel_ai_interviews`, `max_parallel_ai_interviews`, `default_duration_minutes`, `default_buffer_minutes`, `slot_increment_minutes`, and `allowed_weekdays`. Existing per-weekday `working_hours` can remain as an additional constraint only when `enforce_working_hours` is explicitly true; they are never required for date-only scheduling. Backend capacity is independently capped by `BACKEND_MAX_PARALLEL_AI_INTERVIEWS`.

The existing schema has no first-time application/intake domain. This migration backfills company-visible candidates from canonical interview history, and the assignment endpoint can reuse only an already visible candidate. A future applicant/invitation workflow must establish the first company/candidate relationship; the orchestrator deliberately cannot claim arbitrary global profile IDs.

## Database rollout

Migrations are review-only and are never applied by application startup:

1. Apply `202609230004_interview_cancelled_enum.sql` and commit it.
2. Apply `202609230005_scheduling_orchestrator.sql` as a trusted DBA.
3. Create a dedicated `LOGIN` role out of band, grant it membership in `talentflow_orchestrator`, and put only that login URL in Key Vault.
4. Apply each `CREATE INDEX CONCURRENTLY` statement from `202609230006_scheduling_indexes.sql` outside a transaction.
5. Apply `202609240007_scheduling_security_alignment.sql` statement-by-statement outside a transaction. It binds invitation tenant/interview/candidate identities and narrows the orchestrator's column privileges.
6. Run the doctor against staging, then exercise the authenticated API and gated Google tests before production rollout.

Do not rerun `supabase_bootstrap.sql`. Take a database backup and rehearse the migration on a staging copy first. The enum addition is intentionally separate because later constraints use the new value.

## Local verification

```powershell
uv sync --frozen
uv run ruff check .
uv run mypy src tests
uv run pytest -q
uv lock --check
uv run talentflow-orchestrator-doctor --role api
uv run talentflow-orchestrator-doctor --role background
```

Normal tests use mocked Google transports and do not send messages or create Calendar events. See `docs/GOOGLE_SETUP.md` for the explicit, manually gated real-provider procedure and `docs/AZURE_DEPLOYMENT.md` for the dedicated deployment boundary.
