"""Google Calendar adapter with deterministic event identifiers."""

from __future__ import annotations

from datetime import datetime

import httpx

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.providers.base import CalendarBusy, CalendarEvent


def deterministic_event_id(event: CalendarEvent) -> str:
    return f"tf{event.interview_id.hex}v{event.schedule_version}"


class GoogleCalendarProvider:
    _BASE = "https://www.googleapis.com/calendar/v3"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=settings.provider_timeout_seconds)
        self._owns_client = client is None

    async def free_busy(
        self,
        access_token: str,
        *,
        time_min: datetime,
        time_max: datetime,
        calendar_ids: list[str],
    ) -> dict[str, list[CalendarBusy]]:
        response = await self._request(
            "POST",
            self._BASE + "/freeBusy",
            access_token,
            json={
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "items": [{"id": item} for item in calendar_ids],
            },
        )
        try:
            document = response.json()
            result: dict[str, list[CalendarBusy]] = {}
            for calendar_id, details in document.get("calendars", {}).items():
                if details.get("errors"):
                    raise ServiceError("calendar_availability_unavailable")
                result[str(calendar_id)] = [
                    CalendarBusy(
                        start=datetime.fromisoformat(item["start"].replace("Z", "+00:00")),
                        end=datetime.fromisoformat(item["end"].replace("Z", "+00:00")),
                    )
                    for item in details.get("busy", [])
                ]
            if set(result) != set(calendar_ids):
                raise ServiceError("calendar_availability_incomplete")
            return result
        except ServiceError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ServiceError("calendar_response_invalid", retryable=False) from exc

    async def create_event(self, access_token: str, event: CalendarEvent) -> str:
        event_id = deterministic_event_id(event)
        response = await self._request(
            "POST",
            self._BASE + "/calendars/primary/events",
            access_token,
            params={"sendUpdates": "all"},
            json=self._event_body(event, event_id),
            accepted={200, 201, 409},
        )
        if response.status_code == 409:
            existing = await self._request(
                "GET",
                self._BASE + f"/calendars/primary/events/{event_id}",
                access_token,
            )
            document = existing.json()
            private = document.get("extendedProperties", {}).get("private", {})
            if (
                private.get("talentflowInterviewId") != str(event.interview_id)
                or private.get("talentflowScheduleVersion") != str(event.schedule_version)
            ):
                raise ServiceError("calendar_idempotency_conflict", retryable=False)
            return str(document["id"])
        return str(response.json()["id"])

    async def update_event(
        self, access_token: str, event_id: str, event: CalendarEvent
    ) -> str:
        response = await self._request(
            "PUT",
            self._BASE + f"/calendars/primary/events/{event_id}",
            access_token,
            params={"sendUpdates": "all"},
            json=self._event_body(event, event_id),
            accepted={200, 201, 404},
        )
        if response.status_code == 404:
            return await self.create_event(access_token, event)
        return str(response.json()["id"])

    async def cancel_event(self, access_token: str, event_id: str) -> None:
        await self._request(
            "DELETE",
            self._BASE + f"/calendars/primary/events/{event_id}",
            access_token,
            params={"sendUpdates": "all"},
            accepted={200, 204, 404},
        )

    def _event_body(self, event: CalendarEvent, event_id: str) -> dict[str, object]:
        return {
            "id": event_id,
            "summary": event.title,
            "description": event.description,
            "start": {"dateTime": event.start.isoformat(), "timeZone": event.timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": event.timezone},
            "attendees": [{"email": str(email)} for email in event.attendee_emails],
            "extendedProperties": {
                "private": {
                    "talentflowInterviewId": str(event.interview_id),
                    "talentflowScheduleVersion": str(event.schedule_version),
                }
            },
        }

    async def _request(
        self,
        method: str,
        url: str,
        access_token: str,
        *,
        accepted: set[int] | None = None,
        params: dict[str, str] | None = None,
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
            if accepted and response.status_code in accepted:
                return response
            if response.status_code in {401, 403}:
                raise ServiceError("google_authorization_failed", retryable=False)
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", "60"))
                except ValueError:
                    retry_after = 60.0
                raise ServiceError("google_rate_limited", retry_after=retry_after)
            if 400 <= response.status_code < 500:
                raise ServiceError(
                    "google_calendar_request_rejected", retryable=False
                )
            response.raise_for_status()
            return response
        except ServiceError:
            raise
        except httpx.TimeoutException as exc:
            raise ServiceError("google_calendar_timeout") from exc
        except httpx.HTTPError as exc:
            raise ServiceError("google_calendar_unavailable") from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
