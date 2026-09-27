import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from talentflow_orchestrator.api.app import create_app
from talentflow_orchestrator.api.schemas import RescheduleAvailabilityRequest
from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.providers.base import CalendarEvent, EmailMessage
from talentflow_orchestrator.providers.google.calendar import deterministic_event_id
from talentflow_orchestrator.scheduling import SchedulingMode
from talentflow_orchestrator.security.auth import SupabaseAuth
from talentflow_orchestrator.security.crypto import SecretCipher, new_opaque_token, token_digest


def settings_with_key(**values: object) -> Settings:
    key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    return Settings.model_validate({"data_encryption_key": key, **values})


def test_encrypted_values_are_context_bound_and_tokens_are_stored_as_digests() -> None:
    cipher = SecretCipher(settings_with_key(), "invitation")
    token = new_opaque_token()
    interview_id = uuid4()
    encrypted = cipher.encrypt(token, context=interview_id.bytes)
    assert token.encode() not in encrypted.ciphertext
    assert cipher.decrypt(encrypted, context=interview_id.bytes) == token
    assert token_digest(token) != token.encode()
    with pytest.raises(ServiceError, match="encrypted_value_invalid"):
        cipher.decrypt(encrypted, context=uuid4().bytes)


def test_provider_ids_and_email_headers_are_validated() -> None:
    event = CalendarEvent(
        interview_id=uuid4(),
        schedule_version=3,
        title="Interview",
        description="",
        start=datetime(2026, 10, 1, 7, tzinfo=UTC),
        end=datetime(2026, 10, 1, 7, 30, tzinfo=UTC),
        timezone="Asia/Gaza",
        attendee_emails=["candidate@example.com"],
    )
    assert deterministic_event_id(event) == f"tf{event.interview_id.hex}v3"
    with pytest.raises(ValidationError):
        EmailMessage(
            logical_message_id="message",
            from_email="hr@example.com",
            to="not-an-email",
            subject="Interview",
            body="Body",
        )


@pytest.mark.asyncio
async def test_health_endpoint_is_process_only_and_adds_safety_headers() -> None:
    app = create_app(settings_with_key(environment="test"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "talentflow-orchestrator"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-correlation-id"]


@pytest.mark.asyncio
async def test_chunked_request_body_limit_cannot_be_bypassed() -> None:
    app = create_app(
        settings_with_key(environment="test", max_request_bytes=4096)
    )

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 3000
        yield b"y" * 3000

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/unknown", content=chunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.headers["x-correlation-id"]


def test_python_package_has_no_interview_engine_imports() -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "talentflow_orchestrator"
    offenders = [
        path
        for path in package.rglob("*.py")
        if "talentflow_worker" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_settings_reject_invalid_role_pool_and_google_redirect_configuration() -> None:
    with pytest.raises(ValidationError, match="pool minimum"):
        settings_with_key(database_pool_min=5, api_database_pool_max=4)
    with pytest.raises(ValidationError, match="GOOGLE_OAUTH_REDIRECT_URI"):
        settings_with_key(
            google_calendar_enabled=True,
            google_oauth_client_id="client-id",
            google_oauth_client_secret="client-secret",
            google_oauth_redirect_uri="not-a-url",
        )


def test_settings_reject_non_base64_encryption_key() -> None:
    with pytest.raises(ValidationError, match="base64url"):
        Settings.model_validate({"data_encryption_key": "!" * 43})


def test_default_interview_day_range_is_configurable_and_bounded() -> None:
    configured = Settings.model_validate(
        {
            "default_interview_day_start": "08:30",
            "default_interview_day_end": "19:15",
        }
    )
    assert configured.default_interview_day_start == time(8, 30)
    assert configured.default_interview_day_end == time(19, 15)

    with pytest.raises(ValidationError, match="default interview day end"):
        Settings.model_validate(
            {
                "default_interview_day_start": "18:00",
                "default_interview_day_end": "09:00",
            }
        )


def test_production_invitation_url_is_pinned_to_the_flutter_app_link() -> None:
    with pytest.raises(ValidationError, match="talant-flow.app/interview"):
        settings_with_key(
            environment="production",
            supabase_url="https://example.supabase.co",
            supabase_db_url="postgresql://example/db?sslmode=require",
            public_interview_base_url="https://attacker.example/interview",
        )


def test_reschedule_preview_supports_date_only_and_validates_exact_mode() -> None:
    date_only = RescheduleAvailabilityRequest(
        company_id=uuid4(),
        timezone="Asia/Gaza",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
        duration_minutes=30,
    )
    assert date_only.window_start is None
    assert date_only.window_end is None

    with pytest.raises(ValidationError, match="must equal duration_minutes"):
        RescheduleAvailabilityRequest(
            company_id=uuid4(),
            timezone="Asia/Gaza",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 1),
            window_start=time(10),
            window_end=time(11),
            duration_minutes=30,
            mode=SchedulingMode.EXPLICIT,
        )


@pytest.mark.asyncio
async def test_repeated_jwks_outage_remains_service_unavailable_not_invalid_token() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "kid": "missing"}).encode()
    ).rstrip(b"=").decode()
    token = header + ".e30.unsigned"
    auth_settings = settings_with_key(
        supabase_url="https://example.supabase.co",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        auth = SupabaseAuth(auth_settings, client)
        for _ in range(2):
            with pytest.raises(ServiceError) as error:
                await auth.verify(token)
            assert error.value.code == "auth_unavailable"
            assert error.value.status == 503

@pytest.mark.asyncio
async def test_bearer_dependency_reads_authorization_header_instead_of_request_body() -> None:
    from fastapi import Depends, FastAPI
    from talentflow_orchestrator.api.dependencies import authenticated_user

    class StubAuth:
        async def verify(self, token: str):
            assert token == "test-token"
            return uuid4()

    app = FastAPI()
    app.state.auth = StubAuth()

    @app.get("/protected")
    async def protected(user_id=Depends(authenticated_user)):
        return {"user_id": str(user_id)}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/protected",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert response.json()["user_id"]
