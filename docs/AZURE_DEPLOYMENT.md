# Azure deployment

`azure/main.bicep` provisions a dedicated Container Apps environment, a user-assigned managed identity, and two new Container Apps from the same orchestrator image:

- `*-api` runs the FastAPI process with external ingress and `/health` plus `/ready` probes.
- `*-worker` runs only the orchestrator job consumer with no ingress.

These resources are separate from the TalentFlow Interview Engine Container App. They share only the documented PostgreSQL schema/API contracts.

Supply an immutable image digest in production. Configure `defaultInterviewDayStart` and `defaultInterviewDayEnd` as the local-time range used only for date-only requests; the defaults are `09:00` and `18:00`. Key Vault must always contain `orchestrator-supabase-db-url` and `orchestrator-data-encryption-key`. It must also contain `orchestrator-google-oauth-client-secret` when Calendar or Gmail is enabled, and `orchestrator-google-api-key` when Gemini intent parsing is enabled. The default JWKS authentication mode does not require a Supabase publishable key. The managed identity receives Key Vault Secrets User. Grant it `AcrPull` on the registry before deployment when the image is private.

Apply `202609230004_interview_cancelled_enum.sql` by itself first because PostgreSQL enum additions cannot be used safely in the same migration transaction. Then apply `202609230005_scheduling_orchestrator.sql`; apply the concurrent indexes in `202609230006_scheduling_indexes.sql` and the additive security alignment in `202609240007_scheduling_security_alignment.sql` statement-by-statement outside a transaction. Run `talentflow-orchestrator-doctor` against the deployed secret configuration before shifting traffic.
