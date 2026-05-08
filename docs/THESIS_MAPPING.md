# Соответствие диссертации репозиторию

Этот документ — single-source-of-truth для проверяющих: каждый
пункт плана работы (главы 1-3 + приложения) сопоставлен с конкретным
файлом или папкой в этом репозитории. Документ живой — при изменении
кода обновляйте здесь же.

## Глава 1 — Теоретическая

| Пункт                                                  | Где |
|--------------------------------------------------------|-----|
| 1.1 Постановка задачи                                  | _не реализуется в коде_; см. README, секция «О проекте» |
| 1.2 Литературный обзор                                 | _не реализуется в коде_; см. диссертацию + ADR'ы со списком альтернатив |
| 1.3 Сравнение существующих решений                     | _не реализуется в коде_ |

## Глава 2 — Проектная часть

### 2.1 — Сравнение алгоритмов

| Пункт                                                  | Файл / папка |
|--------------------------------------------------------|--------------|
| Сравнение SVD++ / NCF / item-CF / two-tower            | [`docs/adr/0008-svdpp-as-retrieval.md`](adr/0008-svdpp-as-retrieval.md) |
| Сравнение GBDT / DLRM / Wide&Deep / TabNet             | [`docs/adr/0007-lightgbm-as-ranker.md`](adr/0007-lightgbm-as-ranker.md) |
| Двухступенчатая гибридная архитектура                  | `services/recommendation_api/app/api/recommendations.py` (3-stage pipeline) |

### 2.2 — Архитектура (диаграммы + схема БД)

| Пункт                                                  | Файл / папка |
|--------------------------------------------------------|--------------|
| Рисунок 1 (IDEF0 A-0)                                  | [`docs/architecture/idef0-a0.puml`](architecture/idef0-a0.puml) + `IDEF0-A0.png` |
| Рисунок 2 (DFD) *(если применимо)*                     | _покрывается C4 Container диаграммой ниже_ |
| Рисунок 3 (IDEF0 A1)                                   | [`docs/architecture/idef0-a1-decomposition.puml`](architecture/idef0-a1-decomposition.puml) |
| Рисунок 4 (IDEF0 A3)                                   | [`docs/architecture/idef0-a3-decomposition.puml`](architecture/idef0-a3-decomposition.puml) |
| Рисунок 5 (C4 Context)                                 | [`docs/architecture/c4-context.puml`](architecture/c4-context.puml) |
| Рисунок 6 (C4 Container)                               | [`docs/architecture/c4-containers.puml`](architecture/c4-containers.puml) |
| Рисунок 7 (C4 Component — Recommendation)              | [`docs/architecture/c4-components-recommendation.puml`](architecture/c4-components-recommendation.puml) |
| Диаграмма классов                                      | [`docs/architecture/uml-classes.puml`](architecture/uml-classes.puml) |
| **Таблица 9 — логическая схема БД (13 таблиц)**        | [`db_migrations/migrations/versions/001_init_oltp.py`](../db_migrations/migrations/versions/001_init_oltp.py) |
| Индексы из главы 2.2                                   | той же миграции (явные `CREATE INDEX ix_recommendations_pending`, `INCLUDE (status)`, `(user_id, generated_at DESC)`, `(user_id, experiment_id)`) |
| `calculate_cashback` (PL/pgSQL, прогрессивная шкала)   | той же миграции, секция `op.execute("CREATE OR REPLACE FUNCTION ...")` |

### 2.3 — Технологический стек (таблица 13)

Каждое решение из таблицы зафиксировано в ADR (формат Майкла Найгарда):

