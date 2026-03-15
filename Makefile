.PHONY: up down reset logs psql status install pull-model dev \
        apple-up apple-down apple-reset apple-logs apple-psql apple-status

# ---------------------------------------------------------------------------
# Docker (default — macOS 13+)
# ---------------------------------------------------------------------------

up:
	docker compose up -d

down:
	docker compose down

reset:
	docker compose down -v
	docker compose up -d

logs:
	docker compose logs -f postgres

psql:
	docker compose exec postgres psql -U brain -d brain

status:
	docker compose ps

# ---------------------------------------------------------------------------
# Apple container CLI (macOS 26+, Apple Silicon optimised)
# ---------------------------------------------------------------------------

apple-up:
	./scripts/apple-container.sh up

apple-down:
	./scripts/apple-container.sh down

apple-reset:
	./scripts/apple-container.sh reset

apple-logs:
	./scripts/apple-container.sh logs

apple-psql:
	./scripts/apple-container.sh psql

apple-status:
	./scripts/apple-container.sh status

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

install:
	uv sync

pull-model:
	ollama pull nomic-embed-text

dev:
	uv run python -m brain.server
