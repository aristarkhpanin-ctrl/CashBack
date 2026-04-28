# CashBack — Система персонализированного кэшбэка

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
make up      # поднять инфраструктуру
make ps      # статус контейнеров
make logs    # логи
make seed    # заполнить тестовыми данными
make test    # тесты
make down    # остановить
make clean   # удалить контейнеры и volumes
```

## Скриншоты

> TODO: разместить скриншоты в `docs/screenshots/` и сослаться отсюда.

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
