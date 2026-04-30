# =====================================================================
# CashBack — developer Makefile
# =====================================================================

SHELL := /bin/bash

COMPOSE       ?= docker compose
COMPOSE_FILE  ?= docker-compose.yml
PROJECT       ?= cashback
ENV_FILE      ?= .env

# Pass --env-file only if a real .env exists, otherwise rely on .env.example defaults.
ifeq ($(wildcard $(ENV_FILE)),)
COMPOSE_CMD := $(COMPOSE) -p $(PROJECT) -f $(COMPOSE_FILE)
else
COMPOSE_CMD := $(COMPOSE) -p $(PROJECT) --env-file $(ENV_FILE) -f $(COMPOSE_FILE)
endif

.DEFAULT_GOAL := help

.PHONY: help up down logs ps seed test clean config build restart pull migrate \
        seed-users seed-history stream-on stream-off simulator-shell wait-kafka \
        trigger-etl etl-test etl-logs \
        seed-recommendations train-models ml-test \
        api-test api-logs api-shell \
        cm-test cm-logs cm-shell \
        tl-test tl-logs tl-emails \
        mobile-test mobile-logs mobile-shell \
        frontend-build frontend-logs frontend-types \
        test-unit test-integration test-e2e test-load test-all

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start the full stack in detached mode.
	$(COMPOSE_CMD) up -d

down: ## Stop the stack (keep volumes).
	$(COMPOSE_CMD) down

logs: ## Tail logs from all services.
	$(COMPOSE_CMD) logs -f --tail=200

ps: ## Show status of all services.
	$(COMPOSE_CMD) ps

migrate: ## Apply all DB migrations (Postgres via Alembic + ClickHouse SQL).
	@./scripts/init_db.sh

seed: ## Apply migrations and load fixtures (placeholder).
	@echo ">> Seeding databases..."
	@./scripts/init_db.sh
	@if [ -d tests/fixtures ] && [ -n "$$(ls -A tests/fixtures 2>/dev/null)" ]; then \
		echo "   loading fixtures from tests/fixtures/"; \
	else \
		echo "   no fixtures yet — implement once schema is ready"; \
	fi

test: ## Run the project test suite (placeholder).
	@echo ">> Running tests..."
	@if command -v pytest >/dev/null 2>&1; then \
		pytest -q || true; \
	else \
		echo "   pytest not installed; skipping"; \
	fi

clean: ## Stop the stack and remove volumes/orphans (DATA LOSS).
	$(COMPOSE_CMD) down -v --remove-orphans

config: ## Validate and render the compose configuration.
	$(COMPOSE_CMD) config

build: ## Build all custom images defined in compose.
	$(COMPOSE_CMD) build

restart: down up ## Restart the full stack.

pull: ## Pull all referenced images.
	$(COMPOSE_CMD) pull

# ---------------------------------------------------------------------
# TX Simulator targets
# ---------------------------------------------------------------------

wait-kafka: ## Wait for Kafka broker + transactions.raw topic.
	@./scripts/wait_for_kafka.sh

seed-users: ## Generate 10 000 synthetic users + profiles (Postgres + Avro).
	$(COMPOSE_CMD) exec tx-simulator python -m app.simulator init-users --count 10000

seed-history: ## Backfill 90 days of history at 1000 events/sec.
	$(COMPOSE_CMD) exec tx-simulator python -m app.simulator backfill --days 90 --rate 1000

stream-on: ## Start the live stream (50 tx/sec) in the simulator container.
	$(COMPOSE_CMD) exec -d tx-simulator bash -lc \
		'mkdir -p /tmp && nohup python -m app.simulator stream --rate 50 \
		 >/tmp/stream.log 2>&1 & echo $$! >/tmp/stream.pid'
	@echo "stream started — tail logs with: docker exec cashback-tx-simulator tail -f /tmp/stream.log"

stream-off: ## Stop the live stream.
	$(COMPOSE_CMD) exec tx-simulator bash -lc \
		'if [ -f /tmp/stream.pid ]; then \
			kill -TERM $$(cat /tmp/stream.pid) 2>/dev/null || true; \
			rm -f /tmp/stream.pid; \
			echo "stream stopped"; \
		 else echo "no stream running"; fi'

simulator-shell: ## Open a shell inside the tx-simulator container.
	$(COMPOSE_CMD) exec tx-simulator bash

# ---------------------------------------------------------------------
# ETL targets
# ---------------------------------------------------------------------

trigger-etl: ## Trigger the cashback_daily_etl DAG via the Airflow API.
	@./scripts/trigger_etl.sh cashback_daily_etl

