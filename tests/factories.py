"""Plain async helpers that create game data through the session. No factory library.

They flush, never commit: the `db_session` fixture rolls everything back after the test."""

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.title import create_title
from app.models.game import Game
from app.models.puzzle import MediaType, Puzzle, PuzzleClue, PuzzleStatus
from app.models.title import Title, TitleKind

STEP_COUNT = 6


async def create_game(
    db: AsyncSession,
    *,
    slug: str = "game-guesser",
    default_variant: str = "default",
    launch_date: dt.date = dt.date(2026, 1, 1),
    is_enabled: bool = True,
) -> Game:
    game = Game(
        slug=slug,
        default_variant=default_variant,
        title_kind=TitleKind.VIDEO_GAME,
        launch_date=launch_date,
        is_enabled=is_enabled,
    )
    db.add(game)
    await db.flush()
    return game


async def create_titles(db: AsyncSession, *names: str) -> list[Title]:
    return [await create_title(db, name=name) for name in names]


async def create_puzzle(
    db: AsyncSession,
    *,
    game: Game,
    answer: Title,
    puzzle_date: dt.date,
    status: PuzzleStatus = PuzzleStatus.SCHEDULED,
    variant: str | None = None,
) -> Puzzle:
    """A puzzle with its six clues: steps 1 to 5 are images, step 6 is a video. The hint of
    step 3 is null, as for a title with no Metacritic score. Every clue has a source link."""
    puzzle = Puzzle(
        game_id=game.id,
        variant=variant if variant is not None else game.default_variant,
        date=puzzle_date,
        status=status,
        answer_title_id=answer.id,
    )
    db.add(puzzle)
    await db.flush()
    for step in range(1, STEP_COUNT + 1):
        has_hint = step != 3
        db.add(
            PuzzleClue(
                puzzle_id=puzzle.id,
                step=step,
                media_type=MediaType.VIDEO if step == STEP_COUNT else MediaType.IMAGE,
                hint_kind="year" if has_hint else None,
                hint_value=str(2000 + step) if has_hint else None,
                source_url=f"https://example.com/source/{step}",
            )
        )
    await db.flush()
    return puzzle
