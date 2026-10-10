# =====================================================================
# services/etl/Dockerfile — multi-stage; final tag airflow/etl:0.1
#
# Stage 1 builds the cashback-etl wheel; stage 2 installs it (i.e. the
# ETL deps from pyproject.toml) on top of the official Airflow image so
# the same image powers scheduler / webserver / worker.
#
# The install runs WITH the official Airflow constraints for the base
# image's Airflow + Python (vendored as
# constraints-airflow-<AIRFLOW_VERSION>-py<X.Y>.txt), so Airflow's own
# pins (SQLAlchemy<2, redis<5, ...) always win over our deps. Bumping the
# base image means vendoring the matching file — the build fails without it.
# =====================================================================

# ---- builder -------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /src
COPY pyproject.toml constraints-airflow-*.txt ./
COPY app ./app
COPY dags ./dags

RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

# ---- runtime -------------------------------------------------------
FROM apache/airflow:2.9.0 AS runtime

ARG ETL_HOME=/opt/etl
ENV PYTHONPATH=${ETL_HOME} \
    AIRFLOW__CORE__LOAD_EXAMPLES=False

USER root
RUN mkdir -p ${ETL_HOME} /data/staging /data/training \
 && chown -R airflow:0 ${ETL_HOME} /data

COPY --from=builder --chown=airflow:0 /src/app          ${ETL_HOME}/app
COPY --from=builder --chown=airflow:0 /src/dags         ${ETL_HOME}/dags
COPY --from=builder --chown=airflow:0 /src/pyproject.toml ${ETL_HOME}/pyproject.toml
COPY --from=builder --chown=airflow:0 /src/constraints-airflow-*.txt ${ETL_HOME}/
COPY --from=builder --chown=airflow:0 /wheels /tmp/wheels

USER airflow

# AIRFLOW_VERSION is set by the base image; listing apache-airflow==it
# keeps pip from up/downgrading Airflow while it installs our deps.
# `pip check` + importing every app module fail the build if the result
# is not a consistent environment the ETL code can run in.
RUN PY_MM="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')" \
 && pip install --no-cache-dir \
      --constraint "${ETL_HOME}/constraints-airflow-${AIRFLOW_VERSION}-py${PY_MM}.txt" \
      "apache-airflow==${AIRFLOW_VERSION}" \
      "$(ls /tmp/wheels/cashback_etl-*.whl)[airflow]" \
 && pip check \
 && python -c "import importlib, pkgutil, app; [importlib.import_module('app.' + m.name) for m in pkgutil.iter_modules(app.__path__)]" \
 && rm -rf /tmp/wheels
