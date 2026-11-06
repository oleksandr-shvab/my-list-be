"""Signed links for the local media route (AC-70, AC-73).

A bearer link: the signature (HMAC-SHA256 over key and expiry) is the only proof, so anyone holding
the link can open it until it expires.
"""

import base64
import hashlib
import hmac
import re

# The media key is a SHA-256 hex digest (AC-74). Matching this shape before anything else keeps
# every other character, so path traversal included, out of the file path.
MEDIA_KEY_PATTERN = re.compile(r"[a-f0-9]{64}", re.ASCII)


def is_media_key(value: str) -> bool:
    return MEDIA_KEY_PATTERN.fullmatch(value) is not None


def sign_link(key: str, expires_at: int, secret: bytes) -> str:
    message = f"{key}:{expires_at}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def is_valid_link(key: str, expires_at: int, signature: str, *, secret: bytes, now: int) -> bool:
    """Constant-time compare, so response time does not reveal how many leading characters were
    right."""
    expected = sign_link(key, expires_at, secret)
    is_signature_valid = hmac.compare_digest(signature.encode(), expected.encode())
    return is_signature_valid and now < expires_at
