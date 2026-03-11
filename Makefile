# Use "docker compose" (Docker Desktop plugin) or "docker-compose" (standalone)
COMPOSE = docker compose
start:
	$(COMPOSE) up -d --remove-orphans
stop:
	$(COMPOSE) down
build:
	$(COMPOSE) build
pull:
	$(COMPOSE) pull
