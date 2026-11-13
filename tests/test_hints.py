"""The hints and the source link of one game (M1-22b, SPEC-04 AC-30 to AC-33, R-18, R-19)."""

import datetime as dt
from collections import Counter
from typing import Any

import pytest

from app.content.hints import Hint, HintKind, critic_score, hints, join_names, source_url
from app.content.igdb import IgdbGame, parse_game


def make_game(**fields: Any) -> IgdbGame:
    raw = {"id": 1, "name": "Example", "slug": "example", **fields}
    game = parse_game(raw, max_year=2027, skips=Counter(), drops=Counter())
    assert game is not None
    return game


def timestamp(year: int) -> int:
    return int(dt.datetime(year, 6, 1, tzinfo=dt.UTC).timestamp())


@pytest.mark.parametrize(
    ("rating", "count", "expected"),
    [
        (84.5, 4, "85"),  # half up: Python's round() gives 84
        (84.49, 9, "84"),
        (0.5, 4, "1"),
        (100.0, 4, "100"),
        (84.5, 3, None),  # under four critic scores
        (84.5, 0, None),
        (84.5, None, None),
        (None, 12, None),
    ],
)
def test__critic_score__rounds_half_up_and_needs_four_scores(
    rating: float | None, count: int | None, expected: str | None
) -> None:
    game = make_game(aggregated_rating=rating, aggregated_rating_count=count)
    assert critic_score(game) == expected


def test__hints__one_hint_per_step_and_none_when_the_data_is_missing() -> None:
    full = make_game(
        aggregated_rating=71.2,
        aggregated_rating_count=6,
        first_release_date=timestamp(2012),
        release_dates=[
            {"date": timestamp(2013), "platform": {"name": "Switch"}},
            {"date": timestamp(2012), "platform": {"name": "PC"}},
            {"date": timestamp(2012), "platform": {"name": "PlayStation 4"}},
            {"date": timestamp(2012), "platform": {"name": "Xbox One"}},
            {"date": timestamp(2012), "platform": {"name": "Wii U"}},
        ],
        genres=[{"name": "Puzzle"}, {"name": "Adventure"}],
        involved_companies=[
            {"developer": False, "company": {"name": "A Publisher"}},
            {"developer": True, "company": {"name": "A Studio"}},
        ],
    )
    assert hints(full) == {
        1: None,
        2: Hint(HintKind.CRITIC_SCORE, "71"),
        3: Hint(HintKind.PLATFORM, "PC, PlayStation 4, Xbox One"),  # three of four, earliest date
        4: Hint(HintKind.GENRE, "Puzzle, Adventure"),
        5: Hint(HintKind.YEAR, "2012"),
        6: Hint(HintKind.DEVELOPER, "A Studio"),
    }
    assert hints(make_game()) == dict.fromkeys(range(1, 7))
    assert "metacritic_score" not in {kind.value for kind in HintKind}


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["a", "b", "c", "d"], "a, b, c"),
        (["a", " a ", "", "b"], "a, b"),
        ([], None),
        (["x" * 100, "y" * 100, "z" * 100], f"{'x' * 100}, {'y' * 100}"),  # whole names only
        (["x" * 256], None),  # never cut mid-word
    ],
)
def test__join_names__three_at_most_and_whole_names(names: list[str], expected: str | None) -> None:
    assert join_names(names) == expected


def test__source_url__is_built_from_a_plain_slug_only() -> None:
    assert source_url(make_game(slug="half-life-2")) == "https://www.igdb.com/games/half-life-2"
    for slug in ("Half-Life", "half life", "half--life", "-half", "half\n", "half/../x", "é"):
        with pytest.raises(ValueError, match="slug"):
            source_url(make_game(slug=slug))
