# Аудит CashBack — состояние, баги, план улучшений

Дата: 2026-10-07 · ветка `claude/setup-cashback-monorepo-AszKM` · коммит `a54ca75`

Документ — результат проверки проекта после фаз 1–27: что реально работает,
что сломано, и чек-лист исправлений в порядке приоритета. Каждый баг
в разделе «Подтверждено» воспроизведён живым прогоном (PostgreSQL 16 +
Redis + реальные сервисы + Playwright по реальному UI), а не только чтением кода.

---

## 1. Как проверялось

| Проверка | Результат |
|---|---|
| Alembic-миграции `upgrade head` → `downgrade base` → `upgrade head` на чистой БД | ✅ проходит |
| Unit-тесты всех сервисов (`pytest` по каждому `services/*/tests/unit`) | ✅ зелёные |
| Frontend: `tsc --noEmit`, `vite build`, Playwright e2e (36 сценариев, mock-режим) | ✅ 36/36 |
| Frontend: `eslint` | ❌ 15 ошибок |
| `ruff check` той версии, что закреплена в CI (0.6.9) | ❌ падает (см. B3) |
| Сид демо-данных `scripts/seed_demo_data.py` | ❌ падает (B8) |
| campaign_manager как HTTP-сервис (uvicorn + реальная БД) | ❌ 36 эндпоинтов отдают 422 (B7) |
| campaign_manager с локальным патчем B7 + реальный UI в браузере | ⚠ работает, но аналитика 500 (B9) |
| Денежный путь начисления (`AccrualEngine`): идемпотентность + гонка 20 параллельных начислений на один бюджет | ✅ корректно: без двойных списаний, бюджет не уходит в минус |
| Контракты stub-ов фронта vs реальные ответы API (сравнение форм JSON) | ✅ совпадают |
| mobile_api: баланс/история/отклик на оффер | ⚠ работает, но без авторизации (B12) |
| GitHub Actions: ci.yml / pr.yml / nightly-smoke.yml | ❌ CI никогда не запускается, nightly падает 83/83 (B1, B2) |

Итог: **ядро (схема БД, логика начисления в `AccrualEngine`, ML-контракт, UI на моках) — рабочее**;
**«склейка» в прод-режиме — сломана**: главный бэкенд админки не принимает
запросы (B7), transaction_listener не стартует из-за zstd (B18) — значит,
кэшбэк в живом стеке не начисляется, часть аналитики падает, демо-данные не
засеваются, CI/nightly не дают сигнала. Отдельно — серьёзные дыры безопасности
(подделка ADMIN-токена B19, пароль `admin` B20, IDOR в mobile_api B12) и
бизнес-логика кампаний, которая сохраняется, но не применяется (таргетинг,
даты, дневной лимит, минимумы по категориям — B21–B26). Большинство исправлений
точечные; переработки требуют только резервы бюджета (B23) и единая
функция «кому положена кампания» (B26).

---

## 2. Подтверждённые баги

Приоритеты: **P0** — блокирует работу системы/демо, **P1** — неверные данные
или дыра безопасности, **P2** — качество/надёжность, **P3** — косметика.

### P0 — блокеры

**B7. campaign_manager: все эндпоинты с БД отвечают 422**
- `services/campaign_manager/app/db.py:31` — `async def get_session_dep(request)`
  без аннотации типа. FastAPI считает `request` обязательным query-параметром.
- Сценарий: `POST /v1/auth/login` (и ещё 35 эндпоинтов) → `422 {"loc":["query","request"],"msg":"Field required"}`.
  Админка на реальном бэкенде не логинится вообще. Unit-тесты не ловят,
  потому что подменяют зависимость через `dependency_overrides`.
- Исправление: `from starlette.requests import Request` и `request: Request`.
  Добавить интеграционный тест «логин через настоящий `create_app()`».

**B8. Сидер демо-данных падает на шаге ClickHouse**
- `scripts/seed_demo_data.py:355` вызывает `ch_exec(ch_user=..., ...)`,
  а сигнатура (`:134`) — `ch_exec(url, user, password, db, sql, data=None)`.
