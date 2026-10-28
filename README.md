# Daily Game Challenge backend

The FastAPI backend of Daily Game Challenge, a site with daily puzzle games. The package is `daily-game-challenge-be`; the repo keeps the name `my-list-be`. The frontend is the sibling repo `my-list-fe`. The contract between the two is `CONTRACTS.md` in the parent platform folder.

Today the backend has accounts (register, login, logout, password reset and update), health checks and the OpenAPI snapshot. The games come in Phase 1.

## What you need

- [uv](https://docs.astral.sh/uv/). It downloads Python 3.12 if your system has another version.
- Docker, or your own Postgres 16 and Redis 7.

## Install

```
uv sync
cp .env.example .env
```

## Run

```
docker compose up -d
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

The API serves on `http://localhost:8000`, with interactive docs at `/docs`. Use `localhost`, not `127.0.0.1`: the session cookie is host-scoped, so the frontend on `http://localhost:5173` only gets it from the same host name.

## Test

```
uv run pytest
```

The tests need Postgres and Redis. They create `daily_game_challenge_test` themselves, drop its tables when they finish and flush Redis database 1, so `TEST_DATABASE_URL` and `TEST_REDIS_URL` must differ from the dev ones.

If port 5433 or 6379 is taken, start throwaway containers on other ports and pass the four settings on the command line (they beat `.env`):

```
docker run -d --name dgc-dev-pg -e POSTGRES_PASSWORD=postgres -p 5544:5432 postgres:16-alpine
docker run -d --name dgc-dev-redis -p 6390:6379 redis:7-alpine
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5544/daily_game_challenge \
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5544/daily_game_challenge_test \
REDIS_URL=redis://localhost:6390/0 \
TEST_REDIS_URL=redis://localhost:6390/1 \
uv run pytest -q
```

## Lint and the API contract

```
uv run ruff check .
uv run ruff format --check .
uv run python scripts/export_openapi.py --check
```

`openapi/openapi.json` is the snapshot the frontend generates its API types from. After you change a route or a schema, run `uv run python scripts/export_openapi.py` and commit the result.

## Old local database

The development database used to be called `item_list`. A Docker volume `my-list-be_db_data` created before the rename still holds only that database, because the Postgres image applies `POSTGRES_DB` only to an empty data folder. Local data is test data and is not migrated. To start clean:

```
docker compose down
docker volume rm my-list-be_db_data
docker compose up -d
uv run alembic upgrade head
```

Then copy the new `DATABASE_URL` and `TEST_DATABASE_URL` from `.env.example` into your `.env`.

More about the code layout and the rules for changes: `CLAUDE.md`.
