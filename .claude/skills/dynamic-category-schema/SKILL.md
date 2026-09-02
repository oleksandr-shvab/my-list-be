---
name: dynamic-category-schema
description: How to validate Item.custom_attributes against a Category's field_schema dynamically using Pydantic's create_model, without hardcoding a schema per category. Load when working on the items/categories domains, custom_attributes validation, or if field_schema's shape changes.
---

# Dynamic category schema (backend)

Forward-looking: `items` and `categories` aren't scaffolded yet (see
`CLAUDE.md`'s Project state). This documents the intended pattern for when
that work starts, mirroring the frontend's `dynamic-category-schema` skill
(`item-list-frontend/.claude/skills/dynamic-category-schema/`) so both repos
build the same mental model of the contract from day one.

## The contract

- `Category.field_schema`: a JSON list of `{key, type, required}` objects
  describing the variable fields expected in `custom_attributes` for items in
  that category (per `CLAUDE.md`'s "Key architectural decisions" and
  `../CONTRACTS.md`).
- `Item.custom_attributes`: a JSONB column holding the actual values for
  those fields, alongside `Item`'s fixed columns (title, category_id, tags,
  images).

## Validation pattern

- Validate `custom_attributes` against the owning category's `field_schema`
  **dynamically, in the service layer**, using Pydantic's `create_model()` —
  map each field's `type` string (e.g. `"str"`, `"int"`, `"float"`,
  `"bool"`) to the corresponding Python type, and each `required: false`
  field to an `Optional[...]` with a default.
- Never hand-write a Pydantic model per category — that defeats the point of
  a category-defined schema and means a new category requires a code change.
- Build the dynamic model once per validation call (or cache it keyed by the
  category's `field_schema` if it becomes a hot path) — don't rebuild it
  field-by-field inline.

## Cross-repo sync

- The frontend builds a parallel **Zod** schema at runtime from this same
  `{key, type, required}` JSON (its `dynamic-category-schema` skill) to
  render and validate the category's custom fields client-side.
- Per `../CONTRACTS.md`, this is the single highest-risk drift point between
  the two repos: if the `{key, type, required}` shape changes (new field,
  renamed key, new `type` value, etc.), update this backend's
  `create_model()` mapping, the frontend's runtime Zod builder, and
  `../CONTRACTS.md` in the same pass — not as separate follow-ups.
