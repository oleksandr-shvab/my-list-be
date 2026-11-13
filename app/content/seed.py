"""The seed of the puzzle builder (R-8, SPEC-04 AC-26 to AC-28).

Every random choice of a build comes from a seed that only the owner can make: an HMAC of the game,
variant, slot and generator version, keyed with the secret `PUZZLE_SEED_SALT`. The public code and
IGDB's public data are not enough to predict an answer without the salt. A plain hash would be.

`GENERATOR_VERSION` belongs to the builder; it is raised whenever the choice logic changes, because
the same inputs must then give other choices.
"""

import hashlib
import hmac
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import SecretStr

from app.core.config import Settings, is_weak_secret


class SaltError(Exception):
    """The salt cannot be used. The message names the problem and never holds the salt."""


class Purpose(StrEnum):
    """One random stream for each kind of choice, so a change in one never moves another."""

    ANSWER = "answer"
    SCREENSHOTS = "screenshots"
    CROP = "crop"
    CLIP = "clip"


def check_salt(
    salt: SecretStr | str | None, other_secrets: Iterable[SecretStr | str | None]
) -> str:
    """The salt as text, or `SaltError` when it is missing, weak (the rule of `SECRET_KEY`) or equal
    to another secret of the project (AC-27). The builder calls it before it reads IGDB."""
    value = salt.get_secret_value() if isinstance(salt, SecretStr) else salt
    if not value:
        raise SaltError("PUZZLE_SEED_SALT is not set")
    if is_weak_secret(value):
        raise SaltError("PUZZLE_SEED_SALT is a placeholder or too short")
    for other in other_secrets:
        other_value = other.get_secret_value() if isinstance(other, SecretStr) else other
        if other_value and hmac.compare_digest(value.encode(), other_value.encode()):
            raise SaltError("PUZZLE_SEED_SALT must differ from every other secret")
    return value


def reserved_secrets(
    app_settings: Settings, igdb_client_secret: SecretStr | None
) -> list[SecretStr | None]:
    """The secrets the salt must differ from (AC-27)."""
    return [
        app_settings.SECRET_KEY,
        app_settings.MEDIA_SIGNING_KEY,
        app_settings.S3_SECRET_ACCESS_KEY,
        igdb_client_secret,
    ]


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


@dataclass(frozen=True)
class SlotSeed:
    """The seed of one build slot. The salt is left out of `repr`, so it never reaches a log."""

    salt: str = field(repr=False)
    slug: str
    variant: str
    slot: int
    version: str

    @property
    def seed(self) -> bytes:
        return _hmac(self.salt.encode(), f"{self.slug}:{self.variant}:{self.slot}:{self.version}")

    def stream(self, purpose: Purpose) -> random.Random:
        """A fresh stream for one purpose: the same slot and purpose always give the same numbers."""
        return random.Random(int.from_bytes(_hmac(self.seed, purpose), "big"))
