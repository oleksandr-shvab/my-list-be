"""Fill the local database with a playable game, with no real puzzle data (AC-82).

    ENVIRONMENT=local uv run python scripts/seed_dev.py

Safe to run again (daily, or after each PR): the dates are relative to today, the game's puzzles
are deleted and recreated, and the seed titles are updated to match the lists below. It refuses
to run unless the ENVIRONMENT variable is exactly `local` and DATABASE_URL points at this machine.

It creates the enabled game `game-guesser`, 12 titles with full names (some with franchises, and
"Doom" and "DOOM" as a pair of near-duplicates), scheduled puzzles for today and the 3 previous
days, one scheduled puzzle for tomorrow and one draft. Every puzzle has 6 clues. Step 1 has no
hint, and one puzzle also has no Metacritic hint (step 2), so null hints can be seen in play. Clue
media arrives with M1-18, so the puzzle view has no images yet.
"""

import asyncio
import datetime as dt
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_now, utc_today
from app.core.config import settings
from app.crud.title import create_title, find_title_by_name, update_title
from app.models.game import Game
from app.models.puzzle import MediaType, Puzzle, PuzzleClue, PuzzleStatus
from app.models.title import Title, TitleKind

GAME_SLUG = "game-guesser"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULT_VARIANT = "default"
LAUNCH_DAYS_AGO = 3
# The source credit shown when the game ends. A placeholder: no real source is committed.
SOURCE_URL = "https://example.com/seed/source"


@dataclass(frozen=True)
class SeedTitle:
    name: str
    franchise: str | None
    year: int


TITLES = [
    SeedTitle("The Legend of Zelda: Breath of the Wild", "The Legend of Zelda", 2017),
    SeedTitle("The Legend of Zelda: Tears of the Kingdom", "The Legend of Zelda", 2023),
    SeedTitle("Half-Life 2", "Half-Life", 2004),
    SeedTitle("Half-Life: Alyx", "Half-Life", 2020),
    SeedTitle("Mario", "Mario", 1983),
    SeedTitle("Super Mario Bros.", "Mario", 1985),
    SeedTitle("Portal 2", "Portal", 2011),
    SeedTitle("Hades", None, 2020),
    SeedTitle("Celeste", None, 2018),
    SeedTitle("Elden Ring", None, 2022),
    # A pair of near-duplicates: the same normalized name (Q9).
    SeedTitle("Doom", "Doom", 1993),
    SeedTitle("DOOM", "Doom", 2016),
]

# Steps 2 to 6 each carry one hint; step 1 (a pixelated crop) has none, so its hint is null.
HINT_KINDS = [None, "metacritic_score", "platform", "genre", "year", "developer"]


@dataclass(frozen=True)
class SeedPuzzle:
    days_from_today: int
    status: PuzzleStatus
    answer: str
    has_metacritic_hint: bool = True


PUZZLES = [
    SeedPuzzle(0, PuzzleStatus.SCHEDULED, "Half-Life 2"),
    SeedPuzzle(-1, PuzzleStatus.SCHEDULED, "The Legend of Zelda: Breath of the Wild"),
    # The one puzzle with no Metacritic score: its step 2 has a null hint.
    SeedPuzzle(-2, PuzzleStatus.SCHEDULED, "Portal 2", has_metacritic_hint=False),
    SeedPuzzle(-3, PuzzleStatus.SCHEDULED, "Hades"),
    SeedPuzzle(1, PuzzleStatus.SCHEDULED, "Celeste"),
    SeedPuzzle(2, PuzzleStatus.DRAFT, "Elden Ring"),
]


def _hint_value(kind: str | None, title: SeedTitle) -> str | None:
    values = {
        "metacritic_score": "90",
        "platform": "PC",
        "genre": "Action",
        "year": str(title.year),
        "developer": "Placeholder Studio",
    }
    return values[kind] if kind is not None else None


async def seed(db: AsyncSession, today: dt.date) -> None:
    """Create or reset the seed data. The caller commits."""
    game = (await db.execute(select(Game).where(Game.slug == GAME_SLUG))).scalar_one_or_none()
    if game is None:
        game = Game(slug=GAME_SLUG, title_kind=TitleKind.VIDEO_GAME)
        db.add(game)
    game.default_variant = DEFAULT_VARIANT
    game.launch_date = today - dt.timedelta(days=LAUNCH_DAYS_AGO)
    game.is_enabled = True
    await db.flush()

    # Clues go with their puzzle (ON DELETE CASCADE).
    await db.execute(delete(Puzzle).where(Puzzle.game_id == game.id))

    titles: dict[str, Title] = {}
    for seed_title in TITLES:
        existing = await find_title_by_name(db, name=seed_title.name, kind=TitleKind.VIDEO_GAME)
        if existing is None:
            titles[seed_title.name] = await create_title(
                db, name=seed_title.name, franchise=seed_title.franchise, year=seed_title.year
            )
        else:
            titles[seed_title.name] = await update_title(
                db,
                existing,
                name=seed_title.name,
                franchise=seed_title.franchise,
                year=seed_title.year,
            )

    seed_titles = {seed_title.name: seed_title for seed_title in TITLES}
    for seed_puzzle in PUZZLES:
        puzzle = Puzzle(
            game_id=game.id,
            variant=DEFAULT_VARIANT,
            date=today + dt.timedelta(days=seed_puzzle.days_from_today),
            status=seed_puzzle.status,
            answer_title_id=titles[seed_puzzle.answer].id,
        )
        db.add(puzzle)
        await db.flush()
        for step, kind in enumerate(HINT_KINDS, start=1):
            if kind == "metacritic_score" and not seed_puzzle.has_metacritic_hint:
                kind = None
            value = _hint_value(kind, seed_titles[seed_puzzle.answer])
            db.add(
                PuzzleClue(
                    puzzle_id=puzzle.id,
                    step=step,
                    media_type=MediaType.VIDEO if step == len(HINT_KINDS) else MediaType.IMAGE,
                    hint_kind=kind,
                    hint_value=value,
                    source_url=SOURCE_URL,
                )
            )
    await db.flush()


async def _run() -> None:
    from app.core.db import async_session_maker, engine

    today = utc_today(get_now())
    async with async_session_maker() as db:
        await seed(db, today)
        await db.commit()
    await engine.dispose()
    print(f"Seeded {GAME_SLUG}: {len(TITLES)} titles and {len(PUZZLES)} puzzles around {today}.")


def refusal_reason() -> str | None:
    """Why the seed must not run here, or None. It deletes data, so it checks the ENVIRONMENT
    variable itself: `settings.ENVIRONMENT` defaults to `local` when the variable is missing, which
    would make this check pass by accident."""
    environment = os.environ.get("ENVIRONMENT")
    if environment != "local":
        return f"the ENVIRONMENT variable is {environment!r}, not 'local'"
    host = urlsplit(settings.DATABASE_URL).hostname
    if host not in LOCAL_HOSTS:
        return f"DATABASE_URL points at host {host!r}, not this machine"
    return None


def main() -> int:
    reason = refusal_reason()
    if reason is not None:
        print(f"Refusing to seed: {reason}.")
        return 1
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