| Технология        | ADR                                                              |
|-------------------|------------------------------------------------------------------|
| Python 3.11       | [`docs/adr/0001-python-as-backend-language.md`](adr/0001-python-as-backend-language.md) |
| FastAPI 0.115     | [`docs/adr/0002-fastapi-as-web-framework.md`](adr/0002-fastapi-as-web-framework.md) |
| Apache Kafka 3.7  | [`docs/adr/0003-kafka-as-message-broker.md`](adr/0003-kafka-as-message-broker.md) |
| PostgreSQL 16     | [`docs/adr/0004-postgresql-as-oltp.md`](adr/0004-postgresql-as-oltp.md) |
| ClickHouse 24     | [`docs/adr/0005-clickhouse-as-olap.md`](adr/0005-clickhouse-as-olap.md) |
| Redis 7.2         | [`docs/adr/0006-redis-as-cache-and-feature-store.md`](adr/0006-redis-as-cache-and-feature-store.md) |
| LightGBM 4.3      | [`docs/adr/0007-lightgbm-as-ranker.md`](adr/0007-lightgbm-as-ranker.md) |
| implicit (SVD++)  | [`docs/adr/0008-svdpp-as-retrieval.md`](adr/0008-svdpp-as-retrieval.md) |
| MLflow 2.13       | [`docs/adr/0009-mlflow-for-ml-lifecycle.md`](adr/0009-mlflow-for-ml-lifecycle.md) |
| Airflow 2.9       | [`docs/adr/0010-airflow-for-orchestration.md`](adr/0010-airflow-for-orchestration.md) |
| Docker / K8s / Helm | [`docs/adr/0011-docker-kubernetes-helm.md`](adr/0011-docker-kubernetes-helm.md) |
| Frontend FSD      | [`docs/adr/0012-feature-sliced-design.md`](adr/0012-feature-sliced-design.md) |

## Глава 3 — Практическая часть

### 3.1 — Реализация ETL и моделей

| Артефакт                                              | Файл                                                                                  |
|--------------------------------------------------------|---------------------------------------------------------------------------------------|
| **Листинг 1** — Avro `TransactionEvent`                | [`infrastructure/kafka/schemas/transaction_event.avsc`](../infrastructure/kafka/schemas/transaction_event.avsc) |
| **Листинг 3.2** — `TransactionConsumer` (Kafka -> Parquet) | [`services/etl/app/kafka_consumer.py`](../services/etl/app/kafka_consumer.py) |
| **Листинг 3.3** — Daily ETL DAG                       | [`services/etl/dags/cashback_daily_etl.py`](../services/etl/dags/cashback_daily_etl.py) |
| **Листинг 3.4** — `DataValidator` (6 правил, таблица 16) | [`services/etl/app/validator.py`](../services/etl/app/validator.py) |
| **Листинг 3.5** — DDL ClickHouse `transactions_raw`   | [`infrastructure/clickhouse/migrations/001_create_transactions_raw.sql`](../infrastructure/clickhouse/migrations/001_create_transactions_raw.sql) |
| `user_rfm_features` (108 столбцов)                    | [`infrastructure/clickhouse/migrations/002_create_user_rfm_features.sql`](../infrastructure/clickhouse/migrations/002_create_user_rfm_features.sql) |
| `transactions_buffer` (Buffer engine)                 | [`infrastructure/clickhouse/migrations/003_create_buffer_table.sql`](../infrastructure/clickhouse/migrations/003_create_buffer_table.sql) |
| **Листинг 3.6** — `RFMComputer` + `RFM_QUERY`         | [`services/etl/app/feature_eng.py`](../services/etl/app/feature_eng.py) |
| **Листинг 3.7** — `LGBMTrainer` + 5-fold CV + SHAP    | [`services/ml_training/app/trainer.py`](../services/ml_training/app/trainer.py) |
| **Листинг 3.8** — `promote_if_better` + AUC + PSI gate| [`services/ml_training/app/promote_model.py`](../services/ml_training/app/promote_model.py) |
| SVD++ retrieval (ALS + FAISS)                          | [`services/ml_training/app/svd_trainer.py`](../services/ml_training/app/svd_trainer.py) |
| **Листинг 3.9** — Recommendation API endpoint         | [`services/recommendation_api/app/api/recommendations.py`](../services/recommendation_api/app/api/recommendations.py) |
| Симулятор транзакций                                   | [`services/tx_simulator/app/simulator.py`](../services/tx_simulator/app/simulator.py) |
| Таблица 14 — параметры Kafka topic                     | те же файлы (`KAFKA_TOPIC_CONFIG = {retention.ms, compression.type, max.message.bytes}`) |

### 3.2 — Реализация Campaign Manager + BRE + Notifications + Mobile

