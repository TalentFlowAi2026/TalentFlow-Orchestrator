"""System-owned SMTP interview email transport, independent of company Google OAuth.

SMTP cannot reconcile a successful delivery after an unknown network failure, unlike Gmail
message search. To avoid automatic duplicates, ambiguous failures do not retry; an operator
must inspect the provider logs before redriving a failed message.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.providers.base import EmailMessage


class SMTPProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _deliver(self, mime: MimeMessage) -> None:
        host = self.settings.smtp_host
        port = self.settings.smtp_port
        timeout = self.settings.provider_timeout_seconds
        context = ssl.create_default_context()
        if self.settings.smtp_scheme == "smtps":
            connection = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            connection = smtplib.SMTP(host, port, timeout=timeout)
        with connection as client:
            client.ehlo()
            if self.settings.smtp_scheme == "smtp":
                client.starttls(context=context)
                client.ehlo()
            client.login(
                self.settings.smtp_username,
                self.settings.smtp_password.get_secret_value(),
            )
            client.send_message(mime)

    async def send(self, access_token: str, message: EmailMessage) -> str:
        del access_token  # SMTP never needs a Google token.
        if any("\r" in val or "\n" in val for val in (
            message.subject, str(message.to), str(message.from_email), message.logical_message_id
        )):
            raise ServiceError("email_header_invalid", retryable=False)
        if message.from_email.lower() != self.settings.smtp_from_address.lower():
            raise ServiceError("smtp_sender_mismatch", retryable=False)
        mime = MimeMessage()
        mime["From"] = formataddr(("TalentFlow AI", str(message.from_email)))
        mime["To"] = str(message.to)
        mime["Subject"] = message.subject
        mime["Message-ID"] = f"<{message.logical_message_id}>"
        mime.set_content(message.body)
        if message.html_body is not None:
            mime.add_alternative(message.html_body, subtype="html")
        if message.calendar_ics is not None:
            mime.add_attachment(
                message.calendar_ics.encode("utf-8"),
                maintype="text", subtype="calendar", filename="talentflow-interview.ics",
            )
        try:
            await asyncio.to_thread(self._deliver, mime)
        except smtplib.SMTPAuthenticationError as exc:
            raise ServiceError("smtp_authentication_failed", retryable=False) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise ServiceError("smtp_recipient_rejected", retryable=False) from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            # Delivery may already have happened. No automatic retry of uncertain sends.
            raise ServiceError("smtp_delivery_uncertain", retryable=False) from exc
        return message.logical_message_id

    async def aclose(self) -> None:
        return None
