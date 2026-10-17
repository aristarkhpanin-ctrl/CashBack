# CashBack — Система персонализированного кэшбэка

[![CI](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/ci.yml/badge.svg)](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/ci.yml)
[![nightly-smoke](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/nightly-smoke.yml/badge.svg)](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/nightly-smoke.yml)
[![codecov](https://codecov.io/gh/aristarkhpanin-ctrl/CashBack/branch/main/graph/badge.svg)](https://codecov.io/gh/aristarkhpanin-ctrl/CashBack)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Реализация магистерской диссертации **«Система персонализированного кэшбэка
> на основе истории транзакций клиента»**. Каждый листинг и таблица из текста
> работы имеют конкретные файлы в этом репозитории — см. [Соответствие
> диссертации](#соответствие-диссертации) и [`docs/THESIS_MAPPING.md`](docs/THESIS_MAPPING.md).

## О проекте

Цель работы — построить production-ready систему, которая в реальном времени
рекомендует клиенту банка категории повышенного кэшбэка, основываясь на его
поведенческом профиле. Под капотом — двухступенчатая гибридная модель:
SVD++ для retrieval и LightGBM для ranking, обогащённая Business Rules
Engine с шестью бизнес-правилами и оптимизированным Optimal Send Time.
Стек **полностью соответствует таблице 13** диссертации; pipeline собран
через Airflow + Kafka + ClickHouse + Postgres + Redis + MLflow.

---

## Содержание

- [Архитектура](#архитектура)
- [Технологический стек](#технологический-стек-таблица-13)
- [Быстрый старт за 5 минут](#быстрый-старт-за-5-минут)
- [Скриншоты](#скриншоты)
- [Соответствие диссертации](#соответствие-диссертации)
- [Структура репозитория](#структура-репозитория)
- [Тестирование](#тестирование)
- [Развёртывание](#развёртывание)
- [Документация и ADR](#документация)
- [Лицензия](#лицензия)

---

## Архитектура

```text
                   +------------------+
                   |  Web / Mobile    |
                   |   (frontend +    |
                   |    mobile-api)   |
                   +--------+---------+
                            | JSON / REST
        +-------------------+---------------------+
        v                   v                     v
 +-------------+   +----------------+   +----------------+
 | recommend.  |   | campaign-mgr   |   | mobile-bff     |
 | api (8001)  |   | api  (8002)    |   | api  (8003)    |
 |             |   |                |   |                |
 | retrieve -> |   | campaigns +    |   | trim model_    |
 | rank ->     |   | A/B + analytics|   | score, add     |
 | BRE filter  |   |                |   | category info  |
 +--+--------+-+   +-----+----------+   +--------+-------+
    |        |          |                        |
    v        v          v                        |
 Redis    MLflow ---> candidate-gen + LGBM       |
 (feat-     |         (model registry)           |
  store)    |                                    |
            |                                    |
 PostgreSQL OLTP <--------+         <------------+
   * users / campaigns    |  SELECT FOR UPDATE
   * recommendations      |  budget_reservation
   * cashback_accruals    |
                          |
            +-------------+--------------+
            | transaction-listener (9100)|
            |  Kafka -> AccrualEngine -> |
            |  notification pipeline     |
            +-------------+--------------+
                          |
                          v
        Kafka cashback.accrued + recommendations.created
                          |
                          v
 ClickHouse (transactions_raw, user_rfm_features) <-- ETL/Airflow
```

* Полные **C4-диаграммы** (Context / Container / Component) и
  **IDEF0 / BPMN / UML** — в [`docs/architecture/`](docs/architecture/)
  как PlantUML-исходники + готовые PNG.
* Бизнес-логика BRE — 6 правил из таблицы 19 в порядке возрастания
  стоимости (cheap-checks first, FOR UPDATE last) — реализована в
  [`services/recommendation_api/app/bre/`](services/recommendation_api/app/bre/).

---

## Технологический стек (таблица 13)

| Слой               | Технология                          | Версия    | Где в репо |
|--------------------|-------------------------------------|-----------|------------|
| Язык backend       | Python                              | 3.11      | `services/*/pyproject.toml` |
| Web framework      | FastAPI                             | 0.115     | `services/*/app/main.py` |
| Async I/O          | asyncio + uvloop (uvicorn)          | 0.30      | `services/*/Dockerfile` |
| OLTP               | PostgreSQL                          | 16        | `db_migrations/`, `infrastructure/postgres/` |
| OLAP               | ClickHouse                          | 24.3      | `infrastructure/clickhouse/migrations/` |
| Cache & feature store | Redis                            | 7.2       | `services/*/app/feature_store.py` |
| Streaming          | Apache Kafka (KRaft)                | 3.7       | `infrastructure/kafka/schemas/`, `services/transaction_listener/` |
| Schema registry    | Confluent Schema Registry           | 7.6       | `docker-compose.yml` |
| Workflow           | Apache Airflow                      | 2.9       | `services/etl/dags/` |
| ML lifecycle       | MLflow                              | 2.13      | `services/ml_training/` |
| Ranking model      | LightGBM                            | 4.3       | `services/ml_training/app/trainer.py` |
| Retrieval model    | implicit (ALS)                      | 0.7       | `services/ml_training/app/svd_trainer.py` |
| Vector search      | FAISS                               | 1.15      | `services/recommendation_api/app/candidate_gen.py` |
| ORM                | SQLAlchemy 2.0 (async)              | 2.0       | `services/campaign_manager/app/models.py` |
| Frontend           | React + TypeScript + Vite           | 19 / 5    | `frontend/` |
| UI library         | Cashback2 design system + Recharts  | latest    | `frontend/src/components/cashback/` |
| State / data       | TanStack Query (live API + demo fallback) | 5   | `frontend/src/shared/api/` |
| Container          | Docker (multi-stage) + nginx        | 27 / 1.27 | `services/*/Dockerfile`, `frontend/Dockerfile` |
| Orchestration      | Kubernetes + Helm                   | 1.27 / 3.13 | `helm/cashback/` |
| CI/CD              | GitHub Actions + GHCR               | -         | `.github/workflows/` |
| Observability      | Prometheus + OpenTelemetry          | -         | `services/*/app/main.py` |
| Lint / Format      | ruff + mypy + prettier              | 0.6 / 1.11 / 3.3 | `ruff.toml`, `.pre-commit-config.yaml` |

---

## Быстрый старт за 5 минут

```bash
git clone https://github.com/aristarkhpanin-ctrl/CashBack.git
cd CashBack

make env && make up          # .env со случайными секретами; Postgres + Redis + Kafka + ClickHouse + MLflow + Airflow
make migrate                 # alembic upgrade head + ClickHouse SQL; создаёт admin@bank.ru
make seed-users              # 10 000 синтетических users + профили (Avro)
make seed-history            # 90 дней истории транзакций -> Kafka transactions.raw
make trigger-etl             # cashback_daily_etl -> user_rfm_features
make seed-recommendations    # синтетические recommendations с accept_rate 5-15%
make train-models            # SVD++ -> LightGBM -> MLflow Production
```

После этого:

| URL                                    | Что внутри                                         |
|----------------------------------------|----------------------------------------------------|
| http://localhost:3000                  | **Frontend** — Dashboard / Campaigns / Analytics / ML-объяснения |
| http://localhost:8001/docs             | Recommendation API — Swagger                       |
| http://localhost:8002/docs             | Campaign Manager API — Swagger                     |
| http://localhost:8003/docs             | Mobile BFF — Swagger                               |
| http://localhost:8080                  | Airflow UI (`admin` / `admin`)                     |
| http://localhost:5000                  | MLflow Tracking + Model Registry                   |
| http://localhost:8085                  | Kafka UI (топики + сообщения)                      |
| http://localhost:8090                  | Adminer (Postgres + ClickHouse)                    |
| http://localhost:9090                  | Prometheus + алерты (`make up-obs`)                |
| http://localhost:3001                  | Grafana, дашборд CashBack Overview (`make up-obs`) |
| http://localhost:9091                  | Pushgateway — метрики batch-джобов ML (`make up-obs`) |

Полный набор make-целей: `make help`.

> **Секреты и первый вход.** Встроенных секретов нет: без `.env` docker compose
> не стартует (`required variable …_JWT_SECRET is missing a value: run make env` —
> с именем первого отсутствующего ключа),
> сервисы отказываются запускаться с пустым, коротким (< 32 байт) или публичным
> ключом. `make env` создаёт `.env` из `.env.example` (mode 600) и заполняет
> пустые значения: ключи `JWT_SECRET` / `SERVICE_JWT_SECRET` / `MOBILE_JWT_SECRET` —
> `openssl rand -hex 32`, пароли `ADMIN_BOOTSTRAP_PASSWORD` / `DEMO_USERS_PASSWORD` —
> `openssl rand -base64 18`; существующий `.env` не перезаписывает никогда.
> Вход в админ-панель (http://localhost:3000): **admin@bank.ru** с паролем
> `ADMIN_BOOTSTRAP_PASSWORD` из `.env` — его применяет миграция, создающая
> админа (`make migrate` на пустой БД; на старой БД со встроенным `admin/admin`
> миграция 008 заменяет им старый пароль и требует сменить его при первом
> входе; уже существующий другой пароль не меняется). Если `ADMIN_BOOTSTRAP_PASSWORD` пуст,
> миграция 002 (новая БД) или 008 (старая БД с `admin/admin`) один раз печатает
> строку `bootstrap admin password: …`, и этот пароль нужно сменить при первом
> входе (экран «Смена пароля»). Демо-логины `make seed-demo` — пароль
> `DEMO_USERS_PASSWORD` (пуст — случайный, печатается в сводке сидера); их
> старые публичные пароли (`marketer` / `analyst`) на старой БД заменяет уже
> `make migrate` (008): на `DEMO_USERS_PASSWORD` или на случайный, напечатанный
> один раз строкой `demo users password: …`, со сменой при первом входе.
> Подробнее — [«Аутентификация и авторизация»](#аутентификация-и-авторизация-stage-b).

> **Live / демо-режим фронтенда.** В шапке интерфейса показан бейдж источника данных:
> `LIVE API` — страницы читают кампании, воронку, матрицу отклика, динамику принятых
> предложений, канальную аналитику, A/B-эксперименты и SHAP-объяснения
> из backend-сервисов (создание/редактирование/статусы кампаний тоже пишутся в БД);
> `ДЕМО-ДАННЫЕ` — backend недоступен, интерфейс работает на встроенных mock-данных,
> так что UI можно демонстрировать и без docker-стека (`cd frontend && npm run dev`).
> Наполнить БД демо-данными: `make seed-demo`.
>
> **Realtime (SSE).** В live-режиме дашборд слушает `/events/stream`
> (агрегаты начислений из топика `cashback.accrued`): KPI обновляются
> без перезагрузки, в шапке — индикатор «обновлено N с назад». Запустите
> симулятор (`make stream-on`), чтобы увидеть движение цифр вживую.

---

## Скриншоты

> **Примечание.** Скриншоты делаются вручную после первого запуска
> (Claude Code не имеет доступа к браузеру). После `make up` сделайте
> 5 скриншотов и положите их в `docs/screenshots/` под указанными ниже
> именами — комментарии с `<!-- ![...] -->` ниже автоматически подхватят
> файлы, как только вы их добавите и удалите HTML-комментарии.

| Имя файла                                   | Что снять                                      |
|---------------------------------------------|-----------------------------------------------|
| `docs/screenshots/01_dashboard.png`         | http://localhost:3000 — KPI + heatmap + top-5 |
| `docs/screenshots/02_campaigns_wizard.png`  | http://localhost:3000/campaigns — Step 2 (AudiencePreview) |
| `docs/screenshots/03_analytics_funnel.png`  | http://localhost:3000/analytics — funnel + segment matrix  |
| `docs/screenshots/04_experiments.png`       | http://localhost:3000/experiments — z-test calculator |
| `docs/screenshots/05_mlflow.png`            | http://localhost:5000 — experiment + Production-tag модели |

<!-- ![Dashboard](docs/screenshots/01_dashboard.png) -->
<!-- ![Campaign Wizard](docs/screenshots/02_campaigns_wizard.png) -->
<!-- ![Analytics](docs/screenshots/03_analytics_funnel.png) -->
<!-- ![Experiments](docs/screenshots/04_experiments.png) -->
<!-- ![MLflow](docs/screenshots/05_mlflow.png) -->

---

## Соответствие диссертации

Полный mapping — в [`docs/THESIS_MAPPING.md`](docs/THESIS_MAPPING.md).
Краткий обзор главных артефактов:

| Пункт диссертации                            | Файл / папка |
|----------------------------------------------|--------------|
| **Глава 2.1** — сравнение алгоритмов          | `docs/adr/0007-lightgbm-as-ranker.md`, `docs/adr/0008-svdpp-as-retrieval.md` |
| **Глава 2.2** — IDEF0 / BPMN / C4 / UML      | `docs/architecture/*.puml` (+ PNG) |
| **Глава 2.2** таблица 9 — логическая схема   | `db_migrations/migrations/versions/001_init_oltp.py` |
| **Глава 2.3** — обоснование стека             | `docs/adr/0001`-`0012` |
| **Глава 3.1** листинг 1 — Avro-схема         | `infrastructure/kafka/schemas/transaction_event.avsc` |
| **Глава 3.1** листинг 3.2 — Kafka Consumer   | `services/etl/app/kafka_consumer.py` |
| **Глава 3.1** листинг 3.3 — Daily ETL DAG    | `services/etl/dags/cashback_daily_etl.py` |
| **Глава 3.1** листинг 3.4 — DataValidator    | `services/etl/app/validator.py` |
| **Глава 3.1** листинг 3.5 — DDL ClickHouse   | `infrastructure/clickhouse/migrations/001_create_transactions_raw.sql` |
| **Глава 3.1** листинг 3.6 — RFMComputer      | `services/etl/app/feature_eng.py` |
| **Глава 3.1** листинг 3.7 — LightGBM trainer | `services/ml_training/app/trainer.py` |
| **Глава 3.1** листинг 3.8 — promote_model    | `services/ml_training/app/promote_model.py` |
| **Глава 3.1** листинг 3.9 — Recommendation API | `services/recommendation_api/app/api/recommendations.py` |
| **Глава 3.2** листинг 3.10 — applicable filter | `services/campaign_manager/app/api/campaigns.py` (`APPLICABLE_SQL`) |
| **Глава 3.2** листинг 3.11 — TransactionListener | `services/transaction_listener/app/listener.py` |
| **Глава 3.2** листинг 3.12 — NotificationPipeline | `services/transaction_listener/app/notification/pipeline.py` |
| **Глава 3.2** листинг 3.13 — AudiencePreview | `frontend/src/components/cashback/pages/Campaigns.tsx` (StepAudience) |
| **Глава 3.2** листинг 3.14 — BRE_CASES test  | `services/recommendation_api/tests/unit/test_bre.py` |
| **Глава 3.2** таблица 18 — Campaign API      | `services/campaign_manager/app/api/campaigns.py` |
| **Глава 3.2** таблица 19 — 6 правил BRE      | `services/recommendation_api/app/bre/rules/` |
| **Глава 3.2** таблица 20 — A/B testing       | `services/campaign_manager/app/api/ab_testing.py` |
| **Глава 3.2** таблица 22 — Mobile API        | `services/mobile_api/app/api/mobile.py` |
| **Глава 3.3** таблица 23 — пирамида тестов   | `tests/`, `services/*/tests/`, `tests/load/locustfile.py` |
| **Глава 3.3** листинг 3.15 — testcontainers  | `tests/conftest.py` |
| **Глава 3.3** листинг 3.16 — CI/CD           | `.github/workflows/ci.yml` |
| **Глава 3.3** таблица 27 — ресурсы / HPA     | `helm/cashback/values.yaml` |

---

## Структура репозитория

```text
CashBack/
├── .github/                      # GitHub Actions, Dependabot, CODEOWNERS
│   ├── workflows/
│   │   ├── ci.yml                # 4 stages: lint -> test -> build -> deploy
│   │   └── nightly-smoke.yml     # full compose stack + seed + model + e2e
│   ├── dependabot.yml
│   └── CODEOWNERS
├── db_migrations/                # Alembic OLTP schema (chapter 2.2)
│   ├── alembic.ini
│   └── migrations/versions/001_init_oltp.py
├── docs/
│   ├── architecture/             # PlantUML diagrams (rendered to PNG)
│   ├── adr/                      # 12 Architecture Decision Records (Nygard)
│   ├── screenshots/              # UI screenshots (manual)
│   └── THESIS_MAPPING.md         # full thesis to code mapping
├── frontend/                     # React 19 + Vite (Cashback2 design)
│   └── src/
│       ├── components/cashback/  # Layout, UI-примитивы, 6 страниц
│       ├── shared/api/           # axios-клиенты, адаптеры, live-хуки
│       ├── features/             # campaigns, analytics, ab-testing, recommendations
│       ├── pages/                # Dashboard / Campaigns / Analytics / Experiments
│       ├── shared/api/           # axios client, types, generated/
│       └── store/                # zustand wizardStore
├── helm/cashback/                # Helm chart (chapter 3.3, table 27)
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── README.md
│   └── templates/                # Deployment / Service / HPA per service
├── infrastructure/
│   ├── postgres/init/01-init-databases.sh
│   ├── clickhouse/migrations/    # transactions_raw, user_rfm_features, buffer
│   └── kafka/schemas/transaction_event.avsc  # listing 1
├── scripts/
│   ├── init_db.sh                # idempotent PG + CH migrations
│   ├── trigger_etl.sh            # Airflow API trigger
│   ├── seed_recommendations.py   # binomial accept_rate by segment
│   ├── run_load_test.sh          # Locust headless
│   ├── helm-package.sh           # build dist/cashback-X.Y.Z.tgz
│   └── generate-api-types.sh     # openapi-typescript
├── services/
│   ├── recommendation_api/       # listing 3.9 — 3-stage pipeline + BRE
│   ├── campaign_manager/         # table 18 + 20 + analytics
│   ├── mobile_api/               # table 22 — BFF
│   ├── transaction_listener/     # listings 3.11 + 3.12
│   ├── etl/                      # listings 3.2 / 3.3 / 3.4 / 3.6
│   ├── ml_training/              # listings 3.7 + 3.8
│   └── tx_simulator/             # listing 1 — Avro generator
├── shared/python/cashback_shared/
├── tests/
│   ├── conftest.py               # testcontainers fixtures (listing 3.15)
│   ├── integration/              # 47 tests across 6 categories
│   ├── e2e/test_full_cashback_cycle.py  # 9-step cycle
│   ├── load/locustfile.py        # 70/20/10 traffic mix
│   └── README.md
├── docker-compose.yml            # full local stack
├── Makefile                      # 35+ targets, see `make help`
├── ruff.toml
├── .pre-commit-config.yaml
├── README.md                     # this file
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE                       # MIT
└── SECURITY.md
```

---

## Тестирование

Полная пирамида (chapter 3.3, table 23):

| Слой           | Где                              | Метрика                                             |
|----------------|----------------------------------|-----------------------------------------------------|
| **Unit**       | `services/*/tests/unit/`         | **>= 85 %** coverage gates на каждый сервис, 7/7 проходят |
| **Integration**| `tests/integration/`             | **47 тестов** в 6 категориях (Kafka loader, FeatureStore, Campaigns CRUD, BRE 6x3, Notifications, Recommendation flow) |
| **E2E (stack)**| `tests/e2e/`                     | 9-шаговый «Полный цикл персонализированного кэшбэка», <= 5 с |
| **E2E (UI)**   | `frontend/e2e/`                  | **26 Playwright-тестов**: продакшен-бандл в демо- и live-режимах (логин, CRUD, SHAP, A/B, 0 console-ошибок) |
| **Contract**   | `services/campaign_manager/tests/unit/test_stub_contract.py` | payload'ы E2E-стаба валидируются Pydantic-схемами сервиса — расхождение контракта роняет CI |
| **API types**  | `frontend/src/shared/api/generated/` | TS-типы из OpenAPI всех трёх сервисов (faiss/tenacity застаблены в дампере); CI-джоб `api-types-drift` регенерирует и сверяет `git diff` — правка схем без `make gen-api-types` роняет сборку |
| **Load**       | `tests/load/locustfile.py`       | 1 000 RPS / 5 мин — `GET /recommendations` 70 %, `POST /respond` 20 %, `GET /applicable` 10 % |

```bash
make test-unit          # gate >=85% per service
make test-integration   # 47 tests
make test-e2e           # 1 test, ~3 s
make test-load          # -> test-reports/load-<ts>.html
make test-all           # последовательно
cd frontend && npm run test:e2e   # Playwright: demo + live против стаба
```

В CI E2E-джоб (`frontend-e2e`) собирает продакшен-бандл, поднимает его
через `vite preview` и гоняет оба режима; live — против
`frontend/e2e/stub_backend.py`, чей контракт закреплён Pydantic-тестами.
Джоб `api-types-drift` регенерирует TS-типы из OpenAPI и падает при
рассинхроне контракта.
Раз в сутки `nightly-smoke.yml` создаёт `.env` (`make env`), поднимает
полный docker-стек, прогоняет миграции, сид и API-smoke (вход паролем
bootstrap-админа из `.env`, `admin/admin` → 401, SSE / mobile BFF /
recommendation_api без токена → 401), обучает и продвигает в Production
ранжирующую модель (`make train-models`), ждёт её загрузки
в recommendation-api и гоняет e2e денежного пути (`make test-e2e`:
customer-токен демо-IdP, чужой токен → 403 / 404, ML-объяснение без
побочных эффектов). Интеграционные джобы CI генерируют случайные
`JWT_SECRET` / `SERVICE_JWT_SECRET` / `MOBILE_JWT_SECRET` на каждый прогон.

Текущие coverage-цифры (на момент тегирования v1.0.0):
`etl 96.7%` · `recommendation_api 91.5%` · `campaign_manager 98.3%` ·
`transaction_listener 94.9%` · `mobile_api 94.2%` · `ml_training 86.1%` ·
`tx_simulator 47.5%` (gate 40 %; orchestration исключена).

---

## Развёртывание

### Локально (Docker Compose)

```bash
make env        # один раз: .env со случайными секретами
make up
make migrate
```

### Kubernetes (Helm)

```bash
helm dependency build helm/cashback    # сабчарты Bitnami из OCI, без `helm repo add`
helm install cashback helm/cashback \
    --namespace cashback --create-namespace \
    -f my-values.yaml --atomic --timeout 10m
```

Чарт не рендерится без `secrets.jwtSecret`, `secrets.serviceJwtSecret`,
`secrets.mobileJwtSecret` (≥ 32 байт, все разные) и
`secrets.adminBootstrapPassword` — либо `externalSecrets.enabled=true`.
См. [`helm/cashback/README.md`](helm/cashback/README.md) — таблица всех
параметров `values.yaml` (replicas / resources / HPA), описание
NetworkPolicy, варианты secrets (inline vs `external-secrets-operator`) и
какой Deployment какой ключ получает.

### CI/CD

`.github/workflows/ci.yml` запускает 4-стадийный pipeline:
**lint-and-type -> test -> build -> deploy** на push в любую ветку и на
PR из форков (PR из веток репозитория уже проверены push-прогоном);
build/deploy — только push в default-ветку: образы публикуются в
`ghcr.io/aristarkhpanin-ctrl/cashback/<service>`.
Подробнее — листинг 3.16 диссертации.

---

## Наблюдаемость

Observability-стек вынесен в `docker-compose.observability.yml`, чтобы не
утяжелять базовый `make up`:

```bash
make up-obs     # основной стек + Prometheus, Alertmanager, Grafana, kafka-exporter
make down-obs
```

| Компонент | URL | Что смотреть |
|-----------|-----|--------------|
| Prometheus | http://localhost:9090 | вкладка **Alerts** — 5 правил (lag, p95, бюджет, PSI, up) |
| Alertmanager | http://localhost:9093 | маршрутизация; сработавшие алерты дублируются в `docker logs cashback-alert-logger` |
| Grafana | http://localhost:3001 (`admin`/`admin`) | provisioned-дашборд **CashBack Overview**: RPS, p95, consumer lag, освоение бюджетов, исходы начислений, PSI |

Правила алертинга — `infrastructure/prometheus/alerts.yml`:

| Алерт | Условие | Смысл |
|-------|---------|-------|
| `KafkaConsumerLagHigh` | lag > 10 000 за 5 мин | ETL/listener не успевает за потоком транзакций |
| `RecommendationLatencyHigh` | p95 > 100 мс за 5 мин | нарушен бюджет задержки горячего пути (гл. 3.1) |
| `CampaignBudgetNearlyExhausted` | освоение > 95 % | R6 скоро начнёт отклонять рекомендации |
| `ModelDriftDetected` | PSI > 0.2 за 10 мин | дрейф признаков, авто-продвижение заблокировано |
| `ServiceDown` | up == 0 за 2 мин | сервис перестал отвечать на scrape |

Метрики `campaign_budget_utilization_ratio` и `ml_online_ctr{model_version}`
экспортируются планировщиком campaign_manager (`app/scheduling.py`) на каждом
тике; batch-метрики обучения (`ml_last_psi`, `ml_last_promotion_result`)
ml_training пушит в **Pushgateway** (:9091, поднимается `make up-obs`); идемпотентность
денежного контура закреплена интеграционными тестами
(`services/transaction_listener/tests/integration/test_e2e_accrual.py` —
повторная доставка и гонка за бюджет; `services/campaign_manager/tests/
integration/test_campaigns_crud.py` — конкурентное резервирование).

---

## Аутентификация и авторизация (stage B)

Модель угроз, решения и остаточные риски — [ADR-0013](docs/adr/0013-auth-hardening.md).

**Три ключа — три назначения** (утечка одного не даёт подделать остальные):

| Переменная | Подписывает / проверяет | Для чего |
|------------|-------------------------|----------|
| `JWT_SECRET` | campaign_manager | access / refresh / SSE-токены админ-панели |
| `SERVICE_JWT_SECRET` | mobile_api и campaign_manager подписывают, recommendation_api проверяет | service-токены в recommendation_api |
| `MOBILE_JWT_SECRET` | mobile_api проверяет; IdP банка (в демо — `scripts/mint_customer_token.py`) подписывает | customer-токены мобильного BFF |

Каждый сервис получает только свои ключи (docker-compose, Helm): ни
mobile_api, ни recommendation_api не знают `JWT_SECRET`. Все токены — HS256
с `iss` / `aud` / `type` / `exp`, чужой тип или аудитория — 401.

**Админ-панель (campaign_manager).** `POST /auth/login` → access (15 мин) +
refresh (7 дней); 5 неудачных попыток на (email, IP) и 30 попыток на IP за
15 мин → 429 с `Retry-After` (Redis; если он недоступен — вход без лимита
и ошибка в логе). IP клиента за прокси uvicorn берёт из `X-Forwarded-For`
только от `FORWARDED_ALLOW_IPS`: в compose это закреплённый адрес nginx
фронта (`172.28.0.10`), в Helm — обязательный при ingress
`campaignManager.forwardedAllowIps`. Refresh-токены одноразовые: повторное предъявление
отзывает всю цепочку (`POST /auth/refresh`), `POST /auth/logout` отзывает её
явно. Роль и активность читаются из БД на каждом запросе: смена роли,
деактивация или смена пароля (`token_version`) сразу отзывают все токены
пользователя. Сгенерированный миграцией пароль админа требует смены
(`POST /auth/change-password`, ≥ 12 символов): до неё токен годится только
для `/auth/me` и смены пароля (403 `password change required`).

Права — ключи `dashboard`, `campaigns_view`, `campaigns_create`,
`campaigns_edit`, `campaigns_delete`, `analytics`, `users` на каждом
маршруте (полная таблица — контракт в ADR-0013; тест
`test_route_permissions.py` падает на маршруте без права). Значения по
умолчанию (ADMIN меняет матрицу в UI, `PATCH /roles/{role}/permissions`):

| Право | ADMIN | MARKETER | ANALYST |
|-------|:-----:|:--------:|:-------:|
| `dashboard`, `campaigns_view` | ✅ | ✅ | ✅ |
| `campaigns_create`, `campaigns_edit` (в т.ч. мутации A/B) | ✅ | ✅ | ❌ |
| `campaigns_delete` | ✅ | ❌ | ❌ |
| `analytics` (аналитика, эксперименты, ML, ML-лимиты на чтение) | ✅ | ❌ | ✅ |
| `users` (`/auth/users`, чтение матрицы прав) | ✅ | ❌ | ❌ |

Только ADMIN, без делегирования: правка матрицы прав, `PUT /ml-limits`
(kill switch), создание / повышение до ADMIN и правка ADMIN-аккаунтов.
Удаления пользователей нет — только деактивация (`is_active=false`), ради
аудит-следа.

Демо-доступы: **admin@bank.ru** — пароль `ADMIN_BOOTSTRAP_PASSWORD` из `.env`
(см. [«Быстрый старт»](#быстрый-старт-за-5-минут)); после `make seed-demo` —
m.sokolova@bank.ru (MARKETER) и d.ivanov@bank.ru (ANALYST) с паролем
`DEMO_USERS_PASSWORD`. Старые публичные пароли демо-логинов (`marketer` /
`analyst`) заменяет миграция 008 (и сидер). Фронтенд в live-режиме не
показывает учётных данных.

**Realtime (SSE).** `GET /events/stream` требует короткий одноразовый
SSE-токен (`POST /events/token`, право `dashboard`, 60 с) в `?token=`; без
него или повторно — 401. Право `dashboard` проверяется и при подключении, и
раз в минуту у открытого потока (отключённый или лишённый права
пользователь теряет поток); на пользователя — не больше
`SSE_MAX_SUBSCRIBERS_PER_USER` (5) потоков на процесс, сверх — 429.

**Мобильный BFF (mobile_api).** Каждый `/v1/mobile/*` требует customer-токен
(`aud cashback-mobile`, `iss` = `MOBILE_JWT_ISSUER`); `{user_id}` в пути
должен совпадать с `sub` (иначе 403), отклик на чужую рекомендацию — 404.
В проде токены выпускает IdP банка; в демо — скрипт:

```bash
USER_ID=$(docker exec cashback-postgres psql -U cashback -Atc "SELECT user_id FROM users LIMIT 1")
TOKEN=$(python3 scripts/mint_customer_token.py --user-id "$USER_ID" --ttl 3600)   # MOBILE_JWT_SECRET из .env
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8003/v1/mobile/recommendations/$USER_ID"
```

**recommendation_api** принимает только service-токены (`SERVICE_JWT_SECRET`,
`aud recommendation-api`): боевую выдачу `GET /recommendations/{user_id}` —
только от `iss mobile-api`, read-only `GET /recommendations/{user_id}/explain`
(страница «ML-объяснения» через campaign_manager `GET /ml/explain/{user_id}`,
право `analytics`) — только от `iss campaign-manager`. Админский токен там
— 401, токен не того сервиса — 403; выключателя проверки нет.

**CORS.** `CORS_ORIGINS` — явный список (запятые или JSON); `*` сервисы не
принимают. UI ходит same-origin через nginx, compose разрешает
`http://localhost:3000,http://localhost:5173`.

> Компромисс демо-стенда: refresh-токен хранится в localStorage
> (нет httpOnly-cookie сессий); в проде — BFF-cookie или IdP. Остальные
> известные ограничения — раздел «Residual risks» ADR-0013.

---

## Документация

* [`docs/THESIS_MAPPING.md`](docs/THESIS_MAPPING.md) — полная привязка
  пунктов плана диссертации к коду.
* [`docs/ROADMAP.md`](docs/ROADMAP.md) — дорожная карта развития
  (фазы 15–20): auth/RBAC, достройка аналитики, надёжность,
  ML-контур 2.0, realtime, E2E в CI.
* [`docs/architecture/`](docs/architecture/) — диаграммы (PlantUML +
  PNG): C4 Context / Container / Component, IDEF0 (A0 / A1 / A3),
  BPMN cashback flow, UML class diagram.
* [`docs/adr/`](docs/adr/) — 13 ADR в формате Майкла Найгарда:
  - 0001 Python as backend language
  - 0002 FastAPI as web framework
  - 0003 Kafka as message broker
  - 0004 PostgreSQL as OLTP
  - 0005 ClickHouse as OLAP
  - 0006 Redis as cache & feature store
  - 0007 LightGBM as ranker
  - 0008 SVD++ as retrieval
  - 0009 MLflow for ML lifecycle
  - 0010 Airflow for orchestration
  - 0011 Docker / Kubernetes / Helm
  - 0012 Feature-Sliced Design (frontend)
  - 0013 Auth hardening (stage B: секреты, сессии, RBAC, SSE, customer- и service-токены)
* [`tests/README.md`](tests/README.md) — пирамида тестирования.
* [`helm/cashback/README.md`](helm/cashback/README.md) — Helm-chart руководство.
* [`db_migrations/README.md`](db_migrations/README.md) — Alembic CRUD.
* [`CHANGELOG.md`](CHANGELOG.md) — Keep-a-Changelog по фазам разработки.
* [`CONTRIBUTING.md`](CONTRIBUTING.md) — Conventional Commits, git flow.
* [`SECURITY.md`](SECURITY.md) — coordinated disclosure.

---

## Лицензия

[MIT](LICENSE) © 2026 Aristarkh Panin.

Этот репозиторий — академический артефакт магистерской диссертации.
В коммерческой эксплуатации потребуется адаптация: production-grade
secret management, обновление дефолтных пользователей и паролей,
обработка персональных данных по 152-ФЗ, мониторинг (Grafana / Loki /
Tempo), distributed tracing.
