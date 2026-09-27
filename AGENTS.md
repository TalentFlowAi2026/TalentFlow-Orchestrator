# TalentFlow Scheduling Orchestrator

These instructions extend the repository-root `AGENTS.md`.

- This service is independently deployable and must never import `talentflow_worker` internals.
- Supabase tables and documented HTTP contracts are the only integration boundary with the Interview Engine.
- The service owns scheduling proposals, confirmation, Calendar/Gmail operations, OAuth, and their background jobs.
- It must never create LiveKit rooms, issue LiveKit tokens, or conduct/evaluate interviews.
- Provider calls are timeout-bounded, retry-safe, observable, and executed by this service's background process.
- All tenant authorization is revalidated server-side because the service database role bypasses RLS.
