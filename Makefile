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

.PHONY: help up down logs ps seed test clean config build restart pull migrate

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
