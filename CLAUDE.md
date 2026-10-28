# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Daily Game Challenge backend

## Project state

Dependency management runs on uv: direct dependencies are in `pyproject.toml` (runtime under `[project]`, test and lint tools under the `dev` group) and every version, direct or transitive, is pinned in `uv.lock`. Core infra (`app/core/config.py` Settings, `app/core/db.py` async engine/session, Alembic under `app/alembic/`) is wired up. The only domain so far is `app/auth/`: register, login, logout and the current user via Redis-backed sessions (`POST /v1/auth/register`, `POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/me`), plus forgot, reset and update password (`POST /v1/auth/forgot-password`, `POST /v1/auth/reset-password`, `POST /v1/auth/update-password`). No game domain exists yet; the game domains are added in Phase 1 (see "What this is"). The suite lives in `tests/`. `tests/conftest.py` provides session-scoped `engine` (auto-creates `TEST_DATABASE_URL`'s database, creates/drops all tables), function-scoped `db_session` (one transaction per test, rolled back after via a savepoint-joined `AsyncSession`, so `service`-layer `commit()` calls don't leak between tests), and `client` (httpx `ASGITransport` client with `get_db` overridden to `db_session`). `asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope` are pinned to `session` in `pyproject.toml` so the session-scoped engine and per-test event loops don't end up on different asyncio loops (a real asyncpg failure mode otherwise). `TEST_DATABASE_URL` defaults to `localhost:5433/daily_game_challenge_test`, the same host and port as the dev database with a `_test` suffix.

`redis` is installed and used for auth sessions and password reset tokens (`app/core/redis.py`). `arq` and an S3-compatible storage client are **not installed**. They are planned for real email sending (M1-43) and clue images (M1-18); add them when that work starts. Until then `app/core/email.py` is a stub that logs the reset link in `local` and `test` only.

Errors, health and config (2026-10-27): every error body follows the contract in `../CONTRACTS.md` ("Errors"): raise an `AppException` subclass with a `code` (`app/core/exceptions.py`), never an English-only `detail`; handlers live in `app/core/errors.py` and every response carries `X-Request-ID`. `GET /healthz` and `GET /readyz` exist (`app/api/health.py`). `ENVIRONMENT` is `local`, `test`, `staging` or `production`; the last two refuse to start on a placeholder `SECRET_KEY`, a non-https `FRONTEND_URL` or localhost CORS origins (`app/core/config.py`). Passwords use argon2id (`app/core/security.py`, async wrappers). The test suite points the app at `TEST_DATABASE_URL` and `TEST_REDIS_URL` (it drops tables and flushes that Redis database, so both must differ from the dev ones). CI is `.github/workflows/ci.yml` (lint, test, migrations, openapi). The API is mounted under `/v1`; `openapi/openapi.json` is the contract snapshot the frontend generates its types from: after changing any route or schema run `uv run python scripts/export_openapi.py` and commit it (a test and CI fail otherwise); a breaking change needs the `breaking-change` pull request label.

## Commands

