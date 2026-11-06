"""Stats and streak of one account in one game variant (AC-46 to AC-54).

Pure functions on plain data: no database, no clock, no HTTP. The service loads the plays and
passes today's date in, so the account's stats and the guest merge test (AC-61) run the same code.
The rules are pinned by the S1 to S19 and T1 to T4 tables of SPEC-02 (`tests/test_stats_rules.py`),
which the frontend's guest stats must also match (Q8).
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from app.games.enums import ProgressStatus

# A gap of up to this many missed days keeps the run; a longer one starts a new run (AC-50, Q3).
MAX_MISSED_DAYS = 3


@dataclass(frozen=True)
class Play:
    """One stored game of the account, with only what the rules read: the puzzle's variant and date
    (the streak day, AC-49), its status, the guesses used (skips included, AC-30) and the flag
    stored with its first guess (AC-36)."""

    variant: str
    puzzle_date: dt.date
    status: str
    guesses_used: int
    counts_for_streak: bool


@dataclass(frozen=True)
class Stats:
    played: int
    wins: int
    # None when the variant has no streak (AC-51).
    current_streak: int | None
    max_streak: int | None
    # Wins by guesses used: every key from 1 to the game's `max_guesses`, zeros included.
    distribution: dict[int, int]
    # The won plays whose guess count is outside 1 to `max_guesses`, which have no bucket. They
    # are bad stored data. The caller logs them, since it knows which stored rows they are.
    unbucketed_wins: tuple[Play, ...]


def compute_stats(
    plays: Iterable[Play],
    *,
    today: dt.date,
    max_guesses: int,
    variant: str,
    default_variant: str,
) -> Stats:
    """The stats of `plays` in `variant`, which must hold no `is_test` row (AC-48). Every play must
    be of `variant`, finished or not, else `ValueError`: the caller loaded the wrong rows. The guest
    merge test (M1-15, AC-61) passes the plays of the variant it compares, with `variant` and
    `default_variant` both the game's default variant.

    Only finished plays count (AC-47), archive plays included. The streak fields are None unless
    `variant` is `default_variant` (AC-51).

    A won play with fewer than 1 or more than `max_guesses` guesses is bad stored data. It still
    counts in `played`, in `wins` and as a streak day, like any other win, and is left out of
    `distribution` only. It is returned in `unbucketed_wins`, so one bad row never breaks the
    account's stats."""
    plays = list(plays)
    if any(play.variant != variant for play in plays):
        raise ValueError(f"Every play must be of the variant {variant!r}")
    finished = [play for play in plays if play.status in (ProgressStatus.WON, ProgressStatus.LOST)]
    distribution = dict.fromkeys(range(1, max_guesses + 1), 0)
    wins = 0
    unbucketed_wins = []
    for play in finished:
        if play.status != ProgressStatus.WON:
            continue
        wins += 1
        if play.guesses_used in distribution:
            distribution[play.guesses_used] += 1
        else:
            unbucketed_wins.append(play)

    current_streak, max_streak = None, None
    if variant == default_variant:
        streak_days = {play.puzzle_date for play in finished if play.counts_for_streak}
        current_streak, max_streak = streaks(streak_days, today=today)
    return Stats(
        played=len(finished),
        wins=wins,
        current_streak=current_streak,
        max_streak=max_streak,
        distribution=distribution,
        unbucketed_wins=tuple(unbucketed_wins),
    )


def streaks(streak_days: Iterable[dt.date], *, today: dt.date) -> tuple[int, int]:
    """`(current_streak, max_streak)` of the days that count for the streak (AC-49, AC-50).

    Walk the days in order: a gap of 1 to 3 missed days keeps the run and adds only the day
    played, a gap of 4 or more starts a new run at 1. The current streak is the last run's length
    while the missed days after the last streak day, not counting today, are 3 or fewer, and 0
    otherwise: today is not missed until it is over."""
    run = 0
    longest = 0
    last_day: dt.date | None = None
    for day in sorted(set(streak_days)):
        is_continued = last_day is not None and (day - last_day).days - 1 <= MAX_MISSED_DAYS
        run = run + 1 if is_continued else 1
        longest = max(longest, run)
        last_day = day
    if last_day is None:
        return 0, 0
    missed_since_last = max((today - last_day).days - 1, 0)
    return (run if missed_since_last <= MAX_MISSED_DAYS else 0), longest
