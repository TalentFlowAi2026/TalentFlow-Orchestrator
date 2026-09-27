"""Liveness and readiness endpoints."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["service"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "talentflow-orchestrator"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    database_ready = await request.app.state.database.ready()
    status = 200 if database_ready else 503
    return JSONResponse(
        status_code=status,
        content={
            "status": "ready" if database_ready else "not_ready",
            "checks": {"database_contract": database_ready},
        },
    )
