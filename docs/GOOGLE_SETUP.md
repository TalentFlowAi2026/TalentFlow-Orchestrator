# Google Workspace setup

Create a Google Cloud OAuth web application and enable the Google Calendar API and Gmail API. Configure the exact redirect URI shown by `GOOGLE_OAUTH_REDIRECT_URI`.

The requested scopes are OpenID identity/email, Calendar event write, Calendar free/busy read, Gmail send, and Gmail read-only metadata access. Read-only Gmail access is required solely to search the deterministic RFC `Message-ID` before a retry; without that reconciliation Gmail offers no API idempotency key and a lost response could produce a duplicate. Production Google consent-screen verification may be required because Gmail and Calendar scopes are sensitive.

Store the client secret and the independent 32-byte base64url `DATA_ENCRYPTION_KEY` in Azure Key Vault. The encryption key protects refresh/access tokens and invitation tokens at rest; it is never exposed to browser clients. Changing `DATA_ENCRYPTION_KEY_VERSION` requires a controlled re-encryption migration before the old key is removed. The service deliberately fails closed when it encounters an older version and requires fresh Google authorization if a reconnect does not return a new refresh token.

OAuth state is one-time, expires after ten minutes, is bound to the company and initiating administrator, and uses PKCE. Revocation clears cached access-token ciphertext and marks the provider account revoked.

## Gated live verification

Normal tests never contact Google. Before enabling Google in production:

1. Create the OAuth web client, configure the exact HTTPS callback, enable Calendar and Gmail, and complete any required consent-screen verification.
2. Authorize a dedicated non-production Google account through `POST /v1/integrations/google/authorize` and the returned browser URL. Confirm the callback reports `connected`.
3. Obtain a short-lived access token for that same test account with every scope listed above. Do not save it to a file or commit it.
4. Choose a sender equal to the authorized Gmail account and a recipient mailbox whose owner has explicitly agreed to one test message.
5. In a temporary PowerShell session, set `RUN_LIVE_GOOGLE_TESTS=1`, `GOOGLE_LIVE_ACCESS_TOKEN`, `GOOGLE_LIVE_SENDER`, and `GOOGLE_LIVE_RECIPIENT`.
6. Run `uv run pytest -m live_google tests/integration/test_live_google.py -q`.
7. Verify the Calendar event was created, updated, and removed; verify exactly one email arrived; then clear the environment variables and revoke the test authorization.

The Calendar test cleans up its temporary event in `finally`. The Gmail test sends one real message and waits for Gmail search indexing before exercising retry reconciliation. Never run it against a production mailbox without explicit authorization.
