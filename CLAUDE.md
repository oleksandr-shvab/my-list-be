# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# item-list-backend

## Project state

Dependency management runs on uv: direct dependencies are in `pyproject.toml` (runtime under `[project]`, test and lint tools under the `dev` group) and every version, direct or transitive, is pinned in `uv.lock`. Core infra (`app/core/config.py` Settings, `app/core/db.py` async engine/session, Alembic under `app/alembic/`) is wired up. `items` and `categories` domains are intentionally not scaffolded yet — they'll be added when development reaches them. `app/auth/` is implemented: register/login/logout via Redis-backed sessions (`POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`) — password reset/forgot-password is intentionally not built yet. There is no test suite yet — no test files exist — but the pytest harness is wired up: `tests/conftest.py` provides session-scoped `engine` (auto-creates `TEST_DATABASE_URL`'s database, creates/drops all tables), function-scoped `db_session` (one transaction per test, rolled back after via a savepoint-joined `AsyncSession`, so `service`-layer `commit()` calls don't leak between tests), and `client` (httpx `ASGITransport` client with `get_db` overridden to `db_session`). `asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope` are pinned to `session` in `pyproject.toml` so the session-scoped engine and per-test event loops don't end up on different asyncio loops (a real asyncpg failure mode otherwise). `TEST_DATABASE_URL` defaults to the same host/port as `DATABASE_URL` with a `_test` suffix.

`redis` is now installed and used for auth session storage (`app/core/redis.py`). `arq` and an S3/MinIO client are still named in the Stack below but **not yet installed** — they're needed for background jobs and image storage, both tied to the `items` domain, so add them when that work resumes rather than now.

Errors, health and config (2026-10-27): every error body follows the contract in `../CONTRACTS.md` ("Errors"): raise an `AppException` subclass with a `code` (`app/core/exceptions.py`), never an English-only `detail`; handlers live in `app/core/errors.py` and every response carries `X-Request-ID`. `GET /healthz` and `GET /readyz` exist (`app/api/health.py`). `ENVIRONMENT` is `local`, `test`, `staging` or `production`; the last two refuse to start on a placeholder `SECRET_KEY`, a non-https `FRONTEND_URL` or localhost CORS origins (`app/core/config.py`). Passwords use argon2id (`app/core/security.py`, async wrappers). The test suite points the app at `TEST_DATABASE_URL` and `TEST_REDIS_URL` (it drops tables and flushes that Redis database, so both must differ from the dev ones). CI is `.github/workflows/ci.yml` (lint, test, migrations).

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
uv run ruff format .                           # format
uv run pytest                                  # run tests (needs Postgres and Redis, see below)
uv run alembic revision --autogenerate -m "message"   # new migration
uv run alembic upgrade head                    # apply migrations
```

Commit `uv.lock` with every dependency change; CI runs `uv sync --locked` and fails when the lock is stale.

Copy `.env.example` to `.env` and adjust values before running anything that touches the DB (`DATABASE_URL` points at Postgres; no docker-compose exists yet, so a local/external Postgres instance is assumed).

## What this is
A FastAPI backend for a flexible item/list tracker: users create lists of "things they want" — a shopping list with prices, a personal collection without prices, etc. Each item belongs to a category, has tags and images, plus a set of fields that vary by category (price, size, condition, description, ...).

This is a learning/practice project built deliberately with production-grade patterns — the goal is real-world architecture, not shortcuts.

## This repo
FastAPI backend only. Frontend is a separate repo (`item-list-frontend`), a Vite/React SPA running at `http://localhost:5173` in dev.

## Stack
- FastAPI, Python 3.12, managed with **uv** (`pyproject.toml` + `uv.lock`, `.venv` created by `uv sync`)
- SQLAlchemy 2.0 **async** + asyncpg, PostgreSQL
- Alembic (async template) for migrations
- arq + Redis for background jobs (e.g. thumbnail generation after upload)
- S3-compatible storage (MinIO locally) for images
- Session-based auth: opaque server-side session token in an httpOnly cookie, session store is Redis (no Postgres sessions table, no JWT)

## Architecture
Layered, per-domain structure under `app/`. Each domain gets its own `router.py` / `schemas.py` / `models.py` / `service.py` / `repository.py` (currently only `auth/`; `items` and `categories` will follow the same pattern when added). Domain routers are aggregated in `app/api/main.py` and mounted onto the `FastAPI()` instance in `app/main.py`. Shared cross-cutting code (config, DB session, security, exceptions) lives in `app/core/`. Migrations live in `app/alembic/`.

Directory naming (`app/`, `app/api/`, `app/core/`) borrows from the [full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template), but that template is flat (single `models.py`/`crud.py`, no service/repository layer) — this project keeps the per-domain layering above instead.

## Key architectural decisions
- **Variable fields per category**: hybrid model. `Item` has fixed columns (title, category_id, tags, images) plus a `custom_attributes` JSONB column. `Category` stores a `field_schema` (JSON list of `{key, type, required}`) describing what's expected in `custom_attributes` for items in that category. Validate `custom_attributes` against the category's `field_schema` dynamically in the service layer (e.g. via Pydantic's `create_model`) — don't hardcode a Pydantic model per category.
- **Auth**: Django-style server-side sessions, not JWT. On register/login the server creates a Redis key (`session:{opaque token}` → user id, TTL = `SESSION_EXPIRE_MINUTES`) and sets it as a single httpOnly, `SameSite=Lax` session cookie (`session_id`); no access/refresh split. Logout deletes the Redis key and clears the cookie. `SameSite=Lax` is judged sufficient CSRF protection for the current (single mutating-domain) scope — revisit once more mutating routes exist (e.g. `items`). This API is always the source of truth for validation — the frontend's client-side checks are a UX convenience only, never trusted as-is. Dev note: the session cookie is host-scoped, so the frontend must reach the backend via `http://localhost:8000` (not `127.0.0.1:8000`) for the cookie to round-trip from `http://localhost:5173`.
- **Images**: presigned upload URLs. The API issues a presigned PUT URL; the frontend uploads directly to storage; the API only ever receives and stores the resulting object key. A background job (arq) generates thumbnails after upload completes.
- **CORS**: allow `http://localhost:5173` in dev. This becomes real production config later (whatever domain the frontend deploys to), not just a local convenience to remove.

## Conventions
- Run everything through `uv run ...` (or `.venv/bin/...`); `uv.lock` is the source of truth for pinned versions
- Async all the way through DB access; sync only for genuinely CPU-bound or blocking code
- Tests: pytest + pytest-asyncio + httpx `ASGITransport`
- Lint/format: ruff

## Backend conventions
Service/repository layering rules and style conventions live in project skills, not here, since they're only relevant while actively writing router/service/repository code — see `fastapi-service-patterns` and `dynamic-category-schema` under `.claude/skills/`.

## Related
Frontend repo: `item-list-frontend` (has its own `CLAUDE.md` with the corresponding frontend-side decisions)

Cross-repo contract lives in `../CONTRACTS.md` — check/update it when changing anything on the FE/BE boundary.
