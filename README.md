# CashBack — Система персонализированного кэшбэка

[![CI](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/ci.yml)
[![PR](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/pr.yml/badge.svg)](https://github.com/aristarkhpanin-ctrl/CashBack/actions/workflows/pr.yml)
[![codecov](https://codecov.io/gh/aristarkhpanin-ctrl/CashBack/branch/main/graph/badge.svg)](https://codecov.io/gh/aristarkhpanin-ctrl/CashBack)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://pre-commit.com/)
[![License](https://img.shields.io/badge/license-TBD-lightgrey)](#лицензия)

Монорепозиторий системы рекомендации и расчёта персонализированного кэшбэка
на основе истории транзакций клиента.

## Содержание

- [Архитектура](#архитектура)
- [Состав сервисов](#состав-сервисов)
- [Быстрый старт](#быстрый-старт)
- [Скриншоты](#скриншоты)
- [Документация](#документация)

## Архитектура

> TODO: добавить диаграмму архитектуры (`docs/architecture/`).

## Состав сервисов

| Сервис | Описание |
|--------|----------|
| `services/recommendation_api` | API персонализированных рекомендаций кэшбэка |
| `services/campaign_manager` | Управление маркетинговыми кампаниями |
| `services/mobile_api` | BFF для мобильного приложения |
| `services/transaction_listener` | Потоковая обработка транзакций из Kafka |
| `services/etl` | ETL-пайплайны (Airflow DAGs) |
| `services/ml_training` | Обучение ML-моделей, MLflow tracking |
| `services/tx_simulator` | Генератор синтетических транзакций |
| `shared/python/cashback_shared` | Общие Python-библиотеки |
| `frontend` | Веб-интерфейс администратора |

## Инфраструктура

Локальное окружение разворачивается через `docker compose`:

- PostgreSQL 16 — операционная БД
- ClickHouse 24 — аналитическое хранилище
- Redis 7.2 — кеш / feature store
- Apache Kafka 3.7 (KRaft) — шина событий
- Schema Registry — реестр схем
- MLflow 2.x — трекинг ML-экспериментов
- Apache Airflow 2.9 — оркестрация ETL
- Adminer / Kafka UI — инструменты администрирования

## Быстрый старт

```bash
cp .env.example .env
make up      # поднять инфраструктуру + фронтенд
make ps      # статус контейнеров
make logs    # логи
make seed    # заполнить тестовыми данными
make test    # тесты
make down    # остановить
make clean   # удалить контейнеры и volumes
```

После запуска:

| URL                                 | Что внутри                                            |
|-------------------------------------|--------------------------------------------------------|
| http://localhost:3000               | Frontend (Vite + React + nginx)                       |
| http://localhost:8001/docs          | Recommendation API (Swagger)                          |
| http://localhost:8002/docs          | Campaign Manager API                                  |
| http://localhost:8003/docs          | Mobile BFF                                            |
| http://localhost:8080               | Airflow UI (`admin` / `admin`)                        |
| http://localhost:5000               | MLflow Tracking                                       |
| http://localhost:8085               | Kafka UI                                              |
| http://localhost:8090               | Adminer (Postgres / ClickHouse)                       |

## Frontend

`./frontend/` — React 19 + Vite + TypeScript + Tailwind 4 + shadcn/ui +
Recharts + react-router-dom 7 + TanStack Query + zustand + sonner.

* Разделы: **Дашборд**, **Кампании** (5-step Wizard, AudiencePreview через
  `estimateAudience` с debounce 400 мс), **Аналитика** (воронка +
  Сегмент×MCC матрица), **Эксперименты** (список + результаты z-теста +
  client-side калькулятор), `/recommendations/:id/explain` —
  двунаправленный SHAP bar-chart.
* HTTP-клиент: `src/shared/api/client.ts` (axios + sonner-interceptor).
  Базовые URL — `VITE_RECOMMENDATION_API_URL`, `VITE_CAMPAIGN_API_URL`,
  `VITE_MOBILE_API_URL`. В Docker они указывают на nginx-префиксы
  (`/api/recommendations`, `/api/campaigns`, `/api/mobile`); nginx
  проксирует на FastAPI-сервисы — CORS снимается на gateway.
* Типы: ручной контракт в `src/shared/api/types.ts`. Сгенерировать
  full-typing schema из живых сервисов — `make frontend-types`
  (`bash scripts/generate-api-types.sh`).
* Полезные команды:

  ```bash
  make frontend-build      # пересобрать образ
  make frontend-logs       # tail nginx
  make frontend-types      # обновить generated/*.ts из openapi.json
  ```

* Локальная разработка без Docker (бэкенды подняты `make up`):

  ```bash
  cd frontend
  npm install
  npm run dev              # http://localhost:3000
  ```

## Скриншоты

> Разместите PNG-ки в `docs/screenshots/` — ссылки ниже расставлены на
> ожидаемые имена файлов.

<!-- ![Dashboard](docs/screenshots/frontend_dashboard.png) -->
<!-- ![Campaign Wizard step 2 — AudiencePreview](docs/screenshots/frontend_campaigns_wizard.png) -->
<!-- ![Analytics — funnel + segment matrix](docs/screenshots/frontend_analytics.png) -->
<!-- ![Experiments — z-test calculator](docs/screenshots/frontend_experiments.png) -->
<!-- ![SHAP explain](docs/screenshots/frontend_shap_explain.png) -->
<!-- ![Recommendation API](docs/screenshots/recommendation_api.png) -->
<!-- ![Kafka UI](docs/screenshots/kafka_ui.png) -->
<!-- ![MLflow Dashboard](docs/screenshots/mlflow.png) -->
<!-- ![Airflow DAGs](docs/screenshots/airflow.png) -->

## Документация

- `docs/architecture/` — описание архитектуры
- `docs/adr/` — Architecture Decision Records
- `docs/screenshots/` — скриншоты UI и дашбордов

## Лицензия

TBD
