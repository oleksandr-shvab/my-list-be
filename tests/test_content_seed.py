"""The seed and the salt of the puzzle builder (M1-22b, SPEC-04 AC-26 to AC-28, R-8).

No database and no network. The tests use the public `TEST_SALT`, which a `SlotSeed` takes only
under pytest."""

import dataclasses
import hashlib
import pickle
import random
import sys
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.content.candidates import choose_answer
from app.content.igdb import IgdbGame, parse_game
from app.content.seed import (
    TEST_SALT,
    Purpose,
    SaltError,
    SlotSeed,
    check_salt,
    draw_distinct,
    draw_index,
    reserved_secrets,
)
from app.content.settings import ContentSettings
from app.core.config import DEV_MEDIA_SIGNING_KEY

# Two valid salts: long, with many different characters, not public.
SALT = "k3Jq9vR2xWm7Lp0ZsT5nYc8HbD4fGa1eUo6iXw"
OTHER_SALT = "Zp8Qm1Vc5Xr3Nk7Ty2Lh9Wb4Ds6Fj0GaEu1Io"
OTHER_SECRET = "some-other-secret-" + "c" * 30


def secret(value: str | None) -> SecretStr | None:
    return None if value is None else SecretStr(value)


def games(count: int) -> list[IgdbGame]:
    found = []
    for igdb_id in range(1, count + 1):
        game = parse_game(
            {"id": igdb_id, "name": f"Game {igdb_id}", "slug": f"game-{igdb_id}"},
            max_year=2027,
            skips=Counter(),
            drops=Counter(),
        )
        assert game is not None
        found.append(game)
    return found


def seed_of(salt: str, slot: int = 1, *, version: str = "1") -> SlotSeed:
    return SlotSeed(SecretStr(salt), "game-guesser", "default", slot, version, other_secrets=())


def answers(salt: str, candidates: list[IgdbGame], *, version: str = "1") -> list[int]:
    """The answer of slots 1 to 12."""
    return [
        choose_answer(candidates, seed_of(salt, slot, version=version).stream(Purpose.ANSWER)).id
        for slot in range(1, 13)
    ]


# Each row is a salt that must be refused, and the words of the reason.
REFUSED = [
    (None, "is not set"),
    ("", "is not set"),
    ("short", "too short"),
    ("Zq8Xk2Lm9Pw4Rt6Yv1Nb", "too short"),  # 20 characters: weak, and not in the message
    ("change-me", "too short"),
    (DEV_MEDIA_SIGNING_KEY, "public repo"),  # a dev default
    ("change-me-to-a-random-string-of-at-least-32-characters", "public repo"),  # .env.example
    ("my-own-dev-only-salt-" + "Zx9" * 6, "public repo"),  # says it is a placeholder
    ("a" * 40, "too regular"),
    ("ab12" * 10, "too regular"),
    ("abcdefg" * 6, "too regular"),  # seven different characters
    (SALT, "must differ"),  # equal to another secret, below
]


@pytest.mark.parametrize(("salt", "message"), REFUSED)
def test__check_salt__refuses_a_missing_weak_public_regular_or_reused_salt(
    salt: str | None, message: str
) -> None:
    with pytest.raises(SaltError, match=message) as refusal:
        check_salt(secret(salt), [secret(OTHER_SECRET), None, SecretStr(SALT)])
    assert len(salt or "") < 12 or salt not in str(refusal.value)
    # The same salt is accepted when nothing is wrong with it (the reused one, with no clash).
    assert check_salt(SecretStr(SALT), [secret(OTHER_SECRET), None]).get_secret_value() == SALT


@pytest.mark.parametrize("name", ["SECRET_KEY", "MEDIA_SIGNING_KEY", "S3_SECRET_ACCESS_KEY"])
def test__reserved_secrets__every_secret_of_the_project_is_refused(name: str) -> None:
    secrets = {
        "SECRET_KEY": "app-secret-" + "1Ab2Cd3Ef" * 3,
        "MEDIA_SIGNING_KEY": "media-key-" + "4Gh5Ij6Kl" * 3,
        "S3_SECRET_ACCESS_KEY": "bucket-key-" + "7Mn8Op9Qr" * 3,
    }
    igdb_secret = "igdb-secret-" + "0St1Uv2Wx" * 3
    app_settings: Any = SimpleNamespace(**{key: SecretStr(value) for key, value in secrets.items()})
    others = reserved_secrets(app_settings, SecretStr(igdb_secret))
    for reused in (secrets[name], igdb_secret):
        with pytest.raises(SaltError, match="must differ"):
            check_salt(SecretStr(reused), others)
    assert check_salt(SecretStr(SALT), others).get_secret_value() == SALT


