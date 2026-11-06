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

_WITH_GUESSES = selectinload(Progress.guesses).joinedload(ProgressGuess.title)

# How long a request waits for the game's lock (or another request's uncommitted first insert),
# so a stuck transaction cannot pile up requests. Postgres raises 55P03 (`is_lock_failure`).
LOCK_TIMEOUT_MS = 5000


def _progress_query(user_id: uuid.UUID, puzzle_id: int) -> Select[tuple[Progress]]:
    return (
        select(Progress)
        .where(Progress.user_id == user_id, Progress.puzzle_id == puzzle_id)
        .options(_WITH_GUESSES)
        # The session may hold an older copy of the row from an earlier read.
        .execution_options(populate_existing=True)
    )


async def get_progress(db: AsyncSession, *, user_id: uuid.UUID, puzzle_id: int) -> Progress | None:
    """A plain read: no lock, and it never creates a row (views and resumes must not)."""
    result = await db.execute(_progress_query(user_id, puzzle_id))
    return result.scalar_one_or_none()


async def limit_lock_wait(db: AsyncSession) -> None:
    await set_local(db, "lock_timeout", str(LOCK_TIMEOUT_MS))


async def lock_progress(db: AsyncSession, *, user_id: uuid.UUID, puzzle_id: int) -> Progress | None:
    """The lock lasts until the transaction ends (AC-45). The caller sets `limit_lock_wait` first
    (`_locked_unit` does). The guesses are read under the lock, so they include what the previous
    request committed."""
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
    """Of two first guesses at once, one inserts and the other waits and locks the same row.

    `counts_for_streak` and `started_at` are written only here, from the `now` taken at the start
    of the request, so a request that waited on a lock keeps its start time (AC-36).

    A raw `insert` skips the ORM on purpose: `ON CONFLICT` is the only race-free way to create the
    row (SKILL.md, "Bulk operations").
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
    """`finished_at` is never before `started_at`: a request's `now` is taken at its start, and a
    server clock can step back."""
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
    """Returns False, changing nothing, when the account already has a game on the puzzle (AC-58):
    a repeat (AC-61), another entry of the same request and a game begun meanwhile all leave the
    stored row as it is.

    The streak uses the puzzle's date, not `now` (AC-49, R-23).

    A raw `insert` skips the ORM on purpose: `ON CONFLICT` is the only race-free way to tell
    "stored" from "already there" (SKILL.md, "Bulk operations").
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
    progress_id: int
    variant: str
    puzzle_date: dt.date
    status: str
    guesses_used: int
    counts_for_streak: bool


async def list_finished_plays(
    db: AsyncSession, *, user_id: uuid.UUID, game_id: int, variant: str, today: dt.date
) -> list[PlayRow]:
    """Games on a puzzle that is no longer visible are left out, as in the archive (AC-12), and so
    are `is_test` rows (AC-48) and games in progress."""
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
