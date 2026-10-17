# =====================================================================
# tx_simulator — multi-stage build (python:3.11-slim)
# =====================================================================

# ---- builder ---------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

RUN pip install --upgrade pip \
 && pip install --prefix=/install .

# ---- runtime ---------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    AVRO_SCHEMA_PATH=/app/schemas/transaction_event.avsc \
    PROFILES_PATH=/data/profiles.avro

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app /app/app
COPY tests /app/tests

RUN mkdir -p /data /app/schemas

CMD ["sleep", "infinity"]
