# «О!дин запрос» — образ S1 (DELIVERY-01, план §2.16). Mock-путь не требует платного ключа.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    ODIN_PORT=8080 \
    ODIN_DATA_MODE=mock \
    ODIN_LLM_MODE=off \
    ODIN_DB_PATH=/data/odin.sqlite3

# uv фиксированной версии из официального образа (без curl | sh).
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
COPY schemas ./schemas
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --home-dir /app odin \
    && mkdir -p /data \
    && chown -R odin /data
USER odin

ENV PATH="/opt/venv/bin:${PATH}"
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "odin.healthcheck"]

CMD ["python", "-m", "odin.api"]
