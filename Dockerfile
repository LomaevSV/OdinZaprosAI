# «О!дин запрос» — образ приложения (DELIVERY-01; «Правила по технике» организаторов, DEC-32).
# Multi-stage: сборка зависимостей с uv в builder, в рантайме только Python и виртуальное окружение
# (без uv, компиляторов и dev-зависимостей). Процесс — под непривилегированным пользователем.
# Базовые образы зафиксированы по версии; Mock-путь не требует платного ключа.

ARG PYTHON_IMAGE=python:3.12.7-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM ${PYTHON_IMAGE} AS builder
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    ODIN_PORT=8080 \
    ODIN_DATA_MODE=auto \
    ODIN_LLM_MODE=off \
    ODIN_DB_PATH=/data/odin.sqlite3 \
    ODIN_REPLAY_DIR=/data/replay_store
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin odin \
    && mkdir -p /data /app \
    && chown -R odin /data
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY config ./config
USER odin

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-m", "odin.healthcheck"]

CMD ["python", "-m", "odin.api"]
