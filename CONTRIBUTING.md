# Contributing

Спасибо за интерес к проекту! Этот репозиторий — академический артефакт
магистерской диссертации, но мы рады разумным улучшениям.

## Git flow

* `main` — защищённая ветка; PR требует ревью кодовладельца
  (`CODEOWNERS`) и зелёного CI.
* Ветки фич — `feat/<short-slug>`, исправлений — `fix/<short-slug>`.
* Никаких прямых пушей в `main`.

## Conventional commits

Используем [conventional-commits.org](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>
```

Допустимые `type`'ы: `feat`, `fix`, `docs`, `style`, `refactor`,
`perf`, `test`, `chore`, `build`, `ci`. Примеры:

```
feat(reco): add SHAP top-20 to RecommendationResponse
fix(bre): R3 anti-fatigue must not crash on stale Redis key
test(etl): bump validator coverage to 100%
docs(adr): add 0007 lightgbm decision record
ci: pin ruff to 0.6.9
```

Группируйте мелкие изменения в один коммит — каждый коммит должен
проходить тесты.

## Setup

```bash
git clone https://github.com/aristarkhpanin-ctrl/CashBack.git
cd CashBack
cp .env.example .env

# Точка входа в локальный стек.
make up

# Pre-commit (ruff/mypy/prettier/shellcheck) запустится на каждый коммит.
pip install pre-commit
pre-commit install
```

## Запуск тестов

```bash
make test-unit          # unit-тесты по сервисам с coverage gates ≥85%
make test-integration   # 47 интеграционных тестов (нужен Docker)
make test-e2e           # 9-шаговый E2E (требует поднятый стек)
make test-load          # Locust → test-reports/load-<TS>.html
make test-all           # всё последовательно
```

Per-service:

```bash
cd services/recommendation_api
python -m pytest tests/unit -q --cov-config=pyproject.toml --cov
```

## Качество кода

Лидируем `ruff` (lint + format), `mypy` (best-effort), `prettier`
(frontend), `shellcheck` (`scripts/`). См. `.pre-commit-config.yaml`.

```bash
ruff check services scripts tests db_migrations
ruff format services scripts tests db_migrations
```

## Pull request

1. Добавьте тесты для новой логики.
2. Coverage gate сервиса не должен упасть ниже текущего значения.
3. Если меняете контракт API — обновите `frontend/src/shared/api/types.ts`
   и/или сгенерируйте через `make frontend-types`.
4. Если меняете схему БД — добавьте Alembic-миграцию и не правьте
   существующие.
5. Заполните описание PR: что меняется, почему, как тестировали,
   соответствие пункту диссертации (если применимо).

## Архитектурные решения

Перед существенным архитектурным изменением заведите новый ADR в
`docs/adr/NNNN-<slug>.md` (формат Майкла Найгарда — Контекст /
Альтернативы / Решение / Последствия).

## Соответствие диссертации

Этот репозиторий — реализация дипломной работы. См. `docs/THESIS_MAPPING.md`
для соответствия пунктов диссертации файлам в коде. Изменения, ломающие
это соответствие, требуют обновления маппинга в том же PR.
