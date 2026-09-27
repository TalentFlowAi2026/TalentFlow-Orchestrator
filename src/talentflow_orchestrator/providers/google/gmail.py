"""Gmail send adapter with deterministic RFC Message-ID reconciliation."""

from __future__ import annotations

import base64
from email.message import EmailMessage as MimeMessage

import httpx

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.providers.base import EmailMessage


class GmailProvider:
    _BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.provider_timeout_seconds)
        self._owns_client = client is None

    async def send(self, access_token: str, message: EmailMessage) -> str:
        if any("\r" in value or "\n" in value for value in (str(message.to), message.subject)):
            raise ServiceError("email_header_invalid", retryable=False)
        rfc_message_id = f"<{message.logical_message_id}>"
        existing = await self._request(
            "GET",
            self._BASE + "/messages",
            access_token,
            params={"q": f"rfc822msgid:{rfc_message_id}", "maxResults": 1},
        )
        messages = existing.json().get("messages", [])
        if messages:
            return str(messages[0]["id"])

        mime = MimeMessage()
        mime["From"] = str(message.from_email)
        mime["To"] = str(message.to)
        mime["Subject"] = message.subject
        mime["Message-ID"] = rfc_message_id
        mime.set_content(message.body)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).rstrip(b"=").decode()
        response = await self._request(
            "POST",
            self._BASE + "/messages/send",
            access_token,
            json={"raw": raw},
        )
        return str(response.json()["id"])

    async def _request(
        self,
        method: str,
        url: str,
        access_token: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        try:
            response = await self.client.request(
                method,
                url,
                headers={"Authorization": "Bearer " + access_token},
                params=params,
                json=json,
            )
            if response.status_code in {401, 403}:
                raise ServiceError("google_authorization_failed", retryable=False)
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", "60"))
                except ValueError:
                    retry_after = 60.0
                raise ServiceError(
                    "google_rate_limited",
                    retry_after=retry_after,
                )
            if 400 <= response.status_code < 500:
                raise ServiceError("gmail_request_rejected", retryable=False)
            response.raise_for_status()
            return response
        except ServiceError:
            raise
        except httpx.TimeoutException as exc:
            raise ServiceError("gmail_timeout") from exc
        except httpx.HTTPError as exc:
            raise ServiceError("gmail_unavailable") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