- Сценарий: `python scripts/seed_demo_data.py` → на шаге `[4/7]` (`compute_rfm`)
  `TypeError: unexpected keyword argument 'ch_user'`; исключение не перехвачено, поэтому
  шаги 5–7 (Redis-признаки, recommendations, A/B, cashback_accruals) не выполняются вовсе.
  В БД остаются только кампании и пользователи → дашборды пустые/демо.
- Исправление: `user=ch_user` (или позиционно, как в `:294/:363`). Добавить smoke-тест сидера.

**B1. Nightly smoke падает всегда — образ Kafka удалён с Docker Hub**
- `docker-compose.yml:122` `bitnami/kafka:3.7` (+ `:146` volume `/bitnami/kafka`, `:148` healthcheck `/opt/bitnami/...`);
  `helm/cashback/Chart.yaml:27,32,37,42` — `charts.bitnami.com`.
- Bitnami убрал публичный каталог образов/чартов → `docker compose up` не может
  стянуть Kafka, все 83 nightly-прогона красные.
- Исправление: `apache/kafka:3.7.x` (KRaft, другие env/пути) или `bitnamilegacy/kafka:3.7`
  как временная мера; чарты — OCI `oci://registry-1.docker.io/bitnamicharts` или
  отдельные чарты (CloudNativePG/Strimzi/official redis).

**B2. CI не запускается ни на одном push**
- `.github/workflows/ci.yml:16,18`, `pr.yml:13` — триггеры только на `main`,
  а ветки `main` в репозитории нет (работа идёт в `claude/setup-cashback-monorepo-AszKM`).
- Исправление: добавить рабочую ветку / `branches: ["**"]` для push или создать `main`.

**B3. Lint-джоба CI упадёт сразу после включения CI**
- CI закрепляет `ruff==0.6.9`; с ней падают:
  `services/recommendation_api/app/api/recommendations.py:133` (UP038 `isinstance(ev, (list, tuple, np.ndarray))`),
  `scripts/seed_demo_data.py:22` (I001), `:175,233,276,413,486,571` (UP017 `timezone.utc`).
- Шаг lint блокирующий, а `test`, `frontend-e2e`, `api-types-drift` зависят от него → вся цепочка не стартует.
- Исправление: `ruff check --fix` под 0.6.9 (или обновить пин и `ignore` UP038).

**B4. Зависимости ETL не резолвятся**
- `services/etl/pyproject.toml:12,16` — `SQLAlchemy[asyncio]>=2.0.0` + `apache-airflow==2.9.0`
  (Airflow 2.9 требует `sqlalchemy<2.0`) → `pip install` падает с ResolutionImpossible.
- Исправление: вынести DAG-и Airflow в отдельный образ/extra (`[airflow]`), либо Airflow ≥ 2.10 + проверить constraints-файл Airflow.

**B5. recommendation_api: `pip install` — resolution-too-deep**
- `services/recommendation_api/pyproject.toml` — тяжёлые незакреплённые зависимости
  (mlflow, shap, faiss-cpu, opentelemetry-*); Dockerfile ставит `pip install --prefix=/install .`.
- Сборка образа нестабильна/падает. Исправление: верхние границы версий + lock-файл (`uv pip compile`/`pip-tools`).

### P1 — неверные данные / безопасность

**B9. Аналитика: три эндпоинта стабильно 500**
- `services/campaign_manager/app/api/analytics.py:427-428` и `:487-488`:
  `(:campaign_id::uuid IS NULL OR ...)`, `(:deciles::int[] IS NULL OR ...)` —
  SQLAlchemy `text()` не распознаёт bind-параметр, за которым сразу идёт `::`,
  в Postgres уходит буквальный `:campaign_id::uuid` → syntax error.
  Ломаются daily-trend и channels.
- `:324` `(:periods || ' months')::interval` — asyncpg строго типизирует,
  `int || text` → `DataError` → cohort-retention 500.
