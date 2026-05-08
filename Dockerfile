FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# --- deps layer (cache-friendly) ---
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# --- app layer ---
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
COPY config /app/config
COPY scripts /app/scripts
COPY ACKNOWLEDGMENT.md /app/ACKNOWLEDGMENT.md

RUN pip install -e .

# Non-root user
RUN useradd -m -u 1000 trader && chown -R trader:trader /app
USER trader

EXPOSE 8000

# Default command — overridden by docker-compose service definitions
CMD ["uvicorn", "trading_agent.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
