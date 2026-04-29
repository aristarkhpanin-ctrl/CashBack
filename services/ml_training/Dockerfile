# =====================================================================
# ml_training — multi-stage build (python:3.11-slim)
# =====================================================================

# ---- builder ---------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# lightgbm needs libgomp at runtime; gcc/g++ keep the door open for
# any source-only deps (implicit's BLAS bindings, scipy fall-back, …).
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

RUN pip install --upgrade pip \
 && pip install --prefix=/install .

# ---- runtime ---------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    MLFLOW_TRACKING_URI=http://mlflow:5000

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app /app/app

RUN mkdir -p /data/ml-artifacts

CMD ["python", "-m", "app.cli", "--help"]