etl-test: ## Run ETL unit tests with coverage.
	cd services/etl && python -m pytest tests/unit -q --cov=app --cov-report=term

etl-logs: ## Tail Airflow scheduler logs.
	$(COMPOSE_CMD) logs -f --tail=200 airflow-scheduler

# ---------------------------------------------------------------------
# ML training targets (profile: ml)
# ---------------------------------------------------------------------

seed-recommendations: ## Seed synthetic recommendations with binomial accept_rate.
	@python3 scripts/seed_recommendations.py

train-models: ## Run SVD++ + LightGBM training and try to promote.
	$(COMPOSE_CMD) --profile ml run --rm ml-training python -m app.cli train-all

ml-test: ## Run ML unit tests with coverage.
	cd services/ml_training && python -m pytest tests/unit -q --cov=app --cov-report=term

# ---------------------------------------------------------------------
# Recommendation API targets
# ---------------------------------------------------------------------

api-test: ## Run recommendation_api unit tests (excludes integration / docker).
	cd services/recommendation_api && \
		python -m pytest tests/unit -q --cov=app --cov-report=term

api-logs: ## Tail recommendation-api logs.
	$(COMPOSE_CMD) logs -f --tail=200 recommendation-api

api-shell: ## Open a shell inside the recommendation-api container.
	$(COMPOSE_CMD) exec recommendation-api bash

# ---------------------------------------------------------------------
# Campaign Manager targets
# ---------------------------------------------------------------------

cm-test: ## Run campaign-manager unit tests.
	cd services/campaign_manager && \
		python -m pytest tests/unit -q --cov=app --cov-report=term

cm-logs: ## Tail campaign-manager logs.
	$(COMPOSE_CMD) logs -f --tail=200 campaign-manager

cm-shell: ## Shell inside the campaign-manager container.
	$(COMPOSE_CMD) exec campaign-manager bash

# ---------------------------------------------------------------------
# Transaction listener targets
# ---------------------------------------------------------------------

tl-test: ## Run transaction-listener unit tests.
	cd services/transaction_listener && \
		python -m pytest tests/unit -q --cov=app --cov-report=term

tl-logs: ## Tail transaction-listener logs.
	$(COMPOSE_CMD) logs -f --tail=200 transaction-listener

tl-emails: ## Show emails the listener wrote to the sent-emails volume.
	$(COMPOSE_CMD) exec transaction-listener sh -c \
		'ls -lh /tmp/sent_emails | tail -20'

# ---------------------------------------------------------------------
# Mobile BFF targets
# ---------------------------------------------------------------------

mobile-test: ## Run mobile-api unit tests.
	cd services/mobile_api && \
		python -m pytest tests/unit -q --cov=app --cov-report=term

mobile-logs: ## Tail mobile-api logs.
	$(COMPOSE_CMD) logs -f --tail=200 mobile-api

mobile-shell: ## Shell inside the mobile-api container.
	$(COMPOSE_CMD) exec mobile-api bash

# ---------------------------------------------------------------------
# Frontend targets
# ---------------------------------------------------------------------

frontend-build: ## Rebuild the frontend image.
	$(COMPOSE_CMD) build frontend

frontend-logs: ## Tail nginx + Vite logs.
	$(COMPOSE_CMD) logs -f --tail=200 frontend

frontend-types: ## Regenerate TypeScript types from the live FastAPI services.
	@bash scripts/generate-api-types.sh

# ---------------------------------------------------------------------
# Testing pyramid
# ---------------------------------------------------------------------

test-unit: ## Run per-service unit suites with coverage gates.
	@set -e; \
	for svc in etl recommendation_api campaign_manager transaction_listener \
	           mobile_api ml_training tx_simulator; do \
		echo ">>> $$svc"; \
		( cd services/$$svc && \
			python -m pytest tests/ -q -m "not integration" \
				--cov-config=pyproject.toml --cov ); \
	done

test-integration: ## Run repo-level integration tests (requires Docker for some).
	@set -e; \
	cd tests && \
	for f in integration/test_*.py; do \
		echo ">>> $$f"; \
		python -m pytest "$$f" -q -m integration -c pyproject.toml --tb=short || exit $$?; \
	done

test-e2e: ## Run the 9-step e2e cycle against the running stack.
	cd tests && python -m pytest e2e -q -m e2e -c pyproject.toml --tb=short -s

test-load: ## Run the Locust scenario in headless mode → test-reports/.
	@bash scripts/run_load_test.sh

test-all: test-unit test-integration test-e2e ## Full pyramid in sequence.
	@echo ">>> all tests passed"
