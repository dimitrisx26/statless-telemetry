FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv — pinned for reproducible builds (matches CI-era uv)
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv

# Dependency layer: only pyproject + lock invalidate it
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home statless \
    && mkdir -p /app/data && chown -R statless:statless /app
USER statless

# SQLite file lives here (mount a volume in production)
VOLUME /app/data
EXPOSE 8000

# Run the installed venv entrypoint directly (not `uv run`): the non-root user
# has no home directory for uv's cache, and this also skips uv's startup cost.
# The `statless-telemetry` script applies TELEMETRY_HOST/TELEMETRY_PORT.
CMD ["/app/.venv/bin/statless-telemetry"]
