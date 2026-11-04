# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Daily Game Challenge backend

## Project state

Dependency management runs on uv: direct dependencies are in `pyproject.toml` (runtime under `[project]`, test and lint tools under the `dev` group) and every version, direct or transitive, is pinned in `uv.lock`. Core infra (`app/core/config.py` Settings, `app/core/db.py` async engine/session, Alembic under `app/alembic/`) is wired up. The only domain so far is `app/auth/`: register, login, logout and the current user via Redis-backed sessions (`POST /v1/auth/register`, `POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/me`), plus forgot, reset and update password (`POST /v1/auth/forgot-password`, `POST /v1/auth/reset-password`, `POST /v1/auth/update-password`). The game data model exists (M1-10a: `games`, `titles`, `puzzles`, `puzzle_clues`, `clue_media_files`), and the first game routes do too (M1-10): the game list, today's puzzle and a puzzle by date. `GET /v1/clock` (M1-17) returns the server's time. The title search (M1-11) is `GET /v1/games/{slug}/titles?q=`. The guest guess route (M1-12) is `POST /v1/games/{slug}/puzzles/{date}/guesses`. Progress, stats, media, merge and the archive arrive in the rest of Phase 1 (see "What this is"). The suite lives in `tests/`. `tests/conftest.py` provides session-scoped `engine` (auto-creates `TEST_DATABASE_URL`'s database, creates/drops all tables), function-scoped `db_session` (one transaction per test, rolled back after via a savepoint-joined `AsyncSession`, so `service`-layer `commit()` calls don't leak between tests), and `client` (httpx `ASGITransport` client with `get_db` overridden to `db_session`). `asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope` are pinned to `session` in `pyproject.toml` so the session-scoped engine and per-test event loops don't end up on different asyncio loops (a real asyncpg failure mode otherwise). `engine` also runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` before `create_all`, because the tests build tables without migrations and the title indexes need the extension. `TEST_DATABASE_URL` defaults to `localhost:5433/daily_game_challenge_test`, the same host and port as the dev database with a `_test` suffix.

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
The FastAPI backend of Daily Game Challenge, a site with daily puzzle games (product spec: `../SPEC.md`, plan: `../MILESTONE-1.md`). Today it has accounts (auth and password flows), the health checks, the error contract and the OpenAPI snapshot. The game tables, the puzzle visibility rule, the clock dependency, the game list and the puzzle view are in place; the title search is in too (M1-11), and so is the guest guess (M1-12); the rest of Phase 1 (M1-13 to M1-18) adds progress, media, stats, merge and the archive.

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
- `app/crud/`: the query layer, one file per entity (`app/crud/<entity>.py`). `user.py`, `game.py`, `title.py` and `puzzle.py` for the database, `session.py` for Redis sessions, `password_reset.py` for Redis reset tokens.
- `app/models/`: SQLAlchemy models (`User`; `Game`; `Title`; `Puzzle`, `PuzzleClue` and `ClueMediaFile`).
- `app/clock/`: `GET /v1/clock` (`now`, `today`, `next_reset`, `Cache-Control: no-store`), built only from the clock dependency: no session, no database. The client uses it for display only; "today" for every game rule is decided on the server.
- `app/games/`: the game domain. `router.py` (tag `games`, mounted at `/v1/games`), `schemas.py`, `service.py`, `enums.py` (`ProgressStatus`, `GuessResult`), `normalize.py` (the one normalizer for title names and franchises) and `modules/`: the game modules, one per game, registered by slug in `modules/__init__.py` (a game row with no module answers `game_not_found` and is logged at debug level).
- `app/api/`: `deps.py` (dependencies such as the current user, and `OptionalUser`, which is `None` for a guest), `health.py` (`/healthz`, `/readyz`), `main.py` (the `/v1` router that aggregates the domain routers).
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
- Time comes from `app/core/clock.py`: `get_now` (UTC, timezone aware) is a FastAPI dependency (`NowDep`) that tests override, `utc_today(now)` gives the UTC date, `next_utc_midnight(now)` the next 00:00 UTC (strictly after `now`). Services take `now` as a parameter.
- `titles.normalized_name` and `titles.normalized_franchise` have GIN indexes with `gin_trgm_ops` (`ix_titles_normalized_name_trgm`, `ix_titles_normalized_franchise_trgm`), created by the migration that also runs `CREATE EXTENSION IF NOT EXISTS pg_trgm` (a trusted extension, so a database owner can create it). `alembic check` does not compare operator classes: after editing such a migration, compare a database built by `alembic upgrade head` with one built by `create_all` (`pg_indexes`, `pg_get_constraintdef`).
- `normalized_name` and `normalized_franchise` are written only by `create_title` and `update_title` in `app/crud/title.py`, through `normalize_text`.
- Test helpers that build games, titles and puzzles with six clues are in `tests/factories.py`.

