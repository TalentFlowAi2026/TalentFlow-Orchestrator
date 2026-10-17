# System SMTP interview notifications (proposed, not deployed)

This change sends a candidate-facing HTML interview email at HR confirmation, without
Google OAuth or creating calendar events for a company manager. The candidate can use
**Add to Google Calendar** and save the pre-filled event. A `.ics` attachment is also
included. Reschedule/cancel mail continues to use the same SMTP transport so that
candidates are not left with outdated invitations. There is no one-hour reminder.

## Deployment order and safety

1. Leave SMTP disabled (`SMTP_ENABLED=false`) while validating the code and backups.
2. Review and apply `migrations/20261017_allow_system_smtp_email.sql` via Supabase SQL Editor;
   the existing `integration_actions.provider_account_id NOT NULL` and provider check otherwise
   prevent SMTP actions. **Do not apply until the operator approves production changes**.
3. Configure SMTP secrets **on the Orchestrator API and worker** in Azure Container Apps.
   Use the same working mail server configuration as Laravel; do not copy `.env` into the repo.
4. Deploy the API and worker from the same tested release, **both with SMTP_ENABLED=false**.
   Worker requires the mail secrets to send. API requires `SMTP_ENABLED=true` to create
   notifications when HR confirms; enable them together after a smoke test.
5. Run a controlled interview-confirmation test to a known candidate test inbox, check
   that exactly one `smtp` integration action and one orchestrator worker job is created,
   and that message delivery and calendar action work. Inspect failures before redrive.
6. Do not re-enable Laravel's interview notification command.

## Configuration (Orchestrator only)

`SMTP_ENABLED=true`
`SMTP_HOST=<working Laravel MAIL_HOST>`
`SMTP_PORT=<working Laravel MAIL_PORT>`
`SMTP_SCHEME=smtp` for STARTTLS (typically port 587), `smtps` for implicit TLS (typically 465)
`SMTP_USERNAME=<working Laravel MAIL_USERNAME>`
`SMTP_PASSWORD=<working Laravel MAIL_PASSWORD>` (Azure secret or Key Vault reference)
`SMTP_FROM_ADDRESS=<working Laravel MAIL_FROM_ADDRESS>`

Keep `GOOGLE_CALENDAR_ENABLED=false` and `GMAIL_ENABLED=false` for the simplified flow.
This avoids creating manager calendar events and requires no per-company Google account.

## Duplicate and failure handling

The original scheduling transaction creates one deterministic outbox action/job per
interview and schedule version. Normal repeated HR confirmation won't enqueue twice.
SMTP **does not support guaranteed exactly-once delivery** across process crashes:
if the server accepts a message but the process crashes before recording success,
a later worker lease reclamation can resend it. Transient/ambiguous SMTP errors are
therefore deliberately marked non-retryable. This significantly reduces duplicate risk
but does not eliminate a crash window. Review any uncertain delivery before manual retry.

## Uploaded project security

The supplied archive's `.env.example` contains values resembling real database credentials
and a data encryption key. Do not redistribute this file. Rotate any valid exposed secrets
and replace the example values with placeholders in your own source tree.
