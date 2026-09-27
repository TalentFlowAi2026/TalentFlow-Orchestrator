"""Supabase JWT identity verification independent of the Interview Engine."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import httpx
import jwt

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError


class SupabaseAuth:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.issuer = settings.supabase_url.rstrip("/") + "/auth/v1"
        self._keys: dict[str, Any] = {}
        self._expires = 0.0
        self._last_fetch = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now - self._last_fetch < 5:
                if not self._keys or now >= self._expires:
                    raise ServiceError("auth_unavailable")
                return
            self._last_fetch = now
            try:
                response = await self.client.get(self.issuer + "/.well-known/jwks.json")
                response.raise_for_status()
                if len(response.content) > 65536:
                    raise ValueError("oversized JWKS")
                keys = response.json()["keys"]
                if not isinstance(keys, list) or len(keys) > 20:
                    raise ValueError("invalid JWKS")
                parsed = {
                    key["kid"]: key
                    for key in keys
                    if isinstance(key, dict) and isinstance(key.get("kid"), str)
                }
                if not parsed:
                    raise ValueError("empty JWKS")
                self._keys = parsed
                self._expires = now + self.settings.jwks_cache_seconds
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise ServiceError("auth_unavailable") from exc

    async def verify(self, token: str) -> UUID:
        if len(token) > 16384:
            raise ServiceError("invalid_token", status=401, retryable=False)
        if self.settings.supabase_auth_mode == "user_endpoint":
            return await self._verify_remote(token)
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = header.get("kid")
            if algorithm not in {"ES256", "RS256"} or not isinstance(kid, str):
                raise ValueError("unsupported token")
            if time.monotonic() >= self._expires or kid not in self._keys:
                await self._refresh()
            if time.monotonic() >= self._expires or kid not in self._keys:
                raise ValueError("unavailable key")
            key = jwt.PyJWK.from_dict(self._keys[kid], algorithm=algorithm)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self.settings.jwt_audience,
                issuer=self.issuer,
                leeway=10,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
            if claims.get("role") != "authenticated":
                raise ValueError("not an end user")
            return UUID(claims["sub"])
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise ServiceError("invalid_token", status=401, retryable=False) from exc

    async def _verify_remote(self, token: str) -> UUID:
        try:
            response = await self.client.get(
                self.issuer + "/user",
                headers={
                    "Authorization": "Bearer " + token,
                    "apikey": self.settings.supabase_publishable_key.get_secret_value(),
                },
            )
            if response.status_code in {400, 401, 403}:
                raise ServiceError("invalid_token", status=401, retryable=False)
            response.raise_for_status()
            return UUID(response.json()["id"])
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise ServiceError("auth_unavailable") from exc
