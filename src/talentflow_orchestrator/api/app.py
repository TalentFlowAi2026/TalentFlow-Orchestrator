"""Application factory for the Scheduling Orchestrator API process."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from talentflow_orchestrator.api.middleware import (
    RequestBodyLimitMiddleware,
    RequestSafetyMiddleware,
)
from talentflow_orchestrator.api.routes import health, integrations, intent, scheduling
from talentflow_orchestrator.config.logging import configure_logging
from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError
from talentflow_orchestrator.persistence.integrations import IntegrationRepository
from talentflow_orchestrator.persistence.postgres import Database
from talentflow_orchestrator.persistence.repository import SchedulingRepository
from talentflow_orchestrator.providers.disabled import DisabledCalendarProvider
from talentflow_orchestrator.providers.gemini_intent import GeminiIntentProvider
from talentflow_orchestrator.providers.google.calendar import GoogleCalendarProvider
from talentflow_orchestrator.providers.google.oauth import GoogleOAuthProvider
from talentflow_orchestrator.security.auth import SupabaseAuth
from talentflow_orchestrator.workers.handlers import AccessTokenManager

logger = logging.getLogger(__name__)


def _correlation_id(request: Request) -> UUID:
    value: Any = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, UUID) else uuid4()


def _error_response(request: Request, exc: ServiceError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(max(1, int(exc.retry_after)))
    return JSONResponse(
        status_code=exc.status,
        headers=headers,
        content={
            "error": {
                "code": exc.code,
                "message": exc.code.replace("_", " "),
                "retryable": exc.retryable,
                "correlation_id": str(_correlation_id(request)),
            }
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    selected = settings or Settings()
    configure_logging(selected.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        missing = selected.missing("api")
        if missing:
            raise RuntimeError("Missing required API configuration: " + ", ".join(missing))
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(selected.provider_timeout_seconds),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
        database = Database(selected, pool_max=selected.api_database_pool_max)
        await database.open()
        try:
            await database.validate_schema()
            app.state.settings = selected
            app.state.database = database
            app.state.auth = SupabaseAuth(selected, client)
            app.state.scheduling_repository = SchedulingRepository(database, selected)
            integration_repository = IntegrationRepository(database, selected)
            oauth_provider = GoogleOAuthProvider(selected, client)
            app.state.integration_repository = integration_repository
            app.state.oauth_provider = oauth_provider
            app.state.access_token_manager = AccessTokenManager(
                integration_repository, oauth_provider
            )
            app.state.calendar_provider = (
                GoogleCalendarProvider(selected, client)
                if selected.google_calendar_enabled
                else DisabledCalendarProvider()
            )
            app.state.intent_provider = GeminiIntentProvider(selected)
            logger.info("orchestrator_api_ready")
            yield
        finally:
            intent_provider = getattr(app.state, "intent_provider", None)
            if intent_provider is not None:
                await intent_provider.aclose()
            await database.close()
            await client.aclose()

    app = FastAPI(
        title="TalentFlow Scheduling Orchestrator",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if selected.environment in {"development", "test"} else None,
        redoc_url=None,
        openapi_url="/openapi.json" if selected.environment in {"development", "test"} else None,
    )
    app.state.settings = selected
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=selected.max_request_bytes)
    app.add_middleware(RequestSafetyMiddleware, settings=selected)
    app.include_router(health.router)
    app.include_router(scheduling.router)
    app.include_router(intent.router)
    app.include_router(integrations.router)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        logger.warning(
            "orchestrator_request_failed",
            extra={"correlation_id": str(_correlation_id(request)), "error_code": exc.code},
        )
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning(
            "orchestrator_request_validation_failed",
            extra={
                "correlation_id": str(_correlation_id(request)),
                "validation_errors": exc.errors(),
            },
        )
        return _error_response(
            request,
            ServiceError("request_validation_failed", status=422, retryable=False),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "orchestrator_request_unexpected_failure",
            extra={"correlation_id": str(_correlation_id(request))},
            exc_info=exc,
        )
        return _error_response(request, ServiceError("internal_error", status=500))

    return app


app = create_app()


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "talentflow_orchestrator.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
