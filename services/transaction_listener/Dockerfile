# =====================================================================
# transaction_listener — multi-stage build (python:3.11-slim)
# =====================================================================

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

RUN pip install --upgrade pip \
 && pip install --prefix=/install .

# ---------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    METRICS_PORT=9100 \
    TEMPLATE_DIR=/app/app/notification/templates \
    SENT_EMAILS_DIR=/tmp/sent_emails

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app /app/app

RUN mkdir -p /tmp/sent_emails

EXPOSE 9100

CMD ["python", "-m", "app.main"]
