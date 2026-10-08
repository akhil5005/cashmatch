# Thin convenience wrapper. Every target maps to one obvious command.
.PHONY: help up down logs test fmt lint install health generate demo ui

help:
	@echo "up       start Postgres + API in Docker"
	@echo "down     stop everything and delete the database volume"
	@echo "logs     tail the API logs"
	@echo "install  install the backend in editable mode with dev extras"
	@echo "test     run the test suite (SQLite in-memory, no Docker needed)"
	@echo "lint     ruff check"
	@echo "fmt      ruff format"
	@echo "health   curl both health endpoints"
	@echo "generate regenerate the synthetic dataset (wipes existing data)"
	@echo "demo     full pipeline: generate, extract, decide, evaluate"
	@echo "ui       open the review UI"

up:
	docker compose up --build -d

down:
	docker compose down -v

logs:
	docker compose logs -f api

install:
	cd backend && pip install -e ".[dev]"

test:
	cd backend && pytest -q

lint:
	cd backend && ruff check .

fmt:
	cd backend && ruff format .

health:
	curl -sS http://localhost:8000/health && echo && curl -sS http://localhost:8000/health/db && echo

generate:
	cd backend && python -m cashmatch.cli generate --reset

demo:
	docker compose exec -T api python -m cashmatch.cli generate --reset
	docker compose exec -T api python -m cashmatch.cli extract
	docker compose exec -T api python -m cashmatch.cli apply
	docker compose exec -T api python -m cashmatch.cli evaluate

ui:
	@echo "Review UI: http://localhost:5173"
	@echo "API docs : http://localhost:8000/docs"
