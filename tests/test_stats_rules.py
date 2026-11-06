"""The stats and streak rules (AC-47 to AC-54): the S1 to S19 and T1 to T4 tables of SPEC-02,
over the pure functions in `app/games/stats.py`. No database. The frontend's guest stats must
match the same rows (Q8), so the ids are the spec's."""

import datetime as dt

import pytest

from app.games.enums import ProgressStatus
from app.games.stats import Play, Stats, compute_stats

MAX_GUESSES = 6
DEFAULT = "default"
DAY_ONE = dt.date(2026, 10, 1)
WON = ProgressStatus.WON
LOST = ProgressStatus.LOST
IN_PROGRESS = ProgressStatus.IN_PROGRESS


def day(number: int) -> dt.date:
    """The spec's UTC day number as a date."""
    return DAY_ONE + dt.timedelta(days=number - 1)


def play_on(
    puzzle_date: dt.date,
    status: str = WON,
    *,
    guesses: int = 3,
    counts_for_streak: bool = True,
    variant: str = DEFAULT,
) -> Play:
    return Play(
        variant=variant,
        puzzle_date=puzzle_date,
        status=status,
        guesses_used=guesses,
        counts_for_streak=counts_for_streak,
    )


def play(number: int, status: str = WON, **kwargs: int | bool | str) -> Play:
    return play_on(day(number), status, **kwargs)


def daily(*numbers: int) -> list[Play]:
    """Finished daily plays: the spec's "daily finishes" column."""
    return [play(number) for number in numbers]


def stats_of(plays: list[Play], today: dt.date, *, variant: str = DEFAULT) -> Stats:
    return compute_stats(
        plays,
        today=today,
        max_guesses=MAX_GUESSES,
        variant=variant,
        default_variant=DEFAULT,
    )


def streaks_of(plays: list[Play], today: int) -> tuple[int | None, int | None]:
    stats = stats_of(plays, day(today))
    return stats.current_streak, stats.max_streak


# id, plays, today, current, max.
STREAK_ROWS = [
    pytest.param([], 1, 0, 0, id="S1"),
    pytest.param(daily(1), 1, 1, 1, id="S2"),
    pytest.param(daily(1), 2, 1, 1, id="S3"),
    pytest.param(daily(1, 2, 3), 3, 3, 3, id="S4"),
    pytest.param(daily(1, 5), 5, 2, 2, id="S5"),
    pytest.param(daily(1, 6), 6, 1, 1, id="S6"),
    pytest.param(daily(1), 5, 1, 1, id="S7"),
    pytest.param(daily(1), 6, 0, 1, id="S8"),
    pytest.param(daily(1, 2, 4, 5), 5, 4, 4, id="S9"),
    pytest.param(daily(1, 3, 5, 7), 7, 4, 4, id="S10"),
    pytest.param(daily(1, 2, 3, 8, 9), 9, 2, 3, id="S11"),
    pytest.param(daily(1, 2, 3, 4, 5, 10), 12, 1, 5, id="S12"),
    pytest.param(daily(1, 2, 3), 7, 3, 3, id="S13"),
    pytest.param(daily(1, 2, 3), 8, 0, 3, id="S14"),
    # An archive finish of day 2, played on day 3: the flag is false, so it fills no day.
    pytest.param(
        [*daily(1, 3), play(2, counts_for_streak=False)],
        3,
        2,
        2,
        id="S15",
    ),
    # Day 2 was started as a daily and never finished: not activity.
    pytest.param([*daily(1, 3), play(2, IN_PROGRESS, guesses=2)], 3, 2, 2, id="S16"),
    # Losing counts.
    pytest.param([play(1, LOST, guesses=6), play(2), play(3, LOST, guesses=6)], 3, 3, 3, id="S17"),
    # Day 2 began on day 3 (the stored flag is false), then finished: archive.
    pytest.param([play(1), play(2, counts_for_streak=False)], 3, 1, 1, id="S18"),
    # Day 2 began at 23:59 on day 2 and finished on day 3: the stored flag is true, and the
    # streak day is the puzzle's date, not the finish date.
    pytest.param(daily(1, 2), 3, 2, 2, id="S19"),
    # Not a spec row: the same day twice is one streak day, not a run of 2.
    pytest.param(daily(1, 1), 1, 1, 1, id="same_day_twice"),
]


@pytest.mark.parametrize(("plays", "today", "current", "longest"), STREAK_ROWS)
def test__streaks__match_the_spec_table(
    plays: list[Play], today: int, current: int, longest: int
) -> None:
    assert streaks_of(plays, today) == (current, longest)


def date_plays(*dates: str) -> list[Play]:
    return [play_on(dt.date.fromisoformat(text)) for text in dates]


