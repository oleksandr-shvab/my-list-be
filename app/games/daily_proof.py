"""The proof that a guest saw a puzzle on its own day (Q2, `played_as_daily` of a merge entry).

A guest's `counts_for_streak` lives in the browser, and a merge entry only says `played_as_daily`.
Trusting that flag alone lets a client claim a streak it never played. So every view a guest gets
on the puzzle's own day carries `daily_proof`, and a merge counts `played_as_daily` only with the
proof of that puzzle.

The proof is `base64url(HMAC-SHA256(key, "<slug>:<variant>:<date>"))`, unpadded, with the date as
`YYYY-MM-DD`. The key is derived from `SECRET_KEY` with its own label, so it is neither
`SECRET_KEY` nor `MEDIA_SIGNING_KEY`. It has no expiry: it says "this puzzle was seen on its own
day", and the date is in the message. It is a bearer value, like a signed media link.
"""

import base64
import datetime as dt
import functools
import hashlib
import hmac

from app.core.config import settings

KEY_LABEL = b"daily-proof-v1"


@functools.cache
def _derive_key(secret: str) -> bytes:
    """Cached by the secret's value: a changed `SECRET_KEY` is another argument, so it derives
    another key and never reuses the old one."""
    return hmac.new(secret.encode(), KEY_LABEL, hashlib.sha256).digest()


def _key() -> bytes:
    return _derive_key(settings.SECRET_KEY.get_secret_value())


def sign(slug: str, variant: str, puzzle_date: dt.date) -> str:
    message = f"{slug}:{variant}:{puzzle_date.isoformat()}".encode()
    digest = hmac.new(_key(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify(proof: str | None, slug: str, variant: str, puzzle_date: dt.date) -> bool:
    """True when `proof` is the proof of this puzzle. Compared in constant time."""
    if proof is None:
        return False
    expected = sign(slug, variant, puzzle_date)
    return hmac.compare_digest(proof.encode(), expected.encode())
