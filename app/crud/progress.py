import datetime as dt
import uuid

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import set_local
from app.games.enums import ProgressStatus
from app.models.progress import Progress, ProgressGuess, ProgressOrigin

# The guesses with their titles, in position order: two queries, no lazy load.
_WITH_GUESSES = selectinload(Progress.guesses).joinedload(ProgressGuess.title)

# How long a request waits for the game's lock (or for another request's uncommitted first
# insert) before it gives up, so a stuck transaction cannot pile up requests. Postgres reports
# SQLSTATE 55P03 when it runs out (`is_lock_timeout` in `app/core/db.py`).
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


async def lock_progress(db: AsyncSession, *, user_id: uuid.UUID, puzzle_id: int) -> Progress | None:
    """`SELECT ... FOR UPDATE` on the account's game, or None when it has no row yet (AC-45). It
    starts the locked section: the transaction waits for the lock at most `LOCK_TIMEOUT_MS`
    (`lock_timeout`, for this transaction only), and the lock lasts until the caller's
    transaction ends. The guesses are read after the lock is held, so they include what the
    previous request committed."""
    await set_local(db, "lock_timeout", str(LOCK_TIMEOUT_MS))
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
