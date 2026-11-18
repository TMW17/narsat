# Common tasks. `make help` lists them.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv
COMPOSE ?= docker compose

help: ## List the targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ development
install: ## Create the Python environment (all extras, dev tools) and install the web app
	$(UV) sync --frozen --extra all --group dev
	cd web && npm ci

lint: ## Format check, lint and type check (Python and TypeScript)
	$(UV) run --frozen ruff format --check src tests services
	$(UV) run --frozen ruff check src tests services
	$(UV) run --frozen mypy
	cd web && npm run typecheck && npm run lint

format: ## Format the Python code
	$(UV) run --frozen ruff format src tests services
	$(UV) run --frozen ruff check --fix src tests services

test: ## Unit tests (no services needed)
	$(UV) run --frozen pytest tests/unit -q

test-integration: ## Integration tests; needs NARSAT_TEST_DB_DSN (and S3 settings), see docs/development.md
	$(UV) run --frozen pytest tests -q -m integration

test-web: ## Web application tests
	cd web && npm test

dev-api: ## Run the API with auto-reload against the database of .env
	$(UV) run --frozen narsat serve-api --reload --host 127.0.0.1

dev-web: ## Run the web application with hot reload (proxies to localhost:8000 and :8083)
	cd web && npm run dev

marts: ## Build the dbt marts and publish them to the API database
	$(UV) run --frozen dbt build --project-dir transform --profiles-dir transform
	$(UV) run --frozen narsat lake publish-marts --dir var/marts

# ------------------------------------------------------------------ stack
up: ## Build and start the core platform
	$(COMPOSE) up -d --build

up-all: ## Start everything (Airflow, inference, Prometheus/Grafana included)
	$(COMPOSE) --profile orchestration --profile ml --profile observability up -d --build

down: ## Stop the stack (data volumes are kept)
	$(COMPOSE) --profile orchestration --profile ml --profile observability down

ps: ## State of the containers
	$(COMPOSE) --profile orchestration --profile ml --profile observability ps

logs: ## Follow the logs of the platform services
	$(COMPOSE) logs -f --tail=100 poller consumer api

config: ## Validate compose.yaml with the values of .env
	$(COMPOSE) --profile orchestration --profile ml --profile observability config --quiet && echo "compose.yaml is valid"

.PHONY: help install lint format test test-integration test-web dev-api dev-web marts up up-all down ps logs config
