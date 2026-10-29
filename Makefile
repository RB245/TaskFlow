.DEFAULT_GOAL := help
COMPOSE_TEST := docker compose -f docker-compose.test.yml

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: up
up: ## Start the full stack (api, db, redis, worker, beat)
	docker compose up --build -d --wait

.PHONY: down
down: ## Stop the full stack
	docker compose down

.PHONY: clean
clean: ## Stop the full stack and delete its volumes
	docker compose down -v
	$(COMPOSE_TEST) down -v

.PHONY: logs
logs: ## Tail logs from every service
	docker compose logs -f

.PHONY: services
services: ## Start only the backing services needed to run tests locally
	$(COMPOSE_TEST) up -d --wait

.PHONY: test
test: services ## Run the test suite
	pytest

.PHONY: cov
cov: services ## Run the test suite with a coverage report
	pytest --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## Lint, format-check and type-check
	ruff check .
	ruff format --check .
	mypy app

.PHONY: fmt
fmt: ## Auto-format and auto-fix
	ruff check --fix .
	ruff format .

.PHONY: migrate
migrate: ## Apply migrations to the database in DATABASE_URL
	alembic upgrade head

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="add widgets"
	alembic revision --autogenerate -m "$(m)"