- Исправление: `CAST(:campaign_id AS uuid)` / `CAST(:deciles AS int[])`;
  `make_interval(months => :periods)`. Интеграционные тесты на каждый SQL аналитики.

**B10. UI выдаёт ошибку бэкенда за «данных ещё нет»**
- `frontend/src/components/cashback/pages/Analytics.tsx:678` — при HTTP 500 показывается
  «Канал доставки ещё не накопился в БД — ниже модельные (демо) данные»;
  `Dashboard.tsx:296-313` аналогично подставляет демо-ряды.
- Пользователь видит правдоподобные цифры вместо ошибки. Исправление: различать
  `404/пусто` (→ «нет данных») и `5xx/сеть` (→ явная ошибка + retry), демо-фолбэк только в mock-режиме.

**B11. Абсурдные тренды KPI (▲1286%, ▲1940%)**
- `analytics.py:57` `_delta_pct` сравнивает текущий период с почти пустым предыдущим
  (сид кладёт всё в последние недели), а `UI.tsx:82` подписывает «vs прошлый месяц»
  независимо от выбранного периода.
- Исправление: при маленькой базе (`prev < N`) отдавать `null` → «—»;
  подпись брать из выбранного периода; ограничить отображение (например, «>999%»).

**B12. mobile_api: IDOR — любой может читать и менять чужие данные**
- `services/mobile_api/app/api/mobile.py:102,221,322,387` —
  `/v1/mobile/cashback/balance/{user_id}`, `/history/{user_id}`, `/recommendations/{id}/respond`
  без какой-либо аутентификации; `user_id`/`recommendation_id` берутся из URL.
- Сценарий: перебор UUID → чужой баланс и история; отклик на чужой оффер.
- Исправление: JWT/сессия клиента, `user_id` из токена, проверка владельца рекомендации.

**B13. Отклик на просроченный оффер и смена решения задним числом**
- `mobile.py:247-272` — нет проверки `expires_at < now()` и текущего `response_status`:
  истёкший оффер принимается (TTL принудительно `max(60, …)` → ключ живёт 60 с,
  начисление может пройти), `DECLINED → ACCEPTED` и повторные ACCEPTED разрешены
  (повторные Kafka-события `offer.accepted`).
- Исправление: `UPDATE … WHERE recommendation_id=:rid AND response_status='PENDING' AND expires_at > now() RETURNING …`;
  409/410 при конфликте.

### P2 — надёжность и качество

**B14. SSE-мост campaign_manager «умирает» навсегда, если Kafka поднялась позже сервиса**
- `services/campaign_manager/app/events.py:96-100` — при ошибке `consumer.start()`
  цикл просто `return`, без ретраев; незакрытый consumer → предупреждение `Unclosed AIOKafkaConsumer`.
- Исправление: цикл переподключения с backoff, `consumer.stop()` в `finally` и при неудачном старте.

**B15. Фронтенд без проверки типов**
- 12 файлов UI с `// @ts-nocheck` (App.tsx, Layout, UI, Icon, все страницы) — `tsc` зелёный формально;
  `eslint` — 15 ошибок.
- Исправление: снимать `@ts-nocheck` постранично, типизировать через `src/api/types.gen.ts`; eslint в CI.

**B16. Итоговая сводка сидера врёт при пропущенных шагах**
- `scripts/seed_demo_data.py:731-748` — сводка печатает «~N транзакций за 90 дней» даже при
  `--skip-transactions`; ошибки `admin_users`/A/B (`:684,:717`) только логируются, exit-code всё равно 0.
- Исправление: собирать список пропущенных/упавших шагов, печатать его в сводке, exit-code ≠ 0 при ошибках.

**B17. CI маскирует ошибки**
- `.github/workflows/ci.yml`: mypy `|| true`, format-check `continue-on-error`;
  nightly поднимает лишнее (mlflow тянется через `depends_on`).
