import datetime as dt
import uuid
from collections.abc import Collection
from typing import NamedTuple

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import set_local
from app.crud.puzzle import visible_puzzle_condition
from app.games.enums import ProgressStatus
from app.models.progress import Progress, ProgressGuess, ProgressOrigin
from app.models.puzzle import Puzzle

# The guesses with their titles, in position order: two queries, no lazy load.
_WITH_GUESSES = selectinload(Progress.guesses).joinedload(ProgressGuess.title)

# How long a request waits for the game's lock (or for another request's uncommitted first
# insert) before it gives up, so a stuck transaction cannot pile up requests. Postgres reports
# SQLSTATE 55P03 when it runs out (`is_lock_failure` in `app/core/db.py`).
LOCK_TIMEOUT_MS = 5000


def _progress_query(user_id: uuid.UUID, puzzle_id: int) -> Select[tuple[Progress]]:
    """The account's game on this puzzle with its guesses. The one query behind the plain read
    and the locked read."""
    return (
        select(Progress)
        .where(Progress.user_id == user_id, Progress.puzzle_id == puzzle_id)
        .options(_WITH_GUESSES)
        # The session may hold an older copy of the row from an earlier read.
        .execution_options(populate_existing=True)
    )


async def get_progress(db: AsyncSession, *, user_id: uuid.UUID, puzzle_id: int) -> Progress | None:
    """The account's stored game on this puzzle with its guesses, or None. A plain read: it takes
    no lock and never creates a row (views and resumes must not)."""
    result = await db.execute(_progress_query(user_id, puzzle_id))
    return result.scalar_one_or_none()


async def limit_lock_wait(db: AsyncSession) -> None:
    """Limit, for this transaction only, how long a statement waits for a lock held by another
    transaction to `LOCK_TIMEOUT_MS`. Postgres then raises SQLSTATE 55P03 (`is_lock_failure`)."""
    await set_local(db, "lock_timeout", str(LOCK_TIMEOUT_MS))


async def lock_progress(db: AsyncSession, *, user_id: uuid.UUID, puzzle_id: int) -> Progress | None:
    """`SELECT ... FOR UPDATE` on the account's game, or None when it has no row yet (AC-45). It
    starts the locked section, and the lock lasts until the caller's transaction ends. The caller
    limits the wait first (`limit_lock_wait`, which `_locked_unit` in the service does once per
    unit). The guesses are read after the lock is held, so they include what the previous request
    committed."""
    result = await db.execute(_progress_query(user_id, puzzle_id).with_for_update(of=Progress))
    return result.scalar_one_or_none()


async def create_progress(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    puzzle_id: int,
    now: dt.datetime,
    counts_for_streak: bool,
) -> Progress:
    """Create the account's game after `lock_progress` found no row, and lock it. The insert is
    `INSERT ... ON CONFLICT DO NOTHING`: of two first guesses at once, one inserts and the other
    waits for it, inserts nothing and locks the same row.

    `counts_for_streak` and `started_at` are written only here, from the `now` the caller took at
    the start of the request, so a request that waited on a lock keeps its start time (the
    midnight edge of AC-36) and no later request can change them.

    A raw `insert` skips ORM instrumentation on purpose: `ON CONFLICT` is the only race-free way
    to create the row (SKILL.md, "Bulk operations").
    """
    await db.execute(
        insert(Progress)
        .values(
            user_id=user_id,
            puzzle_id=puzzle_id,
            status=ProgressStatus.IN_PROGRESS,
            started_at=now,
            counts_for_streak=counts_for_streak,
            origin=ProgressOrigin.PLAY,
        )
        .on_conflict_do_nothing(index_elements=[Progress.user_id, Progress.puzzle_id])
    )
    result = await db.execute(_progress_query(user_id, puzzle_id).with_for_update(of=Progress))
    return result.scalar_one()


async def append_guess(
    db: AsyncSession,
    progress: Progress,
    *,
    title_id: int | None,
    status: ProgressStatus,
    now: dt.datetime,
) -> None:
    """Store the next guess (`None` is a skip) of a locked progress row, and its new status.
    `finished_at` is set when the game ends, and never before `started_at` (a request's `now` is
    taken at its start, and a server clock can step back). The caller commits."""
    db.add(
        ProgressGuess(
            progress_id=progress.id, position=len(progress.guesses) + 1, title_id=title_id
        )
    )
    progress.status = status
    if status != ProgressStatus.IN_PROGRESS:
        progress.finished_at = max(now, progress.started_at)
    await db.flush()


