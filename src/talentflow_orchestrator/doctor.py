"""Deployment diagnostics for configuration and the live database contract."""

from __future__ import annotations

import argparse
import asyncio
import json

from pydantic import ValidationError

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.persistence.postgres import Database


async def diagnose(role: str, *, database: bool) -> tuple[bool, dict[str, object]]:
    try:
        settings = Settings()
    except ValidationError:
        return False, {"configuration": "invalid"}
    missing = settings.missing("api" if role == "api" else "background")
    result: dict[str, object] = {
        "service": "talentflow-orchestrator",
        "role": role,
        "configuration": "ok" if not missing else "missing",
        "missing": missing,
        "imports_worker_internals": False,
    }
    if missing or not database:
        result["database_contract"] = "not_checked"
        return not missing, result

    db = Database(settings)
    try:
        await db.open()
        await db.validate_schema()
        result["database_contract"] = "ok"
        return True, result
    except Exception:
        result["database_contract"] = "failed"
        return False, result
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate orchestrator deployment prerequisites")
    parser.add_argument("--role", choices=("api", "background"), default="api")
    parser.add_argument("--no-database", action="store_true")
    args = parser.parse_args()
    ok, result = asyncio.run(diagnose(args.role, database=not args.no_database))
    print(json.dumps(result, separators=(",", ":")))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
