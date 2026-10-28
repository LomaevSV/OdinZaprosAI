# Рекомендованные организаторами команды (необязательно): make up / make down / make seed.
.PHONY: up down logs seed test lint

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f app

# Сидирование не требуется: схема SQLite создаётся при старте, демо-данные генерируются. Сброс данных:
seed:
	docker compose down -v

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .
