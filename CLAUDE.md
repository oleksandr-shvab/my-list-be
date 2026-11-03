# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Daily Game Challenge backend

## Project state

Dependency management runs on uv: direct dependencies are in `pyproject.toml` (runtime under `[project]`, test and lint tools under the `dev` group) and every version, direct or transitive, is pinned in `uv.lock`. Core infra (`app/core/config.py` Settings, `app/core/db.py` async engine/session, Alembic under `app/alembic/`) is wired up. The only domain so far is `app/auth/`: register, login, logout and the current user via Redis-backed sessions (`POST /v1/auth/register`, `POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/me`), plus forgot, reset and update password (`POST /v1/auth/forgot-password`, `POST /v1/auth/reset-password`, `POST /v1/auth/update-password`). The game data model exists (M1-10a: `games`, `titles`, `puzzles`, `puzzle_clues`, `clue_media_files`), but no game route yet; the routes arrive in Phase 1 (see "What this is"). The suite lives in `tests/`. `tests/conftest.py` provides session-scoped `engine` (auto-creates `TEST_DATABASE_URL`'s database, creates/drops all tables), function-scoped `db_session` (one transaction per test, rolled back after via a savepoint-joined `AsyncSession`, so `service`-layer `commit()` calls don't leak between tests), and `client` (httpx `ASGITransport` client with `get_db` overridden to `db_session`). `asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope` are pinned to `session` in `pyproject.toml` so the session-scoped engine and per-test event loops don't end up on different asyncio loops (a real asyncpg failure mode otherwise). `TEST_DATABASE_URL` defaults to `localhost:5433/daily_game_challenge_test`, the same host and port as the dev database with a `_test` suffix.

`redis` is installed and used for auth sessions and password reset tokens (`app/core/redis.py`). `arq` and an S3-compatible storage client are **not installed**. They are planned for real email sending (M1-43) and clue images (M1-18); add them when that work starts. Until then `app/core/email.py` is a stub that logs the reset link in `local` and `test` only.

Errors, health and config (2026-10-27): every error body follows the contract in `../CONTRACTS.md` ("Errors"): raise an `AppException` subclass with a `code` (`app/core/exceptions.py`), never an English-only `detail`; handlers live in `app/core/errors.py` and every response carries `X-Request-ID`. `GET /healthz` and `GET /readyz` exist (`app/api/health.py`). `ENVIRONMENT` is `local`, `test`, `staging` or `production`; every environment refuses to start when `FRONTEND_URL` is not an absolute http(s) URL, and `local` and `test` also when `API_BASE_URL` or any `BACKEND_CORS_ORIGINS` entry is not (they feed the allowed origins there only); `staging` and `production` additionally refuse a placeholder `SECRET_KEY`, a non-https `FRONTEND_URL` or localhost CORS origins (`app/core/config.py`). Passwords use argon2id (`app/core/security.py`, async wrappers). The test suite points the app at `TEST_DATABASE_URL` and `TEST_REDIS_URL` (it drops tables and flushes that Redis database, so both must differ from the dev ones). CI is `.github/workflows/ci.yml` (lint, test, migrations, openapi). The API is mounted under `/v1`; `openapi/openapi.json` is the contract snapshot the frontend generates its types from: after changing any route or schema run `uv run python scripts/export_openapi.py` and commit it (a test and CI fail otherwise); a breaking change needs the `breaking-change` pull request label.

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
The FastAPI backend of Daily Game Challenge, a site with daily puzzle games (product spec: `../SPEC.md`, plan: `../MILESTONE-1.md`). Today it has accounts (auth and password flows), the health checks, the error contract and the OpenAPI snapshot. The game tables, the puzzle visibility rule and the clock dependency are in place; Phase 1 (M1-10 to M1-18) adds the game routes, guesses, progress and stats.

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
- `app/crud/`: the query layer, one file per entity (`app/crud/<entity>.py`). `user.py`, `title.py` and `puzzle.py` for the database, `session.py` for Redis sessions, `password_reset.py` for Redis reset tokens.
- `app/models/`: SQLAlchemy models (`User`; `Game`; `Title`; `Puzzle`, `PuzzleClue` and `ClueMediaFile`).
- `app/games/`: the game domain (routers, schemas, services and game modules arrive in M1-10 and later). Today it holds `normalize.py`, the one normalizer for title names and franchises.
- `app/api/`: `deps.py` (dependencies such as the current user), `health.py` (`/healthz`, `/readyz`), `main.py` (the `/v1` router that aggregates the domain routers).
- `app/core/`: cross-cutting code, namely config, DB engine and session, Redis client, security, exceptions and error handlers, logging, request id, OpenAPI helpers, health checks, the email stub.
- `app/alembic/`: migrations.
- `app/main.py` creates the `FastAPI()` instance, mounts the health router at the root and the API router under `/v1`.

