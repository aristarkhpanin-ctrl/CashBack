# db_migrations

Alembic-проект, управляющий схемой OLTP-базы PostgreSQL.

Реализует логическую модель из главы 2.2 диссертации (таблица 9):
`users`, `user_segments`, `cashback_campaigns`, `campaign_categories`,
`recommendations`, `user_consents`, `cashback_accruals`,
`ab_experiments`, `ab_variants`, `ab_assignments`, `ab_events`.

Также создаются enum-типы (`campaign_status`,
`recommendation_response_status`, `consent_status`, `accrual_status`,
`ab_experiment_status`, `ab_event_type`), индексы из главы 2.2 и
plpgsql-функция `calculate_cashback(p_campaign_id, p_amount)` —
прогрессивная шкала кэшбэка по полю `rate_tiers` (JSONB).

## Структура

```
db_migrations/
├── alembic.ini
├── README.md
└── migrations/
    ├── env.py
    ├── script.py.mako
    └── versions/
        └── 001_init_oltp.py
```

## Запуск миграций

### Через Make (рекомендуется)

```bash
docker compose up -d postgres clickhouse
make migrate           # применит и Postgres, и ClickHouse миграции
```

### Вручную из этой папки

```bash
# 1. виртуальное окружение
python -m venv .venv && source .venv/bin/activate
pip install "alembic==1.13.2" "SQLAlchemy==2.0.30" "psycopg2-binary==2.9.9"

# 2. накатить
export POSTGRES_DSN="postgresql+psycopg2://cashback:cashback@localhost:5432/cashback"
alembic upgrade head

# 3. откатить
alembic downgrade -1
```

DSN можно задать переменной окружения `POSTGRES_DSN`; иначе берётся
значение `sqlalchemy.url` из `alembic.ini`.

## Создание новой миграции

```bash
alembic revision -m "add_loyalty_tiers"
# редактируем сгенерированный файл в migrations/versions/
alembic upgrade head
```

## Проверка после применения

```bash
# список таблиц
docker exec cashback-postgres psql -U cashback -d cashback -c "\dt"

# проверить функцию calculate_cashback
docker exec cashback-postgres psql -U cashback -d cashback -c \
  "SELECT calculate_cashback(campaign_id, 12500) FROM cashback_campaigns LIMIT 1;"
```

UI: Adminer — http://localhost:8090 (server `postgres`, user/pass `cashback/cashback`).

## Соглашения

* Имена таблиц — snake_case, мн. число.
* Все PK — `UUID` со значением по умолчанию `uuid_generate_v4()`.
* Все timestamp-поля — `TIMESTAMPTZ` с дефолтом `now()`.
* Enum-типы создаются один раз и переиспользуются между ревизиями.
* В `downgrade()` обязательно удаляются индексы, таблицы и enum-типы
  в обратном порядке создания.
