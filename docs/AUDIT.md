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

Итог: **ядро (схема БД, начисление кэшбэка, ML-контракт, UI на моках) — рабочее**;
**«склейка» в прод-режиме — сломана**: главный бэкенд админки не принимает
запросы, часть аналитики падает, демо-данные не засеваются, CI/nightly
не дают сигнала. Всё это — точечные исправления, не архитектурные.

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

<!-- AUDIT-AGENTS -->

---

## 3. Чек-лист исправлений

Порядок — сверху вниз; каждый шаг проверяем тестом, который ловил бы баг.

### Этап A — «система запускается и CI даёт сигнал» (0.5–1 день)
- [ ] **B7** `db.py:31` — `request: Request`; интеграционный тест логина через `create_app()`
- [ ] **B8** `seed_demo_data.py:355` — `user=ch_user`; smoke-тест сидера (`--dry-run`/SQLite-заглушка CH)
- [ ] **B16** сидер: честная сводка + ненулевой exit-code при упавших шагах
- [ ] **B3** `ruff check --fix` под 0.6.9 (`recommendations.py:133`, `seed_demo_data.py:22,175,…`)
- [ ] **B2** триггеры CI на рабочую ветку / все push
- [ ] **B1** Kafka-образ без Bitnami (`apache/kafka` KRaft) + helm-зависимости на OCI/альтернативы
- [ ] **B4** развести ETL и Airflow по разным зависимостям
- [ ] **B5** закрепить версии recommendation_api (lock-файл)
- [ ] Прогнать nightly-smoke вручную (`workflow_dispatch`) — зелёный

### Этап B — «цифры правдивые» (1 день)
- [ ] **B9** `analytics.py:324,427-428,487-488` — `CAST(... AS ...)`, `make_interval`
- [ ] Интеграционные тесты на каждый SQL аналитики (testcontainers/PG в CI)
- [ ] **B11** `_delta_pct` → `null` при малой базе; подпись периода в `UI.tsx:82`
- [ ] **B10** различать «нет данных» и «ошибка» на Dashboard/Analytics; демо только в mock-режиме

### Этап C — «безопасность клиента» (1–2 дня)
- [ ] **B12** аутентификация mobile_api, `user_id` из токена, проверка владельца
- [ ] **B13** атомарный условный UPDATE для отклика; 409/410; тесты на просрочку и повторный отклик

### Этап D — «надёжность и качество кода» (2–3 дня)
- [ ] **B14** SSE-мост: переподключение к Kafka с backoff
- [ ] **B17** mypy/format блокирующие; nightly без mlflow
- [ ] **B15** снять `@ts-nocheck` со страниц, eslint = 0 ошибок, eslint в CI

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
