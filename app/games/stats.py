"""Pure functions on plain data, so the account's stats and the guest merge test (AC-61) run the
same code. The rules are the S1 to S19 and T1 to T4 tables of SPEC-02, which the frontend's guest
stats must also match (Q8).
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from app.games.enums import ProgressStatus

# A gap of up to this many missed days keeps the run; a longer one starts a new run (AC-50, Q3).
MAX_MISSED_DAYS = 3


@dataclass(frozen=True)
class Play:
    """`guesses_used` counts skips (AC-30); `puzzle_date` is the streak day (AC-49)."""

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
    distribution: dict[int, int]
    # Wins with a guess count outside 1 to `max_guesses` (bad stored data). The caller logs them,
    # since it knows which rows they are.
    unbucketed_wins: tuple[Play, ...]


def compute_stats(
    plays: Iterable[Play],
    *,
    today: dt.date,
    max_guesses: int,
    variant: str,
    default_variant: str,
) -> Stats:
    """`plays` must hold no `is_test` row (AC-48) and all be of `variant`, finished or not, else
    `ValueError`. Only finished plays count (AC-47). The streak fields are None unless `variant` is
    `default_variant` (AC-51).

    A win with fewer than 1 or more than `max_guesses` guesses is bad stored data: it counts
    everywhere except `distribution` and is returned in `unbucketed_wins`, so one bad row never
    breaks the stats."""
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
    """A gap of 1 to 3 missed days keeps the run, 4 or more starts a new one. The current streak is
    0 once more than 3 days were missed after the last streak day; today is not missed until it is
    over (AC-49, AC-50)."""
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