The game domains (Phase 1) follow the same layering. Game code goes in `app/games/` (router, schemas, service, game modules), its queries in `app/crud/<entity>.py` (not in `app/games/`, because puzzles, titles and progress are read by many routes), its tables in `app/models/`.

Database conventions:
- `Base.metadata` has a constraint naming convention (`app/core/db.py`): `ix_`, `uq_`, `ck_`, `fk_` and `pk_` names built from the table and column. Give every `CheckConstraint` a `name=` (the table name is added: `name="status"` becomes `ck_puzzles_status`). The `users` primary key was renamed to `pk_users` in the first game migration, so a migrated database and the one `create_all` builds in tests have the same names.
- A CHECK built from a StrEnum (`enum_check` in `app/core/db.py`: statuses, kinds, media types, clue roles and content types) is stored as text in the database. Changing the enum needs a hand-written migration that drops and re-creates the CHECK, because `alembic check` does not compare CHECK text.
- `Base` sets `__mapper_args__ = {"eager_defaults": True}`, so `updated_at` can be read after a flush with no lazy load (AsyncSession cannot lazy-load). A model that defines its own `__mapper_args__` must keep `"eager_defaults": True`.
- Tables of the game domain have plural names and integer keys; `users` keeps its UUID.
- Puzzle visibility is one SQL condition, `visible_puzzle_condition(today)` in `app/crud/puzzle.py` (scheduled, date today or earlier, on or after the game's launch date, game enabled). Every puzzle query takes it; do not write a second copy.
- Time comes from `app/core/clock.py`: `get_now` (UTC, timezone aware) is a FastAPI dependency (`NowDep`) that tests override, `utc_today(now)` gives the UTC date. Services take `now` as a parameter.
- `normalized_name` and `normalized_franchise` are written only by `create_title` in `app/crud/title.py`, through `normalize_text`.
- Test helpers that build games, titles and puzzles with six clues are in `tests/factories.py`.

Directory naming (`app/`, `app/api/`, `app/core/`) borrows from the [full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template), but that template is flat (single `models.py`/`crud.py`, no service layer) — this project keeps the layering above instead.

## Key architectural decisions
- **Auth**: Django-style server-side sessions, not JWT. On register/login the server creates a Redis key (`session:{opaque token}` → user id, TTL = `SESSION_EXPIRE_MINUTES`) and sets it as a single httpOnly, `SameSite=Lax` session cookie (`session_id`); no access/refresh split. Logout deletes the Redis key and clears the cookie. CSRF protection (M1-41, `app/core/csrf.py`, a pure ASGI middleware that runs before routing, dependencies and body parsing): every request except GET, HEAD and OPTIONS (in practice POST, PUT, PATCH and DELETE) must send an allowed `Origin`, or, if `Origin` is absent, a `Referer` with an allowed origin; otherwise the answer is 403 `csrf_origin_mismatch`, before the session, the body or the route's own rules. GET, HEAD and OPTIONS are not checked. A POST, PUT or PATCH that carries a body must send `Content-Type: application/json` (a charset is fine), otherwise 415 `unsupported_media_type`; a body is any request with `Transfer-Encoding` (whatever `Content-Length` says), a non-zero or unreadable `Content-Length`, so an empty chunked POST without `application/json` gets 415, while a request with no body framing (for example logout from a browser) is never refused for its content type. `SameSite=Lax` stays as defense in depth. There is no custom request header (decision Q18). Allowed origins (`Settings.allowed_origins`, compared as exact scheme, host and port; no wildcard, no suffix match): the origin of `FRONTEND_URL` everywhere; in `local` and `test` also `BACKEND_CORS_ORIGINS` and `API_BASE_URL` (default `http://localhost:8000`, so "Try it out" on `/docs` works). uvicorn prints `http://127.0.0.1:8000`, but open `/docs` at `http://localhost:8000` for "Try it out": `127.0.0.1` is not an allowed origin. With curl or a script, add `-H 'Origin: http://localhost:5173'` (and `-H 'Content-Type: application/json'` when sending a body). Tests: the `client` fixture sends `Origin: http://localhost:5173` by default. Every `application/problem+json` error response of the API, auth included, carries `Cache-Control: no-store`; CORSMiddleware's own plain-text 400 on a refused preflight (dev only) is not one of them. This API is always the source of truth for validation — the frontend's client-side checks are a UX convenience only, never trusted as-is. Dev note: the session cookie is host-scoped, so the frontend must reach the backend via `http://localhost:8000` (not `127.0.0.1:8000`) for the cookie to round-trip from `http://localhost:5173`.
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
