# =====================================================================
# CashBack — developer Makefile
# =====================================================================

SHELL := /bin/bash

COMPOSE       ?= docker compose
COMPOSE_FILE  ?= docker-compose.yml
PROJECT       ?= cashback
ENV_FILE      ?= .env

# Pass --env-file only if a real .env exists. Without it compose stops at the
# first `${VAR:?run make env}` (the services have no built-in secrets).
ifeq ($(wildcard $(ENV_FILE)),)
COMPOSE_CMD := $(COMPOSE) -p $(PROJECT) -f $(COMPOSE_FILE)
else
COMPOSE_CMD := $(COMPOSE) -p $(PROJECT) --env-file $(ENV_FILE) -f $(COMPOSE_FILE)
endif

.DEFAULT_GOAL := help

.PHONY: help env up down logs ps seed test clean config build restart pull migrate \
        seed-users seed-history stream-on stream-off simulator-shell wait-kafka \
        trigger-etl etl-test etl-logs \
        seed-recommendations seed-demo train-models ml-test \
        api-test api-logs api-shell \
        cm-test cm-logs cm-shell \
        tl-test tl-logs tl-emails \
        mobile-test mobile-logs mobile-shell \
        frontend-build frontend-logs frontend-types \
        test-unit test-integration test-e2e test-load test-all \
        helm-lint helm-template helm-package helm-deps

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# Secrets `make env` generates when .env.example leaves them empty:
# signing keys — 32 random bytes as hex, passwords — 18 bytes as base64.
ENV_KEYS      := JWT_SECRET SERVICE_JWT_SECRET MOBILE_JWT_SECRET
ENV_PASSWORDS := ADMIN_BOOTSTRAP_PASSWORD DEMO_USERS_PASSWORD

# Never overwrites an existing .env (it may hold the only copy of the admin
# password): it only reports which secrets there are still empty. The new
# file is written to a temp file (umask 077) and renamed into place.
env: ## Create .env from .env.example with random secrets (never overwrites).
	@set -euo pipefail; \
	if [ -e "$(ENV_FILE)" ]; then \
		echo ">> $(ENV_FILE) already exists — left unchanged"; \
		missing=""; \
		for key in $(ENV_KEYS); do \
			grep -Eq "^$$key=.+" "$(ENV_FILE)" || missing="$$missing $$key"; \
		done; \
		if [ -n "$$missing" ]; then \
			echo "!! empty or missing in $(ENV_FILE):$$missing"; \
			echo "   the services refuse to start without them: add each one"; \
			echo "   (openssl rand -hex 32, all different) or move $(ENV_FILE)"; \
			echo "   away and run 'make env' again"; \
		fi; \
		equal=""; seen=""; \
		for key in $(ENV_KEYS); do \
			value="$$(sed -n "s/^$$key=//p" "$(ENV_FILE)" | tail -n 1)"; \
			if [ -z "$$value" ]; then continue; fi; \
			for prev in $$seen; do \
				if [ "$$value" = "$$(sed -n "s/^$$prev=//p" "$(ENV_FILE)" | tail -n 1)" ]; then \
					equal="$$equal $$prev=$$key"; \
				fi; \
			done; \
			seen="$$seen $$key"; \
		done; \
		if [ -n "$$equal" ]; then \
			echo "!! equal keys in $(ENV_FILE):$$equal"; \
			echo "   each service checks only its own keys: a leak of one key must"; \
			echo "   not forge another's tokens — replace one (openssl rand -hex 32)"; \
		fi; \
		exit 0; \
	fi; \
	command -v openssl >/dev/null || { echo "!! openssl is required for 'make env'"; exit 1; }; \
	umask 077; tmp="$$(mktemp "$(ENV_FILE).XXXXXX")"; \
	trap 'rm -f "$$tmp"' EXIT; \
	while IFS= read -r line || [ -n "$$line" ]; do \
		key="$${line%%=*}"; \
		if [ "$$line" = "$$key=" ]; then \
			case " $(ENV_KEYS) " in *" $$key "*) line="$$key=$$(openssl rand -hex 32)";; esac; \
			case " $(ENV_PASSWORDS) " in *" $$key "*) line="$$key=$$(openssl rand -base64 18)";; esac; \
		fi; \
		printf '%s\n' "$$line"; \
	done < .env.example > "$$tmp"; \
	mv "$$tmp" "$(ENV_FILE)"; trap - EXIT; \
	echo ">> created $(ENV_FILE) (mode 600) with random $(ENV_KEYS) $(ENV_PASSWORDS)"; \
	echo "   admin UI login: admin@bank.ru / ADMIN_BOOTSTRAP_PASSWORD from $(ENV_FILE)"; \
	echo "   (applied by the first 'make migrate' on a fresh database);"; \
	echo "   demo logins (make seed-demo): DEMO_USERS_PASSWORD from $(ENV_FILE)"

up: ## Start the full stack in detached mode.
	$(COMPOSE_CMD) up -d

down: ## Stop the stack (keep volumes).
	$(COMPOSE_CMD) down

