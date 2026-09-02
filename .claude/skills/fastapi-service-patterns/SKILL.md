---
name: fastapi-service-patterns
description: Service/repository-layer architecture rules and style conventions for this FastAPI backend — thin routers, encapsulated queries, N+1 avoidance, testing requirements. Adapted from a Django code-review checklist to this repo's router/service/repository/model layering. Load when writing or reviewing router, service, repository, or model code, or before a code review of backend changes.
---

# FastAPI service patterns

Adapted from a Django "thin views, thick models" (Two Scoops of Django)
review checklist to this repo's layering: `router.py` → `service.py` →
`repository.py` → `models.py` (per `CLAUDE.md` Architecture, currently
implemented in `app/auth/`).

## Thin routers, thick services

- Routers only: parse/validate the request (Pydantic schema), call a service
  function, shape the result into a response schema. No business logic, no
  direct DB session use, no direct ORM mutation in a router.
- Never call `session.add()`, `session.commit()`, or mutate an ORM instance's
  attributes directly from a router — always go through a service function.
  (Django analogue: never call `model.save()`/`form.save()` from a view.)
- Encapsulate state-changing operations into service functions, not scattered
  across routers.

## Query encapsulation

- Routers and services never build a raw `select()` statement or call
  `session.execute()`/`session.scalars()` directly — that belongs in the
  repository layer (`app/<domain>/repository.py`). Services orchestrate and
  apply business rules; repositories are the only place that talks to the DB
  in query form. (Django analogue: using `filter()`/`order_by()` directly
  instead of a QuerySet method is an anti-pattern.)
- Need an alternate constructor for a model instance? Make it a repository
  factory method or a model classmethod, not ad-hoc construction inline.
- Logic that operates on a single model instance → a method or `@property`
  on the model. Logic that operates on a set of instances of the same model
  → a repository method. (Direct analogue of Django's model-method vs.
  manager/QuerySet-method split.)

## Bulk operations

- Avoid raw bulk `update()`/`insert()` statements that skip ORM
  instrumentation (validators, hybrid properties, event listeners) unless
  there's a specific, noted performance reason — same caution as Django's
  `queryset.update()`/`bulk_create()` skipping `save()`/signals.

## Normalization and validation

- Enforce DB normalization; derive don't duplicate — use `@property` /
  `hybrid_property` or query-time annotations instead of denormalized columns
  kept in sync by hand.
- Raise on unexpected/invalid state via `app/core/exceptions.py` — don't
  silently swallow or return `None` for a state that should never occur
  ("dead programs tell no lies").

## Performance

- Watch for N+1 queries — use `selectinload`/`joinedload` for known access
  patterns (e.g. a list endpoint that touches a relationship per row).
  Don't over-optimize a single-row lookup that doesn't need it.
- Avoid eagerly materializing an unbounded result set in async code (e.g.
  loading an entire table into a `list` when a paginated/streamed read would
  do).

## Tests

- Every endpoint touched in a PR needs at least one test. Not optional.
  Use this repo's `tests/conftest.py` fixtures (`client`, `db_session`) —
  see `CLAUDE.md`'s Project state section for how they're wired (per-test
  transaction rollback via savepoint, `get_db` overridden to `db_session`).

## FastAPI/async-specific (not in the Django source, added for this stack)

- Never return an ORM model instance directly from a router — always shape
  the response through a Pydantic response schema.
- One DB session per request via the `get_db` dependency (`app/core/db.py`).
  Never construct an ad-hoc session inside a service.
- No blocking calls inside `async def` code paths (sync HTTP clients, sync
  boto3 calls, etc.) — async all the way through, per `CLAUDE.md` Conventions.
- `custom_attributes` validation against a category's `field_schema` is
  dynamic (Pydantic `create_model`), not a hardcoded model per category —
  see the `dynamic-category-schema` skill.

## Style

- `is_` prefix for booleans.
- No `print()` — this is a served API, not a script.
- f-strings, not `%`-formatting.
- Imports at the top of the file, unless needed inline to break a circular
  import.
- No 1-2 character variable names (except widely-accepted ones).
- A method that takes no args beyond `self` → make it a `@property`.
- Magic constants → `enum.Enum` / `enum.StrEnum`, not bare strings/ints.

## Review workflow

For the big-picture, cross-flow part of a review (how changed code affects
connected user flows, not just local correctness), launch the `flows-review`
agent via the Agent tool (`subagent_type: "flows-review"`) before or alongside
applying the checklist above — it traces the call hierarchy of changed code
and flags logical inconsistencies introduced into connected flows. Merge its
findings with the checklist findings rather than duplicating.
