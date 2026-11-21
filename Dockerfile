FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Сначала зависимости — этот слой кэшируется, пока не меняется uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Миграции Alembic лежат внутри пакета (medllmbot/db/migrations)
COPY medllmbot ./medllmbot

# Процессы работают не от root; база лежит в /app/storage (volume)
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/storage \
    && chown bot:bot /app/storage
USER bot

ENV DB_PATH=/app/storage/bot.db
EXPOSE 8000

# Один образ — три команды: бот (по умолчанию), админка, миграции
CMD ["python", "-m", "medllmbot"]
