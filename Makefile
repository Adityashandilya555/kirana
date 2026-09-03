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

# DATABASE_URL is set here rather than expected in the environment: the
# integration tests skip without it, so `make test-all` used to report a clean
# pass having run nothing at all.
test-all: test ## Unit + integration tests (needs `make db-reset` first)
	cd backend && DATABASE_URL=$(DB_URL) uv run pytest -q -m integration

typecheck: ## Frontend typecheck + production build
	cd frontend && npx tsc --noEmit && npm run build

# Published on 5433, not 5432, so it cannot collide with a Postgres someone
# already runs locally. Without a published port the container was reachable
# only through `docker exec`, which meant nothing outside psql -- the
# integration suite included -- could ever connect to it.
DB_PORT ?= 5433
DB_URL  ?= postgresql://postgres:pw@127.0.0.1:$(DB_PORT)/kirana

db-up: ## Start a local Postgres 16 for schema work
	@docker rm -f $(PG) >/dev/null 2>&1 || true
	docker run -d --name $(PG) -p $(DB_PORT):5432 -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=kirana postgres:16-alpine >/dev/null
	@# pg_isready goes true once during initdb, while the server is still
	@# listening only on the unix socket -- which is why db-apply used to fail
	@# on the first statement of 001. Wait for a query to actually answer.
	@until docker exec $(PG) psql -U postgres -d kirana -c 'select 1' >/dev/null 2>&1; do sleep 0.5; done
	@echo "postgres ready on $(DB_URL)"

# Every numbered migration, in order. This used to list 001/002/003 by hand,
# which meant 004 through 011 had never run against a local database and the
# integration tests were validating a schema several migrations behind
# production. Globbing is what keeps the two honest as more are added.
# all_in_one.sql is deliberately excluded: it is a stale Phase-0 snapshot that
# later files supersede, and replaying it would undo them.
db-apply: ## Apply every sql/NNN_*.sql to the local Postgres, in order
	@for f in $$(ls sql/[0-9][0-9][0-9]_*.sql | sort); do \
	  docker exec -i $(PG) psql -U postgres -d kirana -v ON_ERROR_STOP=1 -q < $$f \
	    && echo "OK    $$f" || { echo "FAIL  $$f"; exit 1; }; \
	done

db-reset: db-up db-apply ## Recreate the local database from scratch

db-psql: ## Open a psql shell on the local Postgres
	docker exec -it $(PG) psql -U postgres -d kirana

db-down: ## Stop and remove the local Postgres
	@docker rm -f $(PG) >/dev/null 2>&1 || true; echo "stopped"

.PHONY: help dev-api dev-web test test-all typecheck db-up db-apply db-reset db-psql db-down
