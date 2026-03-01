FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ src/
COPY alembic.ini .
COPY alembic/ alembic/

ENV PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "-m", "second_brain.main"]