Game domain (M1-10):
- Routes: `GET /v1/games` (enabled games that have a module, `public, max-age=300`), `GET /v1/games/{slug}/puzzles/today` and `GET /v1/games/{slug}/puzzles/{date}` (`private, no-store`). Both puzzle routes take an optional `variant` (default: the game's default variant, named `default`). Declare `today` before the dated route, or it is read as a date and fails with 422.
- A game module (`app/games/modules/base.py`) works on plain dataclasses and does no I/O, so a guest's replay and an account's stored state can run the same code. It has `max_guesses`, `has_variant`, `get_puzzle` and `submit_guess`. `get_puzzle` builds the view from the puzzle, the progress state and whether the caller is signed in; it shows steps 1 to `guesses_used + 1` while the game runs and all six, with `source_url` and the answer, at the end. A scheduled puzzle that does not have exactly the steps 1 to 6 raises `PuzzleDataError` (`app/core/exceptions.py`), which is a 500 `internal_error`: the response says nothing about the cause, and the handler logs the reason.
- The service checks in this order: the game (enabled, with a module; else `game_not_found`), the variant (else `variant_not_found`), then the visible puzzle (else `puzzle_not_found`). The puzzle is loaded with its answer title and its clues in one query (`get_visible_puzzle_with_details`; the `Puzzle` relationships are `lazy="raise"`, so any other access fails loudly instead of lazy-loading). Every hidden puzzle (draft, not scheduled, future date, before launch) raises the same `PuzzleNotFoundError` with the same detail, so the 404 bodies differ only in `request_id`.
- `OptionalUser`: a missing, unknown or expired cookie, or an inactive user, is a guest and the view says `signed_in: false`. Game routes never answer 401 (stats and merge will).
- Path and query dates use `StrictDate` (`app/games/schemas.py`): exactly `YYYY-MM-DD` and a real day, else 422 `validation_error` with `field: "date"`. Validation errors name the bare field: the first part of `loc` (`body`, `path` or `query`) is dropped, a later part with the same name is kept (`field_from_location` in `app/core/errors.py`).
- The view number is days since the game's launch date plus 1. In M1-10 every request is read with an empty progress state, so `counts_for_streak` is `null`; stored progress arrives in M1-13.

Guess (M1-12): `POST /v1/games/{slug}/puzzles/{date}/guesses` (`games_submit_guess`, `private, no-store`) takes `{title_id: int | null, previous: [int | null]}` (both required; `null` is a skip; `previous` capped at 20 entries by the schema, a size guard that gives `validation_error`) and returns the new view. Until M1-13 every request is handled as a guest's, whoever signs it, and nothing is stored (AC-37).
- The rules are in `GameGuesserModule.submit_guess` (pure, `app/games/modules/game_guesser.py`): correct when the title is the answer or has the same normalized name; `franchise` when wrong and both normalized franchises are present and equal (a missing franchise never matches); a skip is `skipped`; the sixth guess or skip that is not correct loses; the score is `guesses_used`. It raises its own errors (`TitleAlreadyGuessedError`, `TitleKindMismatchError`, `GameFinishedError`, in `modules/base.py`, checked in the order finished, kind, duplicate) and changes nothing; the service maps them to the API's codes. `TitleData` has a `kind` and `PuzzleData` a `title_kind` for the kind check.
- `submit_guess` in `app/games/service.py` (the guest flow), in this order: the game, the variant and the visible puzzle (the same `_load_puzzle` as the view); `previous` with `max_guesses` or more entries gives 422 `invalid_guess_history` (AC-39: over 5 on a guess); one query loads every title of `previous` and the new one (`get_titles_by_ids` in `app/crud/title.py`, which leaves out ids of another kind and ids too large for a 32-bit key, so they are "not found" and never a 500); `previous` is replayed through the module, and an unknown title, a duplicate or an entry after the end is `invalid_guess_history`; a replay that ended the game gives 409 `puzzle_already_finished` (before the new title is looked at); an unknown title or one of another kind is 422 `title_not_found`; a title already in `previous` is 422 `duplicate_guess`. A title counts as already guessed when its id or its normalized name equals that of an earlier guess (the same equivalence as a correct guess, Q9: "Portal 2" and "PORTAL  2" are one guess), so a `previous` with such a pair is `invalid_guess_history`. `counts_for_streak` is "today equals the puzzle's date" when `previous` is empty (so a 23:59:59 view guessed at 00:00:01 is `false`) and `null` otherwise (AC-40).
- Validation codes (`app/core/errors.py`): pydantic's `int_type`, `list_type` and `model_attributes_type` (a body that is not an object) give `invalid_type`, and a list over its `max_length` gives `too_long` with `params: {"max": N}`, like a string. The module's franchise rule needs both normalized franchises to be non-empty, so an empty string never matches. The service maps the module's `TitleKindMismatchError` to `title_not_found` (the SQL kind filter normally stops a foreign title first).
- Exceptions: `TitleNotFoundError`, `DuplicateGuessError`, `InvalidGuessHistoryError` (422) and `PuzzleAlreadyFinishedError` (409) in `app/core/exceptions.py`.
- Tests: the rules are tables in `tests/test_game_guesser.py` (no database); `tests/test_guesses.py` has the replay table, the `counts_for_streak` table, the leak scan over a won and a lost game through the route (the one route happy path), and the request validation table. `set_now` (fix the server clock; it removes its override at teardown) is in `tests/conftest.py`.

Title search (M1-11): `GET /v1/games/{slug}/titles?q=` (`games_search_titles`, `public, max-age=300`) returns `{"items": [{id, name, franchise, year}]}`, at most 200, with no marker for answers. The service checks the game first (`game_not_found`), then asks `search_query` (`app/games/service.py`, the one place for the rule) for the normalized text: `None` for a trimmed `q` under 2 characters or one whose normalized text is under 2 characters ("a.", "a-" and a decomposed "é" become one letter and would match up to 200 titles; "zl" is searchable), which gives an empty list; `q` over 100 characters is 422 `too_long` (`Query(max_length=100)`). The query is `search_titles` in `app/crud/title.py`: it sets `pg_trgm.word_similarity_threshold` to 0.4 for the transaction (`set_config(..., true)`; the default 0.6 misses "zlda"), matches the normalized name or franchise with the `%>` operator (served by the GIN indexes), and orders by exact name, then `starts_with`, then the higher word similarity, then the name with `COLLATE "C"` (code point order, so ties and the cut at 200 are the same whatever the database's default collation is) and the id. The operators and `starts_with` take no wildcards and `normalize_text` removes `%`, `_` and backslashes, so `q` is always plain text (AC-22). Latency (AC-24) is checked by hand, not in CI: `ENVIRONMENT=local uv run python scripts/bench_title_search.py [--titles N]` adds 50,000 synthetic titles to the dev database (every one with the year 9999, a generated-shape name and the video game kind: its marker, so a real title with a placeholder year 9999 is never deleted), runs `VACUUM ANALYZE`, times about 200 searches on a warmed connection (the timed window holds the `set_config` too, not the pool checkout), prints the p95 (target under 150 ms), then deletes the marked rows and vacuums again. The result is printed before the cleanup, and a failed cleanup is reported with exit code 1 instead of hiding it. It also deletes marked rows at the start, so a killed run (SIGTERM, `kill -9`) is cleaned up by the next one. Without the vacuum the GIN pending lists make every search slower.

Seeding a local game. The script refuses to run unless the `ENVIRONMENT` environment variable is exactly `local` (it is not enough that `.env` or the default says so, because it deletes data) and the `DATABASE_URL` host is `localhost`, `127.0.0.1` or `::1`:
```
ENVIRONMENT=local uv run python scripts/seed_dev.py
```
It creates the enabled game `game-guesser` (launch date 3 days ago), 12 titles with full names (some with franchises, "Doom" and "DOOM" as a near-duplicate pair), scheduled puzzles for today and the 3 previous days, one scheduled puzzle for tomorrow and one draft (6 clues each; the step 1 hint is null, and the puzzle for two days ago has a null Metacritic hint at step 2). It is safe to run again: it updates the game, matches titles by name (the lowest id if there are duplicates) and rewrites their franchise, year and normalized columns, and deletes and recreates the game's puzzles. "Today" comes from the project clock (`app/core/clock.py`), so run it again on a new day. There is no media yet (M1-18 adds placeholder images and a clip). Run `uv run alembic upgrade head` first. When port 5433 is taken, set `DATABASE_URL` (and `REDIS_URL`) as in "Commands" on both commands.

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
