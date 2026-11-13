"""The seed of the puzzle builder (R-8, SPEC-04 AC-26 to AC-28).

Every random choice of a build comes from a seed that only the owner can make: an HMAC of the game,
variant, slot and generator version, keyed with the secret `PUZZLE_SEED_SALT`. The public code and
IGDB's public data are not enough to predict an answer without the salt. A plain hash would be.

The salt stays a `SecretStr` until the HMAC call, so it is not in a `repr`, an `asdict` or a
traceback with locals, and a `SlotSeed` cannot be pickled. The choices use only `random()` of the
stream (the one method of the `random` module whose output is guaranteed across Python versions)
through `draw_index` and `draw_distinct`, so a new Python cannot change a build.

`GENERATOR_VERSION` belongs to the builder; it is raised whenever the choice logic changes, because
the same inputs must then give other choices.
"""

import hashlib
import hmac
import json
import random
import sys
from collections.abc import Iterable, Sequence
from dataclasses import KW_ONLY, InitVar, dataclass, field
from enum import StrEnum
from typing import NoReturn

from pydantic import SecretStr

from app.core.config import DEV_MEDIA_SIGNING_KEY, MIN_SECRET_KEY_LENGTH, Settings, is_weak_secret

# The salt the tests use. It is public, so `check_salt` refuses it unless pytest is imported
# (`under_pytest`): a script never imports pytest, so it cannot use it.
TEST_SALT = "pytest-fixed-salt-0123456789-abcdefghijklmnopqrstuvwxyz"
# Values that are in the repo or its docs, so anybody can try them: the tests' salt, the dev default
# of `MEDIA_SIGNING_KEY`, and the placeholder `SECRET_KEY` of `.env.example`.
PUBLIC_SALTS = frozenset(
    {
        TEST_SALT,
        DEV_MEDIA_SIGNING_KEY,
        "change-me-to-a-random-string-of-at-least-32-characters",
    }
)
# A text that says it is a placeholder is refused, whatever its length.
PLACEHOLDER_MARKERS = ("change-me", "dev-only", "do-not-use")
# A salt of at least 32 characters that uses fewer different characters is not random ("aaaa...",
# "abababab..."). A random 32-character hex text has about 14 (fewer than 8 is a chance of about
# one in 30 million), a base64 one about 26.
MIN_DISTINCT_CHARACTERS = 8


class SaltError(Exception):
    """The salt cannot be used. The message names the problem and never holds the salt."""


class Purpose(StrEnum):
    """One random stream for each kind of choice, so a change in one never moves another."""

    ANSWER = "answer"
    SCREENSHOTS = "screenshots"
    CROP = "crop"
    CLIP = "clip"


def under_pytest() -> bool:
    """True when pytest is imported. `PYTEST_CURRENT_TEST` is not set while pytest imports test
    modules, so it cannot be the test. A script never imports pytest."""
    return "pytest" in sys.modules


def check_salt(salt: SecretStr | None, other_secrets: Iterable[SecretStr | None] = ()) -> SecretStr:
    """The salt, or `SaltError` when it is missing, weak (the rule of `SECRET_KEY`), public (a dev
    default, a placeholder, and the test salt unless pytest is imported), too regular, or equal to
    another secret of the project (AC-27). The builder calls it before it reads IGDB.

    No text of the salt is kept in a local variable here, so a traceback with locals never shows it.
    """
    if salt is None or not salt.get_secret_value():
        raise SaltError("PUZZLE_SEED_SALT is not set")
    if is_weak_secret(salt.get_secret_value()):
        raise SaltError("PUZZLE_SEED_SALT is a placeholder or too short")
    public = PUBLIC_SALTS - {TEST_SALT} if under_pytest() else PUBLIC_SALTS
    if salt.get_secret_value() in public or any(
        marker in salt.get_secret_value().lower() for marker in PLACEHOLDER_MARKERS
    ):
        raise SaltError("PUZZLE_SEED_SALT is a value that is in the public repo or its docs")
    if len(set(salt.get_secret_value())) < MIN_DISTINCT_CHARACTERS:
        raise SaltError(
            f"PUZZLE_SEED_SALT is too regular: use at least {MIN_DISTINCT_CHARACTERS} different "
            f"characters among {MIN_SECRET_KEY_LENGTH} or more"
        )
    for other in other_secrets:
        if (
            other is not None
            and other.get_secret_value()
            and hmac.compare_digest(
                salt.get_secret_value().encode(), other.get_secret_value().encode()
            )
        ):
            raise SaltError("PUZZLE_SEED_SALT must differ from every other secret")
    return salt


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
    """The seed of one build slot. Making one checks the salt (`check_salt`, with `other_secrets`),
    so a missing, weak or public salt cannot make a seed. `other_secrets` is required, by keyword
    (pass `reserved_secrets(...)`, or `()` in a test), and `dataclasses.replace` asks for it again."""

    salt: SecretStr = field(repr=False)
    slug: str
    variant: str
    slot: int
    version: str
    _: KW_ONLY
    other_secrets: InitVar[Iterable[SecretStr | None]]

    def __post_init__(self, other_secrets: Iterable[SecretStr | None]) -> None:
        check_salt(self.salt, other_secrets)

    def __reduce__(self) -> NoReturn:
        raise TypeError("a SlotSeed holds the salt and cannot be pickled or copied")

    @property
    def seed(self) -> bytes:
        # A JSON array, so no field can run into the next one ("a:b" and "c" against "a" and
        # "b:c"), and a number stays apart from text.
        message = json.dumps([self.slug, self.variant, self.slot, self.version], ensure_ascii=True)
        return _hmac(self.salt.get_secret_value().encode(), message)

    def stream(self, purpose: Purpose) -> random.Random:
        """A fresh stream for one purpose: the same slot and purpose always give the same numbers."""
        return random.Random(int.from_bytes(_hmac(self.seed, purpose), "big"))


def draw_index(stream: random.Random, bound: int) -> int:
    """An index in `range(bound)` from one `random()` (guaranteed stable across Python versions)."""
    if bound < 1:
        raise ValueError("bound must be at least 1")
    return min(int(stream.random() * bound), bound - 1)


def draw_distinct[T](stream: random.Random, items: Sequence[T], count: int) -> list[T]:
    """`count` different items, in drawing order: each draw takes one item out of what is left."""
    if not 0 <= count <= len(items):
        raise ValueError("cannot draw that many items")
    left = list(items)
    return [left.pop(draw_index(stream, len(left))) for _ in range(count)]
