"""Structured JSON logging with a strict safe-field allowlist."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_SAFE_EXTRA = frozenset(
    {
        "company_id",
        "interview_id",
        "candidate_id",
        "correlation_id",
        "job_id",
        "proposal_id",
        "provider",
        "action",
        "error_code",
        "attempt",
        "interview_count",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in _SAFE_EXTRA:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