- Исправление: сделать mypy блокирующим хотя бы для campaign_manager/transaction_listener; сузить nightly.

### Проверено — работает корректно
- Начисление кэшбэка (`transaction_listener/app/accrual/engine.py`): `calculate_cashback` →
  `SELECT … FOR UPDATE` → `INSERT … ON CONFLICT DO NOTHING` → списание бюджета; Kafka/Redis после коммита.
  20 параллельных начислений на одну кампанию — без двойного списания и перерасхода.
- Миграции вверх/вниз, unit-тесты всех сервисов, frontend build/e2e, совпадение stub-контрактов с API.

---

## 2b. Найдено статическим аудитом (campaign_manager, recommendation_api)

Источник — построчный разбор кода по зонам. Пометка ✔ — дополнительно
воспроизведено запуском; остальное подтверждено по коду (файл:строка + сценарий).

### P0 — критично

**B18. Kafka-продюсеры с `compression_type="zstd"` не создаются — нет `cramjam`** ✔
- `services/transaction_listener/app/main.py:166`, `recommendation_api/app/main.py:159`, `mobile_api/app/main.py:79`;
  во всех `pyproject.toml` только `aiokafka>=0.11.0` (ставится 0.14, zstd в ней — через опциональный `cramjam`).
- Проверено: `AIOKafkaProducer(compression_type='zstd')` → `RuntimeError: Compression library for zstd not found`.
- Последствия: **transaction_listener падает при старте → кэшбэк не начисляется вообще**; rec_api и mobile_api
  глотают ошибку и молча не публикуют `recommendations.created` / `offer.accepted` → нет уведомлений, нет SSE.
- Исправление: `aiokafka[zstd]` во всех сервисах-продюсерах и консьюмерах (или `gzip`); smoke-тест конструктора продюсера.

**B19. Подделка ADMIN-токена: пустой/публичный JWT-секрет** ✔
- `helm/cashback/values.yaml:63` `jwtSecret: ""` → `templates/secret.yaml:16`; `docker-compose.yml:442,502,579`
  `${JWT_SECRET:-dev-secret-change-me}`; дефолты в `config.py` трёх сервисов.
- Проверено: python-jose подписывает и принимает HS256 с пустым ключом. Любой собирает `{role: ADMIN, type: access}`
  и вызывает `POST /auth/users`, правит RBAC, бюджеты. Один ключ на все три сервиса (mobile_api клиентский).
- Исправление: валидатор Settings (непустой, ≥32 байт, не из списка дефолтов), `required` в helm, убрать fallback в compose; `aud`/`iss` на сервис.

**B20. Встроенный админ `admin@bank.ru` / `admin` в миграции** ✔
- `db_migrations/migrations/versions/002_admin_users.py:25,60` — bcrypt-хэш пароля `admin` (проверено),
  «смена при первом входе» не реализована, на `/auth/login` нет rate-limit.
- Исправление: пароль из секрета/env при миграции (или случайный, печатается один раз), флаг `must_change_password`, throttling логина.

### P1 — деньги и данные

