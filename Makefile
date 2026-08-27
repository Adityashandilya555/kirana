.DEFAULT_GOAL := help
SHELL := /bin/bash
PG := kirana-pg

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

dev-api: ## Run the FastAPI backend on :8000
	cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-web: ## Run the Vite dev server on :5173
	cd frontend && npm run dev -- --host

test: ## Unit tests (no network, no database)
	cd backend && uv run pytest -q -m "not integration"
	cd frontend && npx vitest run --passWithNoTests

test-all: test ## Unit + integration tests (needs a database)
	cd backend && uv run pytest -q -m integration

typecheck: ## Frontend typecheck + production build
	cd frontend && npx tsc --noEmit && npm run build

db-up: ## Start a local Postgres 16 for schema work
	@docker rm -f $(PG) >/dev/null 2>&1 || true
	docker run -d --name $(PG) -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=kirana postgres:16-alpine >/dev/null
	@until docker exec $(PG) pg_isready -U postgres -d kirana >/dev/null 2>&1; do sleep 0.5; done
	@echo "postgres ready"

db-apply: ## Apply sql/*.sql to the local Postgres
	@for f in sql/001_schema.sql sql/002_functions.sql sql/003_seed.sql; do \
	  docker exec -i $(PG) psql -U postgres -d kirana -v ON_ERROR_STOP=1 -q < $$f \
	    && echo "OK    $$f" || { echo "FAIL  $$f"; exit 1; }; \
	done

db-reset: db-up db-apply ## Recreate the local database from scratch

db-psql: ## Open a psql shell on the local Postgres
	docker exec -it $(PG) psql -U postgres -d kirana

db-down: ## Stop and remove the local Postgres
	@docker rm -f $(PG) >/dev/null 2>&1 || true; echo "stopped"

.PHONY: help dev-api dev-web test test-all typecheck db-up db-apply db-reset db-psql db-down