def test__check_salt__the_test_salt_needs_pytest_and_a_random_hex_salt_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert check_salt(SecretStr(TEST_SALT)).get_secret_value() == TEST_SALT  # pytest is imported
    # A script never imports pytest, so there the test salt is a public value.
    monkeypatch.delitem(sys.modules, "pytest")
    with pytest.raises(SaltError, match="public repo"):
        check_salt(SecretStr(TEST_SALT))
    monkeypatch.undo()
    # pytest sets PYTEST_CURRENT_TEST only while a test runs, not while it imports test modules.
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert check_salt(SecretStr(TEST_SALT)).get_secret_value() == TEST_SALT
    monkeypatch.undo()
    # 8 different characters are enough, and a random hex salt never has fewer (a fixed list here).
    assert check_salt(SecretStr("abcdefgh" * 5))
    for number in range(500):
        check_salt(SecretStr(hashlib.sha256(str(number).encode()).hexdigest()[:32]))


def test__slot_seed__a_bad_salt_cannot_make_a_seed_and_the_salt_stays_secret() -> None:
    for salt, _ in REFUSED[:-1]:
        with pytest.raises(SaltError):
            SlotSeed(secret(salt), "game-guesser", "default", 1, "1", other_secrets=())  # type: ignore[arg-type]
    # A salt equal to another secret (SECRET_KEY, say) cannot make a seed, and the list is required.
    with pytest.raises(SaltError, match="must differ"):
        SlotSeed(
            SecretStr(SALT), "game-guesser", "default", 1, "1", other_secrets=[SecretStr(SALT)]
        )
    with pytest.raises(TypeError, match="other_secrets"):
        SlotSeed(SecretStr(SALT), "game-guesser", "default", 1, "1")  # type: ignore[call-arg]
    seed = seed_of(SALT)
    # `replace` runs the check again, so it needs the list again and a clash is refused.
    with pytest.raises(ValueError, match="other_secrets"):
        dataclasses.replace(seed, slot=2)  # type: ignore[arg-type]
    with pytest.raises(SaltError, match="must differ"):
        dataclasses.replace(seed, slot=2, other_secrets=[SecretStr(SALT)])
    assert dataclasses.replace(seed, slot=2, other_secrets=()).slot == 2
    shown = repr(seed) + str(seed) + repr(dataclasses.asdict(seed))
    shown += repr(ContentSettings(PUZZLE_SEED_SALT=SecretStr(SALT), _env_file=None))
    assert SALT not in shown
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(seed)


def test__slot_seed__one_salt_repeats_and_another_salt_chooses_otherwise() -> None:
    candidates = games(50)
    first = answers(SALT, candidates)
    assert answers(SALT, list(reversed(candidates))) == first
    assert answers(OTHER_SALT, candidates) != first
    assert answers(SALT, candidates, version="2") != first
    seed = seed_of(SALT, 7)
    numbers = {purpose: [seed.stream(purpose).random() for _ in range(2)] for purpose in Purpose}
    assert all(pair[0] == pair[1] for pair in numbers.values())
    assert len({pair[0] for pair in numbers.values()}) == len(Purpose)
    assert seed_of(OTHER_SALT, 7).stream(Purpose.CROP).random() != numbers[Purpose.CROP][0]
    # A colon in a field cannot move a field into its neighbour.
    ambiguous = [("a:b", "c"), ("a", "b:c"), ("a", "b"), ("a:b", "")]
    seeds = {
        SlotSeed(SecretStr(SALT), slug, variant, 1, "1", other_secrets=()).seed
        for slug, variant in ambiguous
    }
    assert len(seeds) == len(ambiguous)
    assert isinstance(seed.stream(Purpose.CLIP), random.Random)


GOLDEN_ANSWERS = [16, 39, 14, 33, 27, 9, 6, 32, 5, 8]
GOLDEN_INDEXES = [7, 8, 4, 4, 4, 7, 7, 7]
GOLDEN_DISTINCT = ["h", "i", "d", "c"]


def test__draws__give_the_same_numbers_on_every_python_version() -> None:
    """A golden test: the draws use only `random()`, so these ids hold on any Python. If this goes
    red, the choice logic changed: raise the builder's GENERATOR_VERSION."""
    candidates = games(50)
    chosen = [
        choose_answer(candidates, seed_of(TEST_SALT, slot).stream(Purpose.ANSWER)).id
        for slot in range(1, 11)
    ]
    assert chosen == GOLDEN_ANSWERS
    stream = seed_of(TEST_SALT, 1).stream(Purpose.SCREENSHOTS)
    assert [draw_index(stream, 10) for _ in range(8)] == GOLDEN_INDEXES
    stream = seed_of(TEST_SALT, 1).stream(Purpose.SCREENSHOTS)
    assert draw_distinct(stream, list("abcdefghij"), 4) == GOLDEN_DISTINCT
    with pytest.raises(ValueError, match="cannot draw"):
        draw_distinct(stream, [1, 2], 3)
