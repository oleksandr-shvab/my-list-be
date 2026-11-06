"""Proof that a guest saw a puzzle on its own day (Q2).

A merge entry only says `played_as_daily`, and trusting that flag alone lets a client claim a
streak it never played. So a guest's view on the puzzle's own day carries `daily_proof`.

The proof is `base64url(HMAC-SHA256(key, "<slug>:<variant>:<YYYY-MM-DD>"))`, unpadded. The key is
derived from `SECRET_KEY` with its own label, so it is neither `SECRET_KEY` nor
`MEDIA_SIGNING_KEY`. It has no expiry because the date is in the message. It is a bearer value.
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
    """Cached by value, so a changed `SECRET_KEY` derives another key."""
    return hmac.new(secret.encode(), KEY_LABEL, hashlib.sha256).digest()


def _key() -> bytes:
    return _derive_key(settings.SECRET_KEY.get_secret_value())


def sign(slug: str, variant: str, puzzle_date: dt.date) -> str:
    message = f"{slug}:{variant}:{puzzle_date.isoformat()}".encode()
    digest = hmac.new(_key(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify(proof: str | None, slug: str, variant: str, puzzle_date: dt.date) -> bool:
    if proof is None:
        return False
    expected = sign(slug, variant, puzzle_date)
    return hmac.compare_digest(proof.encode(), expected.encode())