| # | Где | Суть | Исправление |
|---|---|---|---|
| B21 | `campaigns.py:159`, `001_init_oltp.py:298-309` (`calculate_cashback`), `recommendations.py:180` | Минимумы по категориям (`campaign_categories.min_transaction_amount`) сохраняются, но нигде не применяются; фронт ставит общий минимум = наименьшему → кэшбэк платится ниже порога категории | передавать MCC в `calculate_cashback`, `COALESCE(cc.min, c.min)`; так же в BRE R2 |
| B22 | `scheduling.py:50-55,153-156,186-193` | Авто-пауза по `daily_limit` мертва: запрос в ClickHouse к таблице `cashback_accruals`, которой там нет (начисления только в PG); ошибка глотается. Плюс пауза односторонняя — на следующий день не снимается | считать дневной расход в PG, а лучше — проверять `daily_limit` внутри `AccrualEngine` под `FOR UPDATE`; `pause_reason` + авто-resume |
| B23 | `campaigns.py:429-471` | `POST /campaigns/{id}/budget/check` списывает `budget_spent` навсегда: Redis-«холд» никто не читает и не освобождает, нет идемпотентности (новый `uuid4` на каждый вызов), при выплате деньги списываются второй раз | таблица резервов (idempotency_key UNIQUE, expires_at, state) + confirm/release, либо убрать эндпоинт |
| B24 | `campaigns.py:366-375`, `001_init_oltp.py:188` | Удаление PAUSED/COMPLETED кампании каскадно удаляет журнал начислений `cashback_accruals` (включая PENDING — долг перед клиентами) | hard-delete только DRAFT без начислений, иначе архив; FK → `ON DELETE RESTRICT` |
| B25 | `campaigns.py:392-397`, `recommendations.py:188`, `engine.py:135` | Окно дат кампании не соблюдается: `activate` до `start_date` сразу запускает выдачу и начисления; после `end_date` платим до следующего тика планировщика | проверка дат в `activate` и `now() BETWEEN start_date AND end_date` в rec_api и accrual |
| B26 | `recommendations.py:175-191`, `campaigns.py:236-263` | Таргетинг не работает: сегменты, согласие `personalised_cashback`, `rfm_min/max` проверяются только в неиспользуемом `/campaigns/applicable`; там же отзыв согласия игнорируется (берётся любая старая GRANTED-запись) | единая SQL-функция/представление «eligible campaign» для rec_api, accrual и applicable; последнее согласие через `LATERAL … ORDER BY created_at DESC LIMIT 1` |
| B27 | `frontend/src/shared/api/live.ts:458-471`, `recommendations.py:335-343`, `recommendation_api/app/security.py:44-49` | Страница «ML-объяснения» вызывает боевой `GET /recommendations/{user_id}`: создаёт PENDING-строки, шлёт `recommendations.created` → клиенту уходят push/email, сжигаются anti-fatigue и дневной лимит. Принимается токен любой роли | отдельный `GET /recommendations/{id}/explain` без побочных эффектов, только с правом `analytics` |
| B28 | `bre/rules/anti_fatigue.py:39`, `frequency_gate.py:28-38`, `engine.py:25-33`, `recommendations.py:281` | Правила R3/R4 пишут в Redis до решения R5/R6: отклонённый кандидат помечен «показан» на 14 дней; кандидаты без кампании съедают лимит 5/сутки → пустая выдача на 24 ч; GET→SETEX не атомарны (дубли пушей) | правила — чистые проверки; побочные эффекты только для финального top-K (`SET NX EX`, `INCR`+`EXPIRE`); отбрасывать кандидатов без кампании до BRE |
| B29 | `recommendations.py:370-405`, `mobile_api/app/api/mobile.py:153-183,251-258`, `scheduling.py:89-99` | Каждая рекомендация сохраняется дважды (rec_api с `model_version` и mobile_api без); отклик обновляет только мобильную строку → online-CTR по версии модели и holdout всегда пустые, таблица удваивается | сохранять в одном месте; rec_api возвращает `recommendation_id`, mobile переиспользует |
| B30 | `recommendations.py:218-223`, `mobile_api/app/clients/recommendation_client.py:96-118` | Новый клиент без признаков → 404; мобильный клиент ретраит 4xx и считает их отказами → 5 новых клиентов открывают circuit breaker для всех на 30 с | cold-start через популярные MCC вместо 404; ретраи и breaker только на 5xx/сеть |
| B31 | `ml_training/app/data_loader.py:121-125`, `recommendations.py:79` | Training/serving skew: модель учится на `model_score` и `age_seconds` из таблицы рекомендаций, а при выдаче оба = 0 | убрать их из признаков; тест паритета признаков fingerprint ↔ feature store |
| B32 | `recommendations.py:271`, `bre/rules/ml_rate_cap.py:84-86` | R7: лимиты ставок по сегментам не применяются никогда — `segment_id` нет в feature store (он в PG `users`) | подтягивать сегмент из PG или класть в признаки ETL |

