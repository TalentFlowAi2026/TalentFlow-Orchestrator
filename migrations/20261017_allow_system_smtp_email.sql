-- One-time Supabase SQL Editor migration. Back up the schema before applying.
-- System SMTP notifications have no per-company provider account.
-- Existing Google account foreign key stays in place for Google actions.
BEGIN;
ALTER TABLE public.integration_actions
    ALTER COLUMN provider_account_id DROP NOT NULL;
ALTER TABLE public.integration_actions
    DROP CONSTRAINT integration_actions_provider_check;
ALTER TABLE public.integration_actions
    ADD CONSTRAINT integration_actions_provider_check
    CHECK (provider IN ('google_calendar', 'gmail', 'smtp'));
ALTER TABLE public.integration_actions
    ADD CONSTRAINT integration_actions_account_mode_check
    CHECK (
        (provider = 'smtp' AND provider_account_id IS NULL)
        OR (provider IN ('google_calendar', 'gmail') AND provider_account_id IS NOT NULL)
    );
COMMIT;