# Real calendar days, across a year end and across the end of February in a year that is not a
# leap year (2027). `today` is the last day.
CALENDAR_ROWS = [
    pytest.param(["2026-12-31", "2027-01-01"], 2, 2, id="year_end_continued"),
    pytest.param(["2026-12-30", "2027-01-03"], 2, 2, id="year_end_three_missed_continued"),
    pytest.param(["2026-12-26", "2026-12-27", "2027-01-01"], 1, 2, id="year_end_four_missed_reset"),
    pytest.param(["2027-02-28", "2027-03-01"], 2, 2, id="february_end_continued"),
    pytest.param(["2027-02-25", "2027-03-01"], 2, 2, id="february_end_three_missed_continued"),
    pytest.param(
        ["2027-02-23", "2027-02-24", "2027-03-01"], 1, 2, id="february_end_four_missed_reset"
    ),
]


@pytest.mark.parametrize(("dates", "current", "longest"), CALENDAR_ROWS)
def test__streaks__run_across_a_month_or_year_end(
    dates: list[str], current: int, longest: int
) -> None:
    stats = stats_of(date_plays(*dates), dt.date.fromisoformat(dates[-1]))

    assert (stats.current_streak, stats.max_streak) == (current, longest)


def test__streaks__ignore_the_order_of_the_plays() -> None:
    assert streaks_of(list(reversed(daily(1, 2, 3, 8, 9))), 9) == (2, 3)


def test__streaks__are_none_for_a_variant_with_no_streak() -> None:
    plays = [play(number, variant="hard") for number in (1, 2)]

    stats = stats_of(plays, day(2), variant="hard")

    assert (stats.current_streak, stats.max_streak) == (None, None)
    assert (stats.played, stats.wins) == (2, 2)  # AC-51: only the streak fields are empty.


# id, plays, played, wins, distribution of the wins that are not zero.
COUNTER_ROWS = [
    pytest.param(
        [
            play(1, WON, guesses=1),
            play(2, WON, guesses=3),
            play(3, LOST, guesses=6),
            play(4, WON, guesses=6, counts_for_streak=False),
            play(5, IN_PROGRESS, guesses=2),
        ],
        4,
        3,
        {1: 1, 3: 1, 6: 1},
        id="T1",
    ),
    pytest.param(
        [
            play(1, LOST, guesses=6, counts_for_streak=False),
            play(2, LOST, guesses=6, counts_for_streak=False),
        ],
        2,
        0,
        {},
        id="T2",
    ),
    pytest.param([], 0, 0, {}, id="T3"),
    # Day 2 is a bad stored row (7 guesses): a win with no bucket.
    pytest.param([play(1, WON, guesses=2), play(2, WON, guesses=7)], 2, 2, {2: 1}, id="T4"),
]


@pytest.mark.parametrize(("plays", "played", "wins", "wins_by_guesses"), COUNTER_ROWS)
def test__counters__match_the_spec_table(
    plays: list[Play], played: int, wins: int, wins_by_guesses: dict[int, int]
) -> None:
    stats = stats_of(plays, day(10))

    assert (stats.played, stats.wins) == (played, wins)
    # Keys 1 to 6 are always there, zeros included (AC-46).
    assert stats.distribution == {
        guesses: wins_by_guesses.get(guesses, 0) for guesses in range(1, MAX_GUESSES + 1)
    }


@pytest.mark.parametrize("guesses", [0, MAX_GUESSES + 1], ids=["zero_guesses", "seven_guesses"])
def test__counters__count_a_win_with_no_valid_guess_count_but_give_it_no_bucket(
    guesses: int,
) -> None:
    bad = play(2, WON, guesses=guesses)

    stats = stats_of([play(1, WON, guesses=2), bad, play(3, LOST, guesses=6)], day(3))

    # The bad win is a win like any other, and a streak day: days 1, 2 and 3 are a run of 3 (it
    # would be 2 if the bad day did not count). It is left out of the distribution only.
    assert (stats.played, stats.wins) == (3, 2)
    assert (stats.current_streak, stats.max_streak) == (3, 3)
    assert stats.distribution == {1: 0, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0}
    assert stats.unbucketed_wins == (bad,)


def test__counters__bad_win_follows_the_flag_and_status_rules_of_any_win() -> None:
    # An archive win (flag false) fills no streak day, bad or not; a bad count on a loss is not a
    # win and is not reported.
    plays = [
        play(1, WON, guesses=0, counts_for_streak=False),
        play(2, LOST, guesses=0),
        play(3, IN_PROGRESS, guesses=9),
    ]

    stats = stats_of(plays, day(3))

    assert (stats.played, stats.wins) == (2, 1)
    assert (stats.current_streak, stats.max_streak) == (1, 1)
    assert stats.unbucketed_wins == (plays[0],)


VARIANT_MIX_ROWS = [
    pytest.param([play(1), play(2, variant="hard")], id="finished_play_of_another_variant"),
    # The caller loaded the wrong rows even if the odd one is not finished.
    pytest.param(
        [play(1), play(2, IN_PROGRESS, variant="hard")], id="unfinished_play_of_another_variant"
    ),
]


@pytest.mark.parametrize("plays", VARIANT_MIX_ROWS)
def test__compute_stats__refuses_plays_of_another_variant(plays: list[Play]) -> None:
    with pytest.raises(ValueError, match="variant"):
        stats_of(plays, day(2))