**B33. Аналитика считает неверно (помимо B9/B11)** — `services/campaign_manager/app/api/analytics.py`
- [ ] `:87-124` воронка смешивает единицы (пользователи → строки рекомендаций → строки начислений) → отрицательный drop-off и «400% от принявших»; считать `COUNT(DISTINCT user_id)` на каждом шаге
- [ ] `:103`, `:481` EXPIRED (никто не ответил, ~20% строк) считается «открыто» → `IN ('ACCEPTED','DECLINED','SNOOZE')`
- [ ] `:123` этап `cashback_paid` всегда 0: статус PAID никто не ставит (кроме сидера) — нужен шаг расчёта PENDING→PAID или переименовать этап
- [ ] `:485` каналы считаются только по ответившим (`channel` пишется в момент ответа) → open rate всегда 100%; сохранять канал при отправке
- [ ] `:247` `has_data = spent > 0 and avg_ctr > 0` → должно быть `or` (по контракту API.md)
- [ ] `:316` когорты: `date_part('month', age(...))` сворачивает месяцы ≥12 в 0–11 → `years*12 + months`
- [ ] `:395-398` `conversion_rate` — копия формулы `ctr`
- [ ] `:219-220` при отсутствии ACTIVE/PAUSED кампаний KPI считаются по всем историческим кампаниям

**B34. A/B-тесты** — `services/campaign_manager/app/api/ab_testing.py`
- `:310-316` контроль/тест выбираются по алфавиту имени → знак эффекта переворачивается, UI может советовать «катить в прод» проигравший вариант;
  вердикт «significant» не учитывает направление.
- `:326-334` успехи = число событий CONVERSION, а не конвертированных назначений → доля >1 → `ValueError` в `math.sqrt` → 500.
- `:223-272` `assign` без проверки роли и статуса эксперимента, гонка → `IntegrityError` 500; дубль имени эксперимента → 500.

**B35. Авторизация и RBAC campaign_manager**
- Матрица прав проверяется только во фронте для `analytics`, `users`, `dashboard`, `campaigns_view`: роутеры `analytics.py:35-38`,
  `ab_testing.py:36-39`, `ml_explain.py:20-23` требуют лишь логин (MARKETER по умолчанию без `analytics` получает все данные и UUID клиентов).
- `security.py:121-126` роль берётся из токена без проверки БД: разжалованный/деактивированный админ 15 минут может создать себе нового ADMIN.
- `auth.py:86-92` refresh-токены не ротируются и не отзываются (смена пароля не помогает).
- `auth.py:67-68` bcrypt выполняется в event loop (блокирует весь процесс на ~0.3–0.6 с), неизвестный email проверяется вдвое дольше → перечисление email.
- `rbac.py:39-58` кэш прав на процесс (30 с, другие реплики не узнают об изменениях); `roles.py:72-86` read-merge-write без блокировки → теряются переключения.

**B36. SSE `/events/stream`** (дополняет B14)
- `api/events.py:19` без аутентификации, CORS `*` с credentials → любой сайт читает расход по кампаниям в реальном времени, нет лимита подключений.
- `events.py:61` общий `group_id` для всех реплик → события получает только один под; `docker-compose.yml:482` campaign-manager не ждёт Kafka.
- открытые SSE-соединения блокируют graceful shutdown uvicorn (`Dockerfile:46` без `--timeout-graceful-shutdown`).

### P2 — надёжность