up-obs: ## Start the stack together with observability (Prometheus/Grafana/Alertmanager).
	$(COMPOSE_CMD) -f docker-compose.observability.yml up -d

down-obs: ## Stop the stack including observability services.
	$(COMPOSE_CMD) -f docker-compose.observability.yml down

# app.main builds Settings on import, and Settings refuses to load without
# strong keys (stage B). The schema does not depend on their values: the dump
# gets throwaway random ones (all different, as the services require).
gen-api-types: ## Regenerate TS types from all service OpenAPI schemas (offline, no heavy deps).
	@set -e; \
	export JWT_SECRET=$$(openssl rand -hex 32) SERVICE_JWT_SECRET=$$(openssl rand -hex 32) \
		MOBILE_JWT_SECRET=$$(openssl rand -hex 32); \
	for pair in campaign_manager:campaign-manager recommendation_api:recommendation-api mobile_api:mobile-api; do \
		mod="$${pair%%:*}"; out="$${pair##*:}"; \
		echo ">>> $$out"; \
		python scripts/dump_openapi.py "$$mod" \
			> frontend/src/shared/api/generated/$$out.openapi.json; \
		( cd frontend && npx openapi-typescript \
			src/shared/api/generated/$$out.openapi.json \
			-o src/shared/api/generated/$$out.ts ); \
	done

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

test: test-unit ## Run the unit test suites (alias of test-unit; fails on any failure).

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

seed-demo: ## All-in-one fast demo seed (PG + ClickHouse + Redis, без Kafka/Airflow).
	@python3 scripts/seed_demo_data.py

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

# Every suite runs even if an earlier one fails; the target fails at the end
# and names the failed suites (set -e would hide the ones after the first).
test-unit: ## Run per-service unit suites with coverage gates.
	@failed=""; \
	for svc in etl recommendation_api campaign_manager transaction_listener \
	           mobile_api ml_training tx_simulator; do \
		echo ">>> $$svc"; \
		( cd services/$$svc && \
			python -m pytest tests/ -q -m "not integration" \
				--cov-config=pyproject.toml --cov ) || failed="$$failed $$svc"; \
	done; \
	if [ -n "$$failed" ]; then echo "!!! unit suites failed:$$failed"; exit 1; fi

# One pytest process per module: every service ships its own top-level `app`
# package, and each module puts its service first on sys.path. The modules
# need Docker (testcontainers) and tests/requirements-integration.txt.
# Every module runs even if an earlier one fails; a module with no collected
# tests fails too (pytest exit 5). -rs lists every skip with its reason (under
# CI=true the container fixtures fail instead of skipping, tests/conftest.py).
# No extra -q: tests/pyproject.toml already has it, and -qq would drop the
# "N passed, M skipped" line of every module.
test-integration: ## Run repo-level integration tests (testcontainers, needs Docker).
	@cd tests || exit 1; \
	failed=""; \
	for f in integration/test_*.py; do \
		echo ">>> $$f"; \
		python -m pytest "$$f" -rs -m integration -c pyproject.toml --tb=short \
			|| failed="$$failed $$f"; \
	done; \
	if [ -n "$$failed" ]; then echo "!!! integration modules failed:$$failed"; exit 1; fi

test-e2e: ## Run the 9-step e2e cycle against the running stack.
	cd tests && python -m pytest e2e -q -m e2e -c pyproject.toml --tb=short -s

test-load: ## Run the Locust scenario in headless mode → test-reports/.
	@bash scripts/run_load_test.sh

test-all: test-unit test-integration test-e2e ## Full pyramid in sequence.
	@echo ">>> all tests passed"

# ---------------------------------------------------------------------
# Helm targets
# ---------------------------------------------------------------------

# The chart has no built-in secrets (`required` in templates/secret.yaml)
# and, with the ingress on, needs the ingress controller's pod CIDR
# (campaignManager.forwardedAllowIps): lint/template render it with
# throwaway random secrets and an example CIDR.
HELM_SECRETS = --set secrets.jwtSecret=$$(openssl rand -hex 32) \
	--set secrets.serviceJwtSecret=$$(openssl rand -hex 32) \
	--set secrets.mobileJwtSecret=$$(openssl rand -hex 32) \
	--set secrets.adminBootstrapPassword=$$(openssl rand -hex 12) \
	--set campaignManager.forwardedAllowIps=10.244.0.0/16

helm-lint: ## Lint the cashback Helm chart.
	helm lint helm/cashback $(HELM_SECRETS)

helm-template: ## Render the chart (sanity-check the manifests).
	helm template cashback helm/cashback $(HELM_SECRETS) | head -200

helm-package: ## Package the chart into dist/cashback-<ver>.tgz.
	@bash scripts/helm-package.sh

# Subcharts come from OCI (oci://registry-1.docker.io/bitnamicharts in
# Chart.yaml): no `helm repo add` needed.
helm-deps: ## Pull subchart dependencies (Postgres / Redis / Kafka / ClickHouse).
	helm dependency build helm/cashback
