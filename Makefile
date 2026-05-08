.PHONY: help install up down migrate auth probe acknowledge run test lint typecheck clean

help:
	@echo "Trading_Agent — common commands"
	@echo ""
	@echo "  make install        Install Python deps (editable + dev extras)"
	@echo "  make up             Start Postgres + Redis + app via Docker Compose"
	@echo "  make down           Stop Docker Compose stack"
	@echo "  make migrate        Run Alembic migrations to head"
	@echo "  make auth           Interactive Upstox OAuth (first-time / daily refresh)"
	@echo "  make probe          Probe Upstox API capabilities for current account"
	@echo "  make acknowledge    Sign live-trading acknowledgment (lock 3 of 3)"
	@echo "  make run            Run FastAPI control plane locally"
	@echo "  make test           Run pytest suite"
	@echo "  make lint           Ruff lint"
	@echo "  make typecheck      mypy strict"
	@echo "  make clean          Remove caches and build artifacts"

install:
	pip install -e ".[dev]"

up:
	docker compose up -d

down:
	docker compose down

migrate:
	alembic upgrade head

auth:
	python scripts/upstox_auth_cli.py

probe:
	python scripts/verify_upstox_capabilities.py

acknowledge:
	python scripts/acknowledge_live_trading.py

run:
	uvicorn trading_agent.api.main:app --reload --port 8000

test:
	pytest

lint:
	ruff check src tests scripts

typecheck:
	mypy src

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