- [ ] **B37** PATCH кампании: явные `null` → 500 (`campaigns.py:316-330`); `rfm_min > rfm_max` коммитится, после чего **весь** `GET /campaigns` отдаёт 500 (`schemas.py:117-125`); `min_tx_amounts` без `mcc_codes` молча игнорируется (`:318`); список не возвращает минимумы по категориям → редактирование черновика их затирает (`:191-199`); смешанные naive/aware даты → 500 (`schemas.py:47-51`); нет `max_digits` у сумм (`schemas.py:157`); `PATCH /auth/users` с `null` → 500 (`auth.py:164-178`)
- [ ] **B38** Гонки статусов: `patch_status` и планировщик пишут без compare-and-set → COMPLETED-кампания может «ожить» (`campaigns.py:388-397`, `scheduling.py:164-193`)
- [ ] **B39** Kill-switch ML-лимитов «открыт при сбое»: ошибка публикации в Redis глотается, PUT отвечает 200; republish — последний шаг общего try в тике (`ml_limits.py:125-128`, `scheduling.py:219-226`)
- [ ] **B40** Зависимости при старте: ClickHouse-клиент campaign_manager остаётся `None` навсегда → `/health/ready` 503 (`main.py:81-90`); readiness rec_api требует MLflow и ClickHouse (`health.py:193-209`); один CH-клиент с общим session_id из многих потоков → `SESSION_IS_LOCKED` → 404 (`feature_store.py:70-79`)
- [ ] **B41** Реестр моделей: без `training_fingerprint.json` берутся ключи признаков как попало → shape error на каждом запросе (`recommendations.py:245`); после rollback holdout получает только что откаченную модель (`model_registry.py:172-178`); генератор кандидатов берёт последний run MLflow без фильтра `FINISHED` и не обновляется (`main.py:140-150`)
- [ ] **B42** Мелочи rec_api: в событии Kafka `campaign_name` всегда «Cashback offer» и нет `recommendation_id` (`recommendations.py:177-185,418`); `DISTINCT ON` берёт одну кампанию на MCC до BRE (`:177`); R6 делает `FOR UPDATE` на горячем пути без пользы (`budget_reservation.py:23-63`); `_to_prob` трактует log-odds из [0,1] как вероятность (`:126`); SKIP-семантика движка расходится с API (`:281`)

<!-- AUDIT-AGENTS-2 -->

---

## 3. Чек-лист исправлений

Порядок — сверху вниз; каждый пункт закрываем вместе с тестом, который ловил бы баг.

### Этап A — «система запускается, деньги начисляются, CI даёт сигнал» (1 день)
- [ ] **B7** `db.py:31` — `request: Request`; интеграционный тест логина через `create_app()`
- [ ] **B18** `aiokafka[zstd]` во всех сервисах (или gzip); smoke-тест создания продюсера
- [ ] **B8** `seed_demo_data.py:355` — `user=ch_user`; smoke-тест сидера
- [ ] **B16** сидер: честная сводка + ненулевой exit-code при упавших шагах
- [ ] **B3** `ruff check --fix` под 0.6.9
- [ ] **B2** триггеры CI на рабочую ветку / все push
- [ ] **B1** Kafka без Bitnami (`apache/kafka` KRaft) + helm-зависимости на OCI
- [ ] **B4 / B5** развести ETL и Airflow; lock-файл для recommendation_api
- [ ] Ручной прогон nightly-smoke — зелёный

### Этап B — «закрыть дыры безопасности» (1–2 дня)
- [ ] **B19** запрет пустого/дефолтного JWT-секрета (Settings-валидатор, `required` в helm, без fallback в compose)
- [ ] **B20** bootstrap-админ без пароля `admin`, принудительная смена, rate-limit логина
- [ ] **B35** `require_permission` на analytics / ab_testing / ml_explain / users / campaigns_view; проверка `is_active`/версии токена для привилегированных действий; ротация refresh; bcrypt в threadpool
- [ ] **B36** SSE только с токеном, CORS на origin фронта, группа Kafka на под
- [ ] **B12 / B13** аутентификация mobile_api; атомарный условный отклик (409/410)
- [ ] **B27** read-only `/explain` для админки, без записи и Kafka
- [ ] Тест: обход `app.routes` — у каждого непубличного маршрута есть право

