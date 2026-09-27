"""Small request-safety middleware without storing tokens or request bodies."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from talentflow_orchestrator.config.settings import Settings


class RequestBodyLimitMiddleware:
    """Bound streamed/chunked request bodies before FastAPI parses them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            await self.app(scope, receive, send)
            return

        buffered: deque[Message] = deque()
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    correlation = scope.get("state", {}).get("correlation_id", uuid4())
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "request_too_large",
                                "message": "request too large",
                                "retryable": False,
                                "correlation_id": str(correlation),
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            else:
                break

        async def replay() -> Message:
            if buffered:
                return buffered.popleft()
            return await receive()

        await self.app(scope, replay, send)


class RequestSafetyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.settings = settings
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.correlation_id = self._correlation_id(request)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.settings.max_request_bytes:
                    return self._error(request, 413, "request_too_large")
            except ValueError:
                return self._error(request, 400, "content_length_invalid")
        if not self._allow(request):
            response = self._error(request, 429, "rate_limit_exceeded")
            response.headers["Retry-After"] = "60"
            return response
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = str(request.state.correlation_id)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _allow(self, request: Request) -> bool:
        if request.url.path in {"/health", "/ready"}:
            return True
        authorization = request.headers.get("authorization", "")
        peer = request.client.host if request.client else "unknown"
        identity = hashlib.sha256((authorization + "\0" + peer).encode()).hexdigest()
        now = time.monotonic()
        bucket = self._requests[identity]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= self.settings.api_requests_per_minute:
            return False
        bucket.append(now)
        if len(self._requests) > 10_000:
            self._requests = defaultdict(deque, {identity: bucket})
        return True

    @staticmethod
    def _correlation_id(request: Request) -> UUID:
        value = request.headers.get("x-correlation-id")
        try:
            return UUID(value) if value else uuid4()
        except ValueError:
            return uuid4()

    @staticmethod
    def _error(request: Request, status: int, code: str) -> Response:
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": code.replace("_", " "),
                    "retryable": status >= 429,
                    "correlation_id": str(request.state.correlation_id),
                }
            },
        )
