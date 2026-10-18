# db_migrations

Alembic-проект, управляющий схемой OLTP-базы PostgreSQL.

Реализует логическую модель из главы 2.2 диссертации (таблица 9):
`users`, `user_segments`, `cashback_campaigns`, `campaign_categories`,
`recommendations`, `user_consents`, `cashback_accruals`,
`ab_experiments`, `ab_variants`, `ab_assignments`, `ab_events`.

Также создаются enum-типы (`campaign_status`,
`recommendation_response_status`, `consent_status`, `accrual_status`,
`ab_experiment_status`, `ab_event_type`), индексы из главы 2.2 и
plpgsql-функция `calculate_cashback(p_campaign_id, p_mcc, p_amount)` —
прогрессивная шкала кэшбэка по полю `rate_tiers` (JSONB); с 009 она
учитывает MCC и минимум категории (до 009 — `calculate_cashback(p_campaign_id, p_amount)`).

## Деньги в БД (миграция 009, этап C)

Правила, от которых зависят деньги, живут в Postgres — одно определение на
все сервисы:

* `eligible_campaigns(p_user_id, p_mcc, p_amount, p_at)` — STABLE SQL-функция,
  **единственное** определение «кому положена кампания»: её вызывают
  recommendation_api (офферы, `p_amount = NULL`), transaction_listener
  (начисление) и `GET /campaigns/applicable/{user_id}`. Строка на каждую
  кампанию, где одновременно: `ACTIVE` и не в архиве (`archived_at IS NULL`);
  `start_date <= p_at < end_date`; MCC — категория кампании; сумма не ниже
  минимума категории (а если он не задан — минимума кампании); сегмент клиента
  в `target_segment_ids` (пустой массив — всем); **последнее** по `created_at`
  (при равенстве — по `consent_id`) согласие `personalised_cashback` —
  `GRANTED`; при `rfm_min`/`rfm_max` у клиента известен `rfm_score` и он в
  диапазоне (неизвестен — не положено); остаток бюджета > 0; при
  `daily_limit` — `spent_today < daily_limit`, где `spent_today` — начисления
  кампании с полуночи UTC дня `p_at` (или сегодняшнего дня, если `p_at` позже —
  `LEAST(p_at, now())`: в дневной лимит всегда входит день, на который ляжет начисление). Порядок: `cashback_rate DESC,
  start_date DESC` (затем `campaign_id`). Колонки: `campaign_id, mcc_code,
  cashback_rate, rate_tiers, min_transaction_amount, allowed_channels,
  budget_total, budget_spent, daily_limit, spent_today, start_date, end_date,
  name` (`min_transaction_amount` — действующий минимум категории).
* `calculate_cashback(p_campaign_id, p_mcc, p_amount)` — 0, если MCC не
  категория кампании или сумма ниже `COALESCE(минимум категории, минимум
  кампании)`; ставка и `rate_tiers` — как раньше.
* `cashback_accruals.recommendation_id` — `UNIQUE` + FK `ON DELETE RESTRICT`:
  принятый оффер оплачивается не больше одного раза. Старые (до 009) начисления
  backfill миграции связывает с их оффером; `NULL` остаётся только у двойных
  выплат старого кода.
* `uq_accrual_transaction` — частичный `UNIQUE (transaction_id) WHERE
  recommendation_id IS NOT NULL`: одна покупка оплачивается не больше одного раза,
  даже если к повторной доставке у клиента принят оффер другой кампании.
* FK начислений и рекомендаций на кампанию — `ON DELETE RESTRICT`: кампанию с
  историей не удалить (её архивируют), журнал начислений не теряется;
  `campaign_categories` удаляются каскадом.
* триггер `trg_campaign_status_guard` — переходы статуса строго по
  `services/campaign_manager/app/fsm.py` (DRAFT→ACTIVE, ACTIVE→PAUSED,
  PAUSED→ACTIVE, ACTIVE/PAUSED→COMPLETED); COMPLETED терминален. Иначе —
  ошибка SQLSTATE `23514`, `constraint_name = trg_campaign_status_guard`,
  сообщение `campaign status transition X -> Y is not allowed`.
