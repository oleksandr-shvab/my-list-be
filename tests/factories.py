"""Plain async helpers that create game data through the session. No factory library.
They flush, never commit: the `db_session` fixture rolls everything back after the test."""

import datetime as dt
import hashlib
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.title import create_title
from app.games.enums import ClueContentType, ClueRole, MediaType
from app.games.modules.base import ClueData, MediaFileData, SignedLink
from app.media.storage import LocalMediaStorage
from app.models.game import Game
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue, PuzzleStatus
from app.models.title import Title, TitleKind

STEP_COUNT = 6
IMAGE_SIZE = (640, 360)
CLIP_DURATION_MS = 12_000


def media_key(puzzle_id: int, step: int, name: str) -> str:
    """A test media key: a SHA-256 hex digest like the real ones (AC-74). `name` is `source`,
    `webm` or `poster`."""
    return hashlib.sha256(f"{puzzle_id}/{step}/{name}".encode()).hexdigest()


def make_clue(
    step: int,
    *,
    hint_kind: str | None = None,
    hint_value: str | None = None,
    source_url: str | None = None,
) -> ClueData:
    """Clue data with the media of a valid clue: images for steps 1 to 5, an MP4 and poster for
    step 6. The keys point at no stored file."""
    is_video = step == STEP_COUNT
    width, height = IMAGE_SIZE
    source_type = ClueContentType.VIDEO_MP4 if is_video else ClueContentType.IMAGE_WEBP
    files = [
        MediaFileData(
            role=ClueRole.SOURCE,
            key=media_key(0, step, "source"),
            content_type=source_type,
            width=width,
            height=height,
        )
    ]
    if is_video:
        files.append(
            MediaFileData(
                role=ClueRole.POSTER,
                key=media_key(0, step, "poster"),
                content_type=ClueContentType.IMAGE_WEBP,
                width=width,
                height=height,
            )
        )
    return ClueData(
        step=step,
        hint_kind=hint_kind,
        hint_value=hint_value,
        source_url=source_url,
        media_type=MediaType.VIDEO if is_video else MediaType.IMAGE,
        media_files=tuple(files),
    )


FAKE_EXPIRY = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)


def fake_sign(key: str, content_type: str) -> SignedLink:
    """A signer for module tests: the link names the key and content type, so a test can see which
    files were signed."""
    return SignedLink(url=f"https://media.test/{key}?type={content_type}", expires_at=FAKE_EXPIRY)


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
    with_webm: bool = False,
    with_media: bool = True,
) -> Puzzle:
    """A puzzle with its six clues: steps 1 to 5 are images, step 6 is a video (an MP4, a WebM when
    `with_webm`, and a poster). The hint of step 3 is null, as for a title with no Metacritic
    score. Every clue has a source link and, unless `with_media` is false, its media files."""
    puzzle = Puzzle(
        game_id=game.id,
        variant=variant if variant is not None else game.default_variant,
        date=puzzle_date,
        status=status,
        answer_title_id=answer.id,
    )
    db.add(puzzle)
    await db.flush()
    clues = []
    for step in range(1, STEP_COUNT + 1):
        has_hint = step != 3
        clues.append(
            PuzzleClue(
                puzzle_id=puzzle.id,
                step=step,
                media_type=MediaType.VIDEO if step == STEP_COUNT else MediaType.IMAGE,
                hint_kind="year" if has_hint else None,
                hint_value=str(2000 + step) if has_hint else None,
                source_url=f"https://example.com/source/{step}",
            )
        )
    db.add_all(clues)
    await db.flush()
    for clue in clues if with_media else []:
        files = [(ClueRole.SOURCE, "source", ClueContentType.IMAGE_WEBP)]
        if clue.media_type == MediaType.VIDEO:
            files = [(ClueRole.SOURCE, "source", ClueContentType.VIDEO_MP4)]
            if with_webm:
                files.append((ClueRole.SOURCE, "webm", ClueContentType.VIDEO_WEBM))
            files.append((ClueRole.POSTER, "poster", ClueContentType.IMAGE_WEBP))
        for role, name, content_type in files:
            db.add(
                ClueMediaFile(
                    clue_id=clue.id,
                    role=role,
                    key=media_key(puzzle.id, clue.step, name),
                    content_type=content_type,
                    byte_size=1000,
                    width=IMAGE_SIZE[0],
                    height=IMAGE_SIZE[1],
                    duration_ms=CLIP_DURATION_MS if content_type.startswith("video/") else None,
                )
            )
    await db.flush()
    return puzzle


TEST_SIGNING_KEY = b"test-signing-key-of-32-bytes-or-more"
TEST_MEDIA_BASE_URL = "https://media.test/api"
IMAGE_TTL_SECONDS = 600
VIDEO_TTL_SECONDS = 3600


def make_local_storage(directory: Path | None = None) -> LocalMediaStorage:
    """A local storage with a fixed key and the default lifetimes. The directory matters only to
    tests that serve files."""
    return LocalMediaStorage(
        directory=directory or Path("/nonexistent-media-dir"),
        base_url=TEST_MEDIA_BASE_URL,
        signing_key=TEST_SIGNING_KEY,
        image_ttl_seconds=IMAGE_TTL_SECONDS,
        video_ttl_seconds=VIDEO_TTL_SECONDS,
    )