| Артефакт                                              | Файл                                                                                  |
|--------------------------------------------------------|---------------------------------------------------------------------------------------|
| **Листинг 3.10** — applicable filter (4-stage SQL)     | [`services/campaign_manager/app/api/campaigns.py`](../services/campaign_manager/app/api/campaigns.py) → `APPLICABLE_SQL` |
| **Листинг 3.11** — `TransactionListener`              | [`services/transaction_listener/app/listener.py`](../services/transaction_listener/app/listener.py) |
| `AccrualEngine` (PG TX + Redis delete + Kafka publish)| [`services/transaction_listener/app/accrual/engine.py`](../services/transaction_listener/app/accrual/engine.py) |
| **Листинг 3.12** — `NotificationPipeline` (Chain of Responsibility) | [`services/transaction_listener/app/notification/pipeline.py`](../services/transaction_listener/app/notification/pipeline.py) |
| OST (Optimal Send Time) — Updater + DeliveryScheduler | [`services/transaction_listener/app/notification/ost.py`](../services/transaction_listener/app/notification/ost.py) |
| **Листинг 3.13** — AudiencePreview UI                 | [`frontend/src/pages/Campaigns.tsx`](../frontend/src/pages/Campaigns.tsx) (Step 2) |
| **Листинг 3.14** — `BRE_CASES` параметризованный тест | [`services/recommendation_api/tests/unit/test_bre.py`](../services/recommendation_api/tests/unit/test_bre.py) |
| **Таблица 18** — Campaign API endpoints               | [`services/campaign_manager/app/api/campaigns.py`](../services/campaign_manager/app/api/campaigns.py) |
| **Таблица 19** — 6 правил BRE                         | [`services/recommendation_api/app/bre/rules/`](../services/recommendation_api/app/bre/rules/) |
| **Таблица 20** — A/B testing schema                   | [`db_migrations/migrations/versions/001_init_oltp.py`](../db_migrations/migrations/versions/001_init_oltp.py) (`ab_experiments`, `ab_variants`, `ab_assignments`, `ab_events`) |
| **Таблица 22** — Mobile API endpoints                 | [`services/mobile_api/app/api/mobile.py`](../services/mobile_api/app/api/mobile.py) |
| FSM статусов кампаний (DRAFT → ACTIVE → PAUSED → COMPLETED) | [`services/campaign_manager/app/fsm.py`](../services/campaign_manager/app/fsm.py) |

### 3.3 — Качество, тесты, CI/CD, развёртывание

| Артефакт                                              | Файл                                                                                  |
|--------------------------------------------------------|---------------------------------------------------------------------------------------|
| **Таблица 23** — пирамида тестирования (≥85% / 47 / E2E ≤5с / load ≥1000 RPS) | [`tests/`](../tests/), [`tests/README.md`](../tests/README.md) |
| **Листинг 3.15** — testcontainers fixtures            | [`tests/conftest.py`](../tests/conftest.py) |
| **Листинг 3.16** — GitHub Actions CI/CD pipeline      | [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) |
| 47 интеграционных тестов                              | [`tests/integration/`](../tests/integration/) |
| 9-шаговый E2E «Полный цикл»                          | [`tests/e2e/test_full_cashback_cycle.py`](../tests/e2e/test_full_cashback_cycle.py) |
| Locust 1 000 RPS / 70-20-10 mix                       | [`tests/load/locustfile.py`](../tests/load/locustfile.py) |
| **Таблица 27** — ресурсы / реплики / HPA               | [`helm/cashback/values.yaml`](../helm/cashback/values.yaml) |
| Helm chart                                            | [`helm/cashback/`](../helm/cashback/) |
| NetworkPolicy / Ingress                                | [`helm/cashback/templates/{networkpolicy,ingress}.yaml`](../helm/cashback/templates/) |
| Pre-commit hooks (ruff + mypy + prettier + shellcheck) | [`.pre-commit-config.yaml`](../.pre-commit-config.yaml) |
| Dependabot — еженедельные обновления                  | [`.github/dependabot.yml`](../.github/dependabot.yml) |
| CODEOWNERS                                             | [`.github/CODEOWNERS`](../.github/CODEOWNERS) |

## Приложения

| Приложение                                            | Файл                                                                                  |
|--------------------------------------------------------|---------------------------------------------------------------------------------------|
| Приложение №1 — BPMN основного потока                 | [`docs/architecture/bpmn-cashback-flow.puml`](architecture/bpmn-cashback-flow.puml) |
| Приложение №2 — листинги исходного кода (см. главу 3) | сами `*.py` файлы — все аннотированы заголовком и упоминанием листинга |

## Как пользоваться этой таблицей

1. Если PR меняет код, который указан в этой таблице — **обновите запись здесь
   же** в том же PR.
2. Если добавляете новую фичу, не отражённую в диссертации — заводите новый
   ADR в `docs/adr/NNNN-<slug>.md` и добавляйте строку в таблицу выше.
3. Перед защитой проверьте все ссылки кликабельны (`grep -RhE
   "\[\`.*\`\]\(.*\)"` + `xargs -I{}` проверка существования).
