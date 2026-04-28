#!/usr/bin/env bash
# Creates auxiliary databases and roles for Airflow and MLflow on
# the same PostgreSQL instance as the application database.
set -euo pipefail

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER ${AIRFLOW_POSTGRES_USER:-airflow} WITH PASSWORD '${AIRFLOW_POSTGRES_PASSWORD:-airflow}';
    CREATE DATABASE ${AIRFLOW_POSTGRES_DB:-airflow} OWNER ${AIRFLOW_POSTGRES_USER:-airflow};
    GRANT ALL PRIVILEGES ON DATABASE ${AIRFLOW_POSTGRES_DB:-airflow} TO ${AIRFLOW_POSTGRES_USER:-airflow};

    CREATE USER ${MLFLOW_POSTGRES_USER:-mlflow} WITH PASSWORD '${MLFLOW_POSTGRES_PASSWORD:-mlflow}';
    CREATE DATABASE ${MLFLOW_POSTGRES_DB:-mlflow} OWNER ${MLFLOW_POSTGRES_USER:-mlflow};
    GRANT ALL PRIVILEGES ON DATABASE ${MLFLOW_POSTGRES_DB:-mlflow} TO ${MLFLOW_POSTGRES_USER:-mlflow};
EOSQL