async def list_owned_puzzle_ids(
    db: AsyncSession, *, user_id: uuid.UUID, puzzle_ids: Collection[int]
) -> set[int]:
    """The ids among `puzzle_ids` that the account already has a game on, in one query. A plain
    read: no lock, no row created."""
    if not puzzle_ids:
        return set()
    result = await db.execute(
        select(Progress.puzzle_id).where(
            Progress.user_id == user_id, Progress.puzzle_id.in_(puzzle_ids)
        )
    )
    return set(result.scalars())


async def insert_merged_progress(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    puzzle_id: int,
    status: ProgressStatus,
    now: dt.datetime,
    counts_for_streak: bool,
    title_ids: list[int | None],
) -> bool:
    """Store a guest's game for the account, with `origin` merge (AC-57). Returns False, and
    changes nothing, when the account already has a game on the puzzle (AC-58): the insert is
    `INSERT ... ON CONFLICT (user_id, puzzle_id) DO NOTHING`, so a repeat (AC-61), another entry of
    the same request and a game begun in the meantime all leave the stored row as it is.

    `started_at` is `now`, and so is `finished_at` of a finished game (R-23); the streak uses the
    puzzle's date, not these times (AC-49). The caller commits.

    A raw `insert` skips ORM instrumentation on purpose: `ON CONFLICT` is the only race-free way
    to tell "stored" from "already there" (SKILL.md, "Bulk operations").
    """
    progress_id = await db.scalar(
        insert(Progress)
        .values(
            user_id=user_id,
            puzzle_id=puzzle_id,
            status=status,
            started_at=now,
            finished_at=None if status == ProgressStatus.IN_PROGRESS else now,
            counts_for_streak=counts_for_streak,
            origin=ProgressOrigin.MERGE,
        )
        .on_conflict_do_nothing(index_elements=[Progress.user_id, Progress.puzzle_id])
        .returning(Progress.id)
    )
    if progress_id is None:
        return False
    await db.execute(
        insert(ProgressGuess),
        [
            {"progress_id": progress_id, "position": position, "title_id": title_id}
            for position, title_id in enumerate(title_ids, start=1)
        ],
    )
    return True


class PlayRow(NamedTuple):
    """One finished game for the stats: the plain data of a progress row and its puzzle."""

    progress_id: int
    variant: str
    puzzle_date: dt.date
    status: str
    guesses_used: int
    counts_for_streak: bool


async def list_finished_plays(
    db: AsyncSession, *, user_id: uuid.UUID, game_id: int, variant: str, today: dt.date
) -> list[PlayRow]:
    """The account's finished (won or lost) games of one game and variant on visible puzzles, for
    the stats (AC-46). A game of a puzzle that is no longer visible (unscheduled, or before a moved
    launch date) is left out, as in the archive (AC-12). Rows with `is_test` are left out
    (AC-48), and so are games in progress: they count for nothing, so their guesses are never
    counted. A plain read in one query; the rule that turns it into numbers is in
    `app/games/stats.py`."""
    guesses_used = (
        select(func.count(ProgressGuess.id))
        .where(ProgressGuess.progress_id == Progress.id)
        .correlate(Progress)
        .scalar_subquery()
    )
    result = await db.execute(
        # Every column is labelled with its `PlayRow` field, and the row is built by name.
        select(
            Progress.id.label("progress_id"),
            Puzzle.variant.label("variant"),
            Puzzle.date.label("puzzle_date"),
            Progress.status.label("status"),
            guesses_used.label("guesses_used"),
            Progress.counts_for_streak.label("counts_for_streak"),
        )
        .join(Puzzle, Puzzle.id == Progress.puzzle_id)
        .where(
            Progress.user_id == user_id,
            Progress.is_test.is_(False),
            Progress.status.in_([ProgressStatus.WON, ProgressStatus.LOST]),
            Puzzle.game_id == game_id,
            Puzzle.variant == variant,
            visible_puzzle_condition(today),
        )
    )
    return [PlayRow(**row._mapping) for row in result.all()]
