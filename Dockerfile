FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first: this layer stays cached until uv.lock changes
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Alembic migrations live inside the package (medllmbot/db/migrations)
COPY medllmbot ./medllmbot

# Processes do not run as root; the DB lives in /app/storage (volume)
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/storage \
    && chown bot:bot /app/storage
USER bot

ENV DB_PATH=/app/storage/bot.db
EXPOSE 8000

# One image, three commands: bot (default), admin, migrations
CMD ["python", "-m", "medllmbot"]
