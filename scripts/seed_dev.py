"""Fill the local database with a playable game, with no real puzzle data (AC-82).

    ENVIRONMENT=local uv run python scripts/seed_dev.py

Safe to run again: the dates are relative to today, the game's puzzles are deleted and recreated,
and the seed titles are updated to match the lists below. It refuses to run unless ENVIRONMENT is
exactly `local` and DATABASE_URL points at this machine.

It creates the enabled game `game-guesser`, 12 titles ("Doom" and "DOOM" are near-duplicates) and
puzzles with 6 clues each: scheduled for today, the 3 previous days and tomorrow, plus one draft.

The clue media are the placeholder files in `scripts/seed_media/` (see its README). The script
copies them into MEDIA_LOCAL_DIR under their SHA-256 (the media key, AC-74), so it refuses to run
when MEDIA_STORAGE is not `local`.
"""

import asyncio
import datetime as dt
import hashlib
import json
import os
import shutil
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
from app.games.enums import ClueRole, MediaType
from app.models.game import Game
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue, PuzzleStatus
from app.models.title import Title, TitleKind

GAME_SLUG = "game-guesser"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULT_VARIANT = "default"
LAUNCH_DAYS_AGO = 3
# The source credit shown when the game ends. A placeholder: no real source is committed.
SOURCE_URL = "https://example.com/seed/source"

SEED_MEDIA_DIR = ROOT / "scripts" / "seed_media"
STEP_IMAGES = ["step-1.webp", "step-2.webp", "step-3.webp", "step-4.webp", "step-5.webp"]
CLIP = "clip.mp4"
POSTER = "poster.webp"


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
    # None for a puzzle that is not scheduled: it has no date (the content tools' rule).
    days_from_today: int | None
    status: PuzzleStatus
    answer: str
    has_metacritic_hint: bool = True


PUZZLES = [
    SeedPuzzle(0, PuzzleStatus.SCHEDULED, "Half-Life 2"),
    SeedPuzzle(-1, PuzzleStatus.SCHEDULED, "The Legend of Zelda: Breath of the Wild"),
    SeedPuzzle(-2, PuzzleStatus.SCHEDULED, "Portal 2", has_metacritic_hint=False),
    SeedPuzzle(-3, PuzzleStatus.SCHEDULED, "Hades"),
    SeedPuzzle(1, PuzzleStatus.SCHEDULED, "Celeste"),
    SeedPuzzle(None, PuzzleStatus.DRAFT, "Elden Ring"),
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


@dataclass(frozen=True)
class StoredMedia:
    key: str
    content_type: str
    byte_size: int
    width: int
    height: int
    duration_ms: int | None


def store_media(media_dir: Path) -> dict[str, StoredMedia]:
    """Copy the seed files into `media_dir` named by their SHA-256 (the media key); return them by
    file name."""
    manifest = json.loads((SEED_MEDIA_DIR / "manifest.json").read_text())
    media_dir.mkdir(parents=True, exist_ok=True)
    stored: dict[str, StoredMedia] = {}
    for name, spec in manifest.items():
        source = SEED_MEDIA_DIR / name
        key = hashlib.sha256(source.read_bytes()).hexdigest()
        shutil.copyfile(source, media_dir / key)
        stored[name] = StoredMedia(
            key=key,
            content_type=spec["content_type"],
            byte_size=source.stat().st_size,
            width=spec["width"],
            height=spec["height"],
            duration_ms=spec.get("duration_ms"),
        )
    return stored


def _media_row(clue_id: int, role: ClueRole, file: StoredMedia) -> ClueMediaFile:
    return ClueMediaFile(
        clue_id=clue_id,
        role=role,
        key=file.key,
        content_type=file.content_type,
        byte_size=file.byte_size,
        width=file.width,
        height=file.height,
        duration_ms=file.duration_ms,
    )


async def seed(db: AsyncSession, today: dt.date, *, media_dir: Path) -> None:
    """Create or reset the seed data and copy the media files. The caller commits."""
    media = store_media(media_dir)
    game = (await db.execute(select(Game).where(Game.slug == GAME_SLUG))).scalar_one_or_none()
    if game is None:
        game = Game(slug=GAME_SLUG, title_kind=TitleKind.VIDEO_GAME)
        db.add(game)
    game.default_variant = DEFAULT_VARIANT
    game.launch_date = today - dt.timedelta(days=LAUNCH_DAYS_AGO)
    game.is_enabled = True
    await db.flush()

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
            date=None
            if seed_puzzle.days_from_today is None
            else today + dt.timedelta(days=seed_puzzle.days_from_today),
            status=seed_puzzle.status,
            answer_title_id=titles[seed_puzzle.answer].id,
        )
        db.add(puzzle)
        await db.flush()
        clues: list[PuzzleClue] = []
        for step, kind in enumerate(HINT_KINDS, start=1):
            if kind == "metacritic_score" and not seed_puzzle.has_metacritic_hint:
                kind = None
            value = _hint_value(kind, seed_titles[seed_puzzle.answer])
            clues.append(
                PuzzleClue(
                    puzzle_id=puzzle.id,
                    step=step,
                    media_type=MediaType.VIDEO if step == len(HINT_KINDS) else MediaType.IMAGE,
                    hint_kind=kind,
                    hint_value=value,
                    source_url=SOURCE_URL,
                )
            )
        db.add_all(clues)
        await db.flush()
        for clue in clues:
            if clue.media_type == MediaType.VIDEO:
                db.add(_media_row(clue.id, ClueRole.SOURCE, media[CLIP]))
                db.add(_media_row(clue.id, ClueRole.POSTER, media[POSTER]))
            else:
                db.add(_media_row(clue.id, ClueRole.SOURCE, media[STEP_IMAGES[clue.step - 1]]))
    await db.flush()


async def _run() -> None:
    from app.core.db import async_session_maker, engine

    today = utc_today(get_now())
    async with async_session_maker() as db:
        await seed(db, today, media_dir=settings.media_local_path)
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
    if settings.MEDIA_STORAGE != "local":
        return f"MEDIA_STORAGE is {settings.MEDIA_STORAGE!r}, and the seed writes to a local folder"
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