Install uv (https://docs.astral.sh/uv/), then set up the venv (Python 3.12, from `.python-version`; uv downloads it if the system has another):
```
uv sync            # creates .venv and installs exactly what uv.lock says (runtime + dev)
```

Run the dev server (auto-reload):
```
uv run uvicorn app.main:app --reload
```

The app serves on `http://127.0.0.1:8000` by default. In dev, reach it via `http://localhost:8000` (not `127.0.0.1:8000`) from the frontend — the session cookie is host-scoped, and `127.0.0.1`/`localhost` are different hosts for cookie purposes, so a cookie set via `127.0.0.1` won't round-trip to a `localhost:5173` origin. `localhost` already resolves to `127.0.0.1`, so no bind-host change is needed, just consistent hostnames on both sides.

Other common commands:
```
uv add <pkg>                                   # add a runtime dependency (uv add --dev <pkg> for tools); updates pyproject.toml and uv.lock
uv lock --upgrade-package <pkg>                # upgrade one package in uv.lock
uv run ruff check .                            # lint
uv run ruff format .                           # format (CI runs ruff format --check .)
uv run pytest                                  # run tests (needs Postgres and Redis, see below)
uv run python scripts/export_openapi.py        # rewrite openapi/openapi.json (add --check to only compare)
uv run alembic revision --autogenerate -m "message"   # new migration
uv run alembic upgrade head                    # apply migrations
```

Commit `uv.lock` with every dependency change; CI runs `uv sync --locked` and fails when the lock is stale.

Copy `.env.example` to `.env` and adjust values before running anything that touches the DB (`DATABASE_URL` points at Postgres).

`docker compose up -d` starts Postgres 16 (host port 5433, database `daily_game_challenge`) and Redis 7 (port 6379).

Old local database: the database used to be called `item_list`. A volume `my-list-be_db_data` created before the rename still holds only that old database, because the Postgres image applies `POSTGRES_DB` only to an empty data folder. Local data is test data and is not migrated. To start clean: `docker compose down`, `docker volume rm my-list-be_db_data`, `docker compose up -d`, `uv run alembic upgrade head`. Then copy the new `DATABASE_URL` and `TEST_DATABASE_URL` from `.env.example` into your `.env`. The test suite creates the test database itself.

Running the tests when port 5433 or 6379 is taken (for example by another project's database): start throwaway containers on other ports and override the four settings on the same command line. The overrides beat `.env`. The suite drops its tables and flushes the test Redis database, so never point `TEST_DATABASE_URL` or `TEST_REDIS_URL` at data you want to keep.
```
docker run -d --name dgc-dev-pg -e POSTGRES_PASSWORD=postgres -p 5544:5432 postgres:16-alpine
docker run -d --name dgc-dev-redis -p 6390:6379 redis:7-alpine
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5544/daily_game_challenge \
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5544/daily_game_challenge_test \
REDIS_URL=redis://localhost:6390/0 \
TEST_REDIS_URL=redis://localhost:6390/1 \
uv run pytest -q
```
Use the same four values with `uv run alembic ...` (create the `daily_game_challenge` database first: `docker exec dgc-dev-pg createdb -U postgres daily_game_challenge`). Remove the containers with `docker rm -f dgc-dev-pg dgc-dev-redis` when done.

## What this is
The FastAPI backend of Daily Game Challenge, a site with daily puzzle games (product spec: `../SPEC.md`, plan: `../MILESTONE-1.md`). Today it has accounts (auth and password flows), the health checks, the error contract and the OpenAPI snapshot. There is no game code yet. Phase 1 (M1-10 to M1-18) adds games, puzzles, guesses, progress and stats.

This is a learning/practice project built deliberately with production-grade patterns — the goal is real-world architecture, not shortcuts.

## This repo
FastAPI backend only. The frontend is the sibling repo `../my-list-fe`, a Vite/React SPA running at `http://localhost:5173` in dev.

## Stack
- FastAPI, Python 3.12, managed with **uv** (`pyproject.toml` + `uv.lock`, `.venv` created by `uv sync`)
- SQLAlchemy 2.0 **async** + asyncpg, PostgreSQL 16
- Alembic (async template) for migrations
- Redis 7 for sessions and password reset tokens
- argon2id password hashing (`argon2-cffi`)
- Session-based auth: opaque server-side session token in an httpOnly cookie, session store is Redis (no Postgres sessions table, no JWT)
- Planned, not installed: arq for background jobs (email sending, M1-43) and S3-compatible storage for clue images (M1-18)

## Architecture
Layered structure under `app/`: router, service, query layer, models. Dependencies point one way: a router calls a service, a service calls the query layer in `app/crud/`, the query layer uses the models in `app/models/`. Today's layout:
- `app/auth/`: the auth domain (`router.py`, `schemas.py`, `service.py`).
- `app/crud/`: the query layer. `user.py` for the database, `session.py` for Redis sessions, `password_reset.py` for Redis reset tokens.
- `app/models/`: SQLAlchemy models (`User`).
- `app/api/`: `deps.py` (dependencies such as the current user), `health.py` (`/healthz`, `/readyz`), `main.py` (the `/v1` router that aggregates the domain routers).
- `app/core/`: cross-cutting code, namely config, DB engine and session, Redis client, security, exceptions and error handlers, logging, request id, OpenAPI helpers, health checks, the email stub.
- `app/alembic/`: migrations.
- `app/main.py` creates the `FastAPI()` instance, mounts the health router at the root and the API router under `/v1`.

The game domains (Phase 1) will follow the same layering.

Directory naming (`app/`, `app/api/`, `app/core/`) borrows from the [full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template), but that template is flat (single `models.py`/`crud.py`, no service layer) — this project keeps the layering above instead.

## Key architectural decisions
- **Auth**: Django-style server-side sessions, not JWT. On register/login the server creates a Redis key (`session:{opaque token}` → user id, TTL = `SESSION_EXPIRE_MINUTES`) and sets it as a single httpOnly, `SameSite=Lax` session cookie (`session_id`); no access/refresh split. Logout deletes the Redis key and clears the cookie. `SameSite=Lax` is judged sufficient CSRF protection for the current (single mutating-domain) scope — revisit once the game endpoints in Phase 1 add mutating routes (guesses, progress); the CSRF Origin check is task M1-41. This API is always the source of truth for validation — the frontend's client-side checks are a UX convenience only, never trusted as-is. Dev note: the session cookie is host-scoped, so the frontend must reach the backend via `http://localhost:8000` (not `127.0.0.1:8000`) for the cookie to round-trip from `http://localhost:5173`.
- **CORS**: only for local development, where Vite runs on another port (`BACKEND_CORS_ORIGINS`, default `http://localhost:5173`). Production is same origin behind the proxy, so it needs no CORS, and `staging` and `production` refuse to start with localhost origins (`app/main.py`, `app/core/config.py`).

## Conventions
- Run everything through `uv run ...` (or `.venv/bin/...`); `uv.lock` is the source of truth for pinned versions
- Async all the way through DB access; sync only for genuinely CPU-bound or blocking code
- Tests: pytest + pytest-asyncio + httpx `ASGITransport`
- Lint/format: ruff

## Backend conventions
Layering rules and style conventions live in a project skill, not here, since they're only relevant while actively writing router/service/query-layer code — see `fastapi-service-patterns` under `.claude/skills/`.

## Related
Frontend repo: `../my-list-fe` (has its own `CLAUDE.md` with the corresponding frontend-side decisions)

Cross-repo contract lives in `../CONTRACTS.md` — check/update it when changing anything on the FE/BE boundary.