* `users.rfm_score` — RFM-квинтиль 1…5 (5 — лучший), `NULL` — неизвестен.

## Структура

```
db_migrations/
├── alembic.ini
├── README.md
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 001_init_oltp.py
│       ├── …
│       └── 009_money_integrity.py
└── tests/                  # тесты миграций на настоящем Postgres
    ├── requirements.txt
    ├── conftest.py
    └── test_009_*.py
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

## Обновление работающей системы до 009 (этап C)

009 удаляет `calculate_cashback(campaign, amount)`: transaction_listener до
этапа C на такой ошибке сдвигает offset Kafka — покупка теряется навсегда.
Порядок:

1. Новый `transaction_listener` — и дождаться ухода старых подов. На схеме 008
   он не начисляет, а повторяет (SQLSTATE 42xxx — временная ошибка), offset
   стоит, покупки ждут в Kafka.
2. `alembic upgrade head` — в тихое окно. 009 сразу берёт ACCESS EXCLUSIVE на
   `cashback_accruals`, `recommendations`, `user_consents`,
   `cashback_campaigns`, `users` и держит до COMMIT (проверка FK, индексы,
   привязка старых начислений — секунды на миллион строк). Ждать блокировку
   дольше `lock_timeout` (5 с) миграция не станет: откатится целиком, без
   изменений — повторить, когда долгие транзакции закончатся.
3. `campaign_manager` и `recommendation_api` (им нужны функции и колонки 009).

Старые начисления (без `recommendation_id`) 009 привязывает к офферу, который
они оплатили: последний ACCEPTED того же клиента, кампании и MCC, принятый не
позже начисления; на оффер и на покупку — только самое раннее. Такой оффер
второй раз не платит.

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

# проверить функцию calculate_cashback (кампания, MCC, сумма)
docker exec cashback-postgres psql -U cashback -d cashback -c \
  "SELECT calculate_cashback(campaign_id, mcc_code, 12500) FROM campaign_categories LIMIT 1;"

# какие кампании положены клиенту за покупку 1500 ₽ в MCC 5411 сейчас
docker exec cashback-postgres psql -U cashback -d cashback -c \
  "SELECT campaign_id, cashback_rate, spent_today FROM eligible_campaigns(
     (SELECT user_id FROM users LIMIT 1), '5411', 1500, now());"
```

## Тесты миграций

`tests/` — pytest на настоящем Postgres: каждое правило 009 с положительным
и отрицательным случаем и upgrade → downgrade → upgrade на пустой БД и на БД
с данными (снимок каталога после downgrade совпадает со схемой 008).
Сервер — `MIGRATIONS_IT_POSTGRES_DSN` (пользователь с правом CREATEDB: тесты
создают и удаляют свои БД) или, без переменной, одноразовый
testcontainers-Postgres (нужен Docker); нет ни того, ни другого — тесты
пропускаются.

```bash
pip install -r db_migrations/tests/requirements.txt
MIGRATIONS_IT_POSTGRES_DSN=postgresql://cashback:cashback@127.0.0.1:5432/postgres \
  python -m pytest db_migrations/tests -q -rs
```

Разрешённые переходы триггера тесты берут из `campaign_manager/app/fsm.py`:
поменяли FSM — нужна миграция, обновляющая триггер.

UI: Adminer — http://localhost:8090 (server `postgres`, user/pass `cashback/cashback`).

## Соглашения

* Имена таблиц — snake_case, мн. число.
* Все PK — `UUID` со значением по умолчанию `uuid_generate_v4()`.
* Все timestamp-поля — `TIMESTAMPTZ` с дефолтом `now()`.
* Enum-типы создаются один раз и переиспользуются между ревизиями.
* В `downgrade()` обязательно удаляются индексы, таблицы и enum-типы
  в обратном порядке создания.
