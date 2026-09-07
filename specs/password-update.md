# Spec: Password Update (authenticated)

## Problem

A logged-in user can currently only change their password via the "forgot password" email-token flow. There's no way for an already-authenticated user to change their password directly from a profile page. This spec adds that: `old_password` + `new_password` + `confirm_password`, verified against the current session.

This is backend-only. Frontend (profile page, form) lives in `item-list-frontend` and is out of scope here, but the request/response contract below is what that repo will integrate against — update `../CONTRACTS.md` once this ships.

## User flow

1. User is logged in (valid `session_id` cookie) and navigates to their profile page.
2. User fills in old password, new password, confirm new password, submits.
3. Backend verifies the old password against the stored hash, verifies new/confirm match and new != old, updates the password, and revokes every *other* active session for that user (other devices/tabs get logged out; the session making this request stays logged in).
4. Frontend shows success and clears the form. No email notification is sent (out of scope for this iteration).

## API contract

`POST /auth/update-password` — requires authentication (session cookie), same pattern as `/auth/logout`.

Request body (`UpdatePasswordRequest`):
```json
{
  "old_password": "string",
  "new_password": "string, 8-128 chars",
  "confirm_password": "string, 8-128 chars"
}
```

Validation (pydantic, mirrors `ResetPasswordRequest`):
- `new_password` and `confirm_password` must match.
- `new_password` must differ from `old_password`.

Response: `204 No Content` on success (matches `/auth/reset-password`).

### Error cases

| Condition | Status | Detail / mechanism |
|---|---|---|
| No/invalid session cookie | 401 | existing `NotAuthenticatedError` via `CurrentUser` dep |
| `new_password != confirm_password` | 422 | pydantic validation (existing pattern) |
| `new_password == old_password` | 422 | pydantic validation (new) |
| `old_password` doesn't match stored hash | 400 | new `IncorrectPasswordError` |

## Architecture decisions

- **Old password check**: reuse `verify_password` from `app/core/security.py` (same as login) against `current_user.hashed_password`.
- **Password update**: reuse existing `update_user_password(db, user, new_password)` in `app/crud/user.py` — no changes needed there.
- **Session revocation ("log out other sessions")**: Redis sessions are currently keyed only by token (`session:{token} -> user_id`), so there's no way to enumerate a user's active sessions. This requires a new reverse index, following the same pattern already used for password-reset tokens (`password_reset_user:{user_id} -> token`) but as a **set**, since a user can have multiple concurrent sessions (unlike the single-use reset token):
  - New Redis key: `sessions_user:{user_id}` → Set[token], TTL refreshed to `SESSION_EXPIRE_MINUTES * 60` on every add.
  - `create_session` additionally `SADD`s the new token into this set (and sets/refreshes its TTL).
  - `delete_session` additionally removes the token from its user's set (needs the user_id, resolved via existing `get_session(token)` lookup before deleting).
  - New `revoke_other_sessions(user_id, keep_token)`: reads the set, deletes every `session:{token}` key except `keep_token`, and resets the set to contain only `keep_token`.
- **No email notification** on password change (matches team decision — keeps scope tight since this is a same-session authenticated action).
- **No Postgres/model changes** — `User.hashed_password` already exists and is reused as-is.

## Files touched (expected)

- `app/auth/schemas.py` — add `UpdatePasswordRequest`.
- `app/auth/router.py` — add `POST /auth/update-password`, using `CurrentUser` + the session cookie (like `/auth/logout`).
- `app/auth/service.py` — add `update_password(db, user, current_token, old_password, new_password)`.
- `app/crud/session.py` — extend `create_session`/`delete_session` for the reverse index; add `revoke_other_sessions`.
- `app/core/exceptions.py` — add `IncorrectPasswordError` (400).
- `tests/test_password_update.py` — new test file (see below).

## Testing strategy

Mirror `tests/test_password_reset.py` structure/style, kept to the two highest-value cases:
- `test__update_password__ok` — correct old password → 204, new password authenticates, old one doesn't, and a second session for the same user is revoked while the requesting session stays valid (covers the happy path + session revocation in one flow).
- `test__update_password__wrong_old_password` — 400 `IncorrectPasswordError`, password unchanged, other sessions untouched.

## Out of scope

- Email/notification on password change.
- Rate limiting / lockout on repeated wrong `old_password` attempts (nothing like this exists elsewhere in the codebase either — e.g. `/auth/login`).
- Frontend implementation (separate repo, separate spec/PR).
