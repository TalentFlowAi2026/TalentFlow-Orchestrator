"""Google OAuth 2.0 authorization-code/refresh/revocation adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.providers.base import OAuthTokens

IDENTITY_SCOPES = (
    "openid",
    "email",
)
CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    # Gmail has no request idempotency key. Metadata read is required to
    # reconcile the deterministic RFC Message-ID before a retry sends again.
    "https://www.googleapis.com/auth/gmail.readonly",
)


class GoogleOAuthProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.provider_timeout_seconds)
        self._owns_client = client is None

    @property
    def scopes(self) -> tuple[str, ...]:
        result = list(IDENTITY_SCOPES)
        if self.settings.google_calendar_enabled:
            result.extend(CALENDAR_SCOPES)
        if self.settings.gmail_enabled:
            result.extend(GMAIL_SCOPES)
        return tuple(result)

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self.settings.google_oauth_client_id,
                "redirect_uri": self.settings.google_oauth_redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.scopes),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return "https://accounts.google.com/o/oauth2/v2/auth?" + query

    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        try:
            response = await self.client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self.settings.google_oauth_client_id,
                    "client_secret": self.settings.google_oauth_client_secret.get_secret_value(),
                    "code": code,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.settings.google_oauth_redirect_uri,
                },
            )
            if response.status_code in {400, 401}:
                raise ServiceError("oauth_code_invalid", status=400, retryable=False)
            response.raise_for_status()
            document = response.json()
            access_token = str(document["access_token"])
            userinfo = await self.client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": "Bearer " + access_token},
            )
            userinfo.raise_for_status()
            identity = userinfo.json()
            granted = str(document.get("scope") or " ".join(self.scopes)).split()
            if not set(self.scopes).issubset(granted):
                raise ServiceError("oauth_scopes_missing", status=422, retryable=False)
            return OAuthTokens(
                access_token=access_token,
                refresh_token=document.get("refresh_token"),
                expires_at=datetime.now(UTC) + timedelta(seconds=int(document["expires_in"])),
                scopes=granted,
                provider_subject=str(identity["sub"]),
                account_email=str(identity["email"]),
            )
        except ServiceError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ServiceError("oauth_exchange_failed") from exc

    async def refresh(self, refresh_token: str) -> tuple[str, datetime]:
        try:
            response = await self.client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self.settings.google_oauth_client_id,
                    "client_secret": self.settings.google_oauth_client_secret.get_secret_value(),
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code in {400, 401}:
                raise ServiceError("oauth_refresh_revoked", retryable=False)
            if response.status_code == 429:
                raise ServiceError("google_rate_limited", retry_after=60)
            response.raise_for_status()
            document = response.json()
            return (
                str(document["access_token"]),
                datetime.now(UTC) + timedelta(seconds=int(document["expires_in"])),
            )
        except ServiceError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ServiceError("oauth_refresh_failed") from exc

    async def revoke(self, access_or_refresh_token: str) -> None:
        try:
            response = await self.client.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": access_or_refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code not in {200, 400}:
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceError("oauth_revocation_failed") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
