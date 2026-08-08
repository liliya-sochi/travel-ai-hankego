# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

FROM python:3.11.15-slim-bookworm AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Отдельный слой зависимостей ускоряет повторные сборки.
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project


FROM python:3.11.15-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# API и бот не должны работать от root.
RUN groupadd --system --gid 10001 hankego \
    && useradd \
        --system \
        --uid 10001 \
        --gid hankego \
        --home-dir /app \
        --no-create-home \
        hankego

COPY --from=builder --chown=hankego:hankego /app/.venv /app/.venv
COPY --chown=hankego:hankego app ./app
COPY --chown=hankego:hankego alembic ./alembic
COPY --chown=hankego:hankego alembic.ini ./

USER 10001:10001

EXPOSE 8000

CMD ["gunicorn", "app.main:app", "--worker-class", "uvicorn_worker.UvicornWorker", "--workers", "2", "--bind", "0.0.0.0:8000", "--timeout", "120", "--graceful-timeout", "30", "--keep-alive", "5", "--access-logfile", "-", "--error-logfile", "-", "--no-control-socket"]