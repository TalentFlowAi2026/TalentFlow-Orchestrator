FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_HTTP_TIMEOUT=300
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system orchestrator && useradd --system --gid orchestrator orchestrator
WORKDIR /app
COPY --from=builder --chown=orchestrator:orchestrator /app/.venv /app/.venv
COPY --from=builder --chown=orchestrator:orchestrator /app/src /app/src
USER orchestrator
EXPOSE 8080
CMD ["talentflow-orchestrator-api"]
