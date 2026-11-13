"""The seed and the salt of the puzzle builder (M1-22b, SPEC-04 AC-26 to AC-28, R-8).

No database and no network. Every choice below uses a fixed test salt."""

import random
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from app.content.candidates import choose_answer
from app.content.igdb import IgdbGame, parse_game
from app.content.seed import Purpose, SaltError, SlotSeed, check_salt, reserved_secrets
from app.content.settings import ContentSettings

SALT = "test-salt-" + "a" * 40
OTHER_SALT = "test-salt-" + "b" * 40
OTHER_SECRET = "some-other-secret-" + "c" * 30


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


def answers(salt: str, candidates: list[IgdbGame], *, version: str = "1") -> list[int]:
    """The answer of slots 1 to 12."""
    return [
        choose_answer(
            candidates,
            SlotSeed(salt, "game-guesser", "default", slot, version).stream(Purpose.ANSWER),
        ).id
        for slot in range(1, 13)
    ]


@pytest.mark.parametrize(
    ("salt", "others", "message"),
    [
        (None, [], "is not set"),
        (SecretStr(""), [], "is not set"),
        ("short", [], "too short"),
        ("change-me", [], "too short"),
        (SALT, [OTHER_SECRET, SALT], "must differ"),
        (SecretStr(SALT), [SecretStr(SALT)], "must differ"),
    ],
)
def test__check_salt__refuses_a_missing_weak_or_reused_salt(
    salt: Any, others: list[Any], message: str
) -> None:
    with pytest.raises(SaltError, match=message) as refusal:
        check_salt(salt, others)
    assert SALT not in str(refusal.value)
    assert check_salt(SALT, [OTHER_SECRET, None, SecretStr(OTHER_SECRET)]) == SALT


@pytest.mark.parametrize("name", ["SECRET_KEY", "MEDIA_SIGNING_KEY", "S3_SECRET_ACCESS_KEY"])
def test__reserved_secrets__every_secret_of_the_project_is_refused(name: str) -> None:
    secrets = {
        "SECRET_KEY": "app-secret-" + "1" * 30,
        "MEDIA_SIGNING_KEY": "media-key-" + "2" * 30,
        "S3_SECRET_ACCESS_KEY": "bucket-key-" + "3" * 30,
    }
    app_settings: Any = SimpleNamespace(**{key: SecretStr(value) for key, value in secrets.items()})
    others = reserved_secrets(app_settings, SecretStr("igdb-secret-" + "4" * 30))
    with pytest.raises(SaltError, match="must differ"):
        check_salt(secrets[name], others)
    with pytest.raises(SaltError, match="must differ"):
        check_salt("igdb-secret-" + "4" * 30, others)
    assert check_salt(SALT, others) == SALT


def test__slot_seed__one_salt_repeats_and_another_salt_chooses_otherwise() -> None:
    candidates = games(50)
    first = answers(SALT, candidates)
    assert answers(SALT, list(reversed(candidates))) == first
    assert answers(OTHER_SALT, candidates) != first
    assert answers(SALT, candidates, version="2") != first
    seed = SlotSeed(SALT, "game-guesser", "default", 7, "1")
    numbers = {purpose: [seed.stream(purpose).random() for _ in range(2)] for purpose in Purpose}
    assert all(pair[0] == pair[1] for pair in numbers.values())
    assert len({pair[0] for pair in numbers.values()}) == len(Purpose)
    other = SlotSeed(OTHER_SALT, "game-guesser", "default", 7, "1")
    assert other.stream(Purpose.CROP).random() != seed.stream(Purpose.CROP).random()
    assert isinstance(seed.stream(Purpose.CLIP), random.Random)


def test__salt__is_never_in_a_repr() -> None:
    seed = SlotSeed(SALT, "game-guesser", "default", 1, "1")
    settings = ContentSettings(PUZZLE_SEED_SALT=SecretStr(SALT), _env_file=None)
    assert SALT not in repr(seed) + str(seed) + repr(settings) + str(settings)