### Этап C — «деньги считаются верно» (2 дня)
- [ ] **B24** запрет каскадного удаления начислений (`ON DELETE RESTRICT`, архив вместо удаления)
- [ ] **B23** резервы бюджета с идемпотентностью и освобождением (или удалить эндпоинт)
- [ ] **B22** `daily_limit` проверять в `AccrualEngine` под блокировкой; авто-resume
- [ ] **B25** соблюдать окно дат в activate / rec_api / accrual
- [ ] **B21** минимумы по категориям в `calculate_cashback` и BRE R2
- [ ] **B26** единая функция eligibility (сегмент, согласие — последнее, RFM, даты)
- [ ] **B38** compare-and-set для смены статуса; триггер «из COMPLETED нельзя»
- [ ] **B39** kill-switch ML-лимитов: ошибка публикации → 503, отдельная задача republish

### Этап D — «цифры правдивые» (1–2 дня)
- [ ] **B9** `CAST(... AS ...)`, `make_interval` в аналитике
- [ ] **B33** воронка на `COUNT(DISTINCT user_id)`, EXPIRED ≠ opened, этап PAID, каналы, `has_data` через `or`, когорты, `conversion_rate`
- [ ] **B11 / B10** тренды при малой базе → «—»; ошибка ≠ «нет данных»
- [ ] **B34** явная роль control в A/B, `COUNT(DISTINCT assignment_id)`, проверки assign
- [ ] **B29** одна запись рекомендации на показ (с `model_version`)
- [ ] Интеграционные тесты каждого SQL аналитики на реальном PG

### Этап E — «рекомендации ведут себя корректно» (1–2 дня)
- [ ] **B28** побочные эффекты BRE только для итогового top-K, атомарно; кандидаты без кампании — до BRE
- [ ] **B30** cold-start без 404; breaker только на 5xx
- [ ] **B31** убрать `model_score`/`age_seconds` из обучения; тест паритета признаков
- [ ] **B32** сегмент пользователя для R7
- [ ] **B41 / B42** fingerprint → колонки из модели; holdout после rollback; фильтр FINISHED; `campaign_name` и `recommendation_id` в событии

### Этап F — «надёжность и качество кода» (2–3 дня)
- [ ] **B14 / B40** переподключение к Kafka и ClickHouse; readiness только по жёстким зависимостям
- [ ] **B37** валидация PATCH (null, rfm, min_tx, даты, суммы)
- [ ] **B17** mypy/format блокирующие; nightly без mlflow
- [ ] **B15** снять `@ts-nocheck`, eslint = 0, eslint в CI

---

## 4. План улучшений (после исправлений)

1. **Интеграционный контур в CI.** Сейчас unit-тесты подменяют БД/зависимости и
   поэтому пропустили B7 и B9. Нужен job «PG + Redis + сервисы через `create_app()`»
   и e2e Playwright против реального бэкенда (а не только mock-режим) — те же
   36 сценариев в режиме `VITE_API_MODE=live`.
2. **Единый контракт API.** `types.gen.ts` уже генерируется — подключить его к
   страницам вместо `@ts-nocheck`, а `api-types-drift` сделать обязательным.
3. **Честная деградация UI.** Ввести общий хук `useLiveOrDemo` с явными
   состояниями `loading / empty / error / demo`, бейдж «демо» только в mock-режиме.
4. **Безопасность.** Аутентификация mobile_api, rate-limit на логин админки,
   секреты из `.env` → Kubernetes Secrets/External Secrets, аудит-лог изменений кампаний (уже есть RBAC — связать с ним).
5. **Воспроизводимые сборки.** Lock-файлы для всех Python-сервисов, закреплённые
   теги образов, Renovate/Dependabot — чтобы история с Bitnami не повторилась.
6. **Наблюдаемость денег.** Алерты на `accrual_engine_total{outcome="budget_exhausted"|"duplicate"}`,
   сверка `SUM(cashback_accruals)` ↔ `budget_spent` ночным джобом.
7. **Демо-сценарий для защиты.** Один скрипт `make demo`: compose up → миграции →
   сид → проверка health → открытие UI; сам скрипт — часть nightly.
