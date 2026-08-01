# =====================================================================
# services/etl/Dockerfile — multi-stage; final tag airflow/etl:0.1
#
# Stage 1 stages the source tree; stage 2 layers our ETL deps and DAGs
# on top of the official Airflow image so the same image powers
# scheduler / webserver / worker.
# =====================================================================

# ---- builder -------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
COPY dags ./dags

# ---- runtime -------------------------------------------------------
FROM apache/airflow:3.3.0 AS runtime

ARG ETL_HOME=/opt/etl
ENV PYTHONPATH=${ETL_HOME} \
    AIRFLOW__CORE__LOAD_EXAMPLES=False

USER root
RUN mkdir -p ${ETL_HOME} /data/staging /data/training \
 && chown -R airflow:0 ${ETL_HOME} /data

COPY --from=builder --chown=airflow:0 /src/app          ${ETL_HOME}/app
COPY --from=builder --chown=airflow:0 /src/dags         ${ETL_HOME}/dags
COPY --from=builder --chown=airflow:0 /src/pyproject.toml ${ETL_HOME}/pyproject.toml

USER airflow

RUN pip install --no-cache-dir \
    "aiokafka>=0.11.0" \
    "confluent-kafka[avro,schemaregistry]>=2.5.0" \
    "pandas>=2.1.0" \
    "pyarrow>=15.0.0" \
    "SQLAlchemy[asyncio]>=2.0.0" \
    "asyncpg>=0.29.0" \
    "clickhouse-connect>=0.7.0" \
    "redis[hiredis]>=5.0.0" \
    "pydantic-settings>=2.0.0" \
    "structlog>=24.1.0" \
    "fastavro>=1.9.0" \
    "prometheus-client>=0.20.0"
