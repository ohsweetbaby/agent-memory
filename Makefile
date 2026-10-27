SHELL := /bin/bash

UV ?= uv
COMPOSE ?= docker compose
COMPOSE_FILE := infra/docker-compose.yml
ENV_FILE := $(wildcard .env)
COMPOSE_ENV := $(if $(ENV_FILE),--env-file $(ENV_FILE),)
COMPOSE_CMD := $(COMPOSE) $(COMPOSE_ENV) -f $(COMPOSE_FILE)

.PHONY: up down logs test e2e seed backup restore

up:
	$(COMPOSE_CMD) up -d --build

down:
	$(COMPOSE_CMD) down

logs:
	$(COMPOSE_CMD) logs -f --tail=200

test:
	$(UV) run pytest

e2e:
	pnpm --dir apps/web test:e2e

seed:
	$(COMPOSE_CMD) exec -T api python -m app.seed

backup:
	bash infra/scripts/backup.sh

restore:
	bash infra/scripts/restore.sh $(BACKUP)
