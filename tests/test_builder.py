"""The puzzle builder (M1-22d, SPEC-04 AC-2, AC-29, AC-32, AC-33, AC-41 to AC-46, AC-50, AC-52,
AC-85, AC-86, decision 71).

No network: IGDB is a fake list of games, screenshots are small generated JPEGs, storage and the
working folder are in `tmp_path`, the clip is shortened to the smallest the limits allow."""

import datetime as dt
import hashlib
import importlib.util
import random
import subprocess
import sys
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import partial
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.content import builder as builder_module
from app.content import media
from app.content.builder import (
    GENERATOR_VERSION,
    MAX_SLOT_NUMBER,
    Builder,
    BuildReport,
    DatabaseFailure,
    GiveUpError,
    ImageFetcher,
    ImageHostUnavailableError,
    ImageUnavailableError,
    SlotLimitError,
    SlotOutcome,
    SlotRequest,
    SlotStatus,
    database_session,
    give_up_slot,
)
from app.content.candidates import assign_screenshots, choose_answer, never_igdb_ids
from app.content.fetch import ForbiddenHostError, ResponseTooLargeError
from app.content.igdb import MAX_RETRIES, IgdbError, IgdbGame, parse_game
from app.content.seed import TEST_SALT, Purpose, SaltError, SlotSeed
from app.content.settings import ContentSettings
from app.content.target import TargetRefusedError
from app.core.config import settings as app_settings
from app.crud import puzzle as crud_puzzle
from app.crud.exclusion import list_exclusions
from app.crud.title import create_title
from app.media.writer import LocalMediaWriter, MediaWriteError, S3MediaWriter
from app.models.exclusion import AnswerExclusion
from app.models.game import Game
from app.models.progress import Progress
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue, PuzzleStatus
from app.models.title import Title
from app.models.user import User
from tests.factories import create_game, create_puzzle

NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.UTC)
LONG_AGO = int(dt.datetime(2015, 1, 1, tzinfo=dt.UTC).timestamp())
# The shortest clip the limits allow (8 s), small and slow, so the run stays quick.
TINY_CLIP = {"size": (320, 180), "fps": 12, "seconds": 8}


def make_game(igdb_id: int, *, screenshots: int = 8, **fields: Any) -> IgdbGame:
    """A game that passes every filter and has every hint's data."""
    raw = {
        "id": igdb_id,
        "name": f"Test Game {igdb_id}",
        "slug": f"test-game-{igdb_id}",
        "game_type": 0,
        "first_release_date": LONG_AGO + igdb_id * 86400 * 400,
        "total_rating_count": 100,
        "aggregated_rating": 84.5,
        "aggregated_rating_count": 10,
        "genres": [{"name": "Adventure"}],
        "involved_companies": [{"developer": True, "company": {"name": "Test Studio"}}],
        "release_dates": [{"date": LONG_AGO, "platform": {"name": "PC"}}],
        "screenshots": [
            {"image_id": f"g{igdb_id}s{number:02d}", "width": 1920, "height": 1080}
            for number in range(screenshots)
        ],
        **fields,
    }
    game = parse_game(raw, max_year=2027, skips=Counter(), drops=Counter())
    assert game is not None
    return game


class FakeIgdb:
    """The games source of a run. `calls` counts how often a run asked IGDB to be read, and
    `in_transaction` says whether the database session had a transaction open each time."""

    def __init__(
        self,
        games: list[IgdbGame],
        db: AsyncSession | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.games = games
        self.db = db
        self.fail = fail
        self.calls = 0
        self.in_transaction: list[bool] = []

    def __call__(self) -> AsyncIterator[IgdbGame]:
        self.calls += 1
        if self.db is not None:
            self.in_transaction.append(self.db.in_transaction())
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[IgdbGame]:
        if self.fail is not None:
            raise self.fail
        for game in self.games:
            yield game


class FakeImages:
    """Answers for `fetch_image`: a small JPEG for each image id, and a list of what was asked.
    `fail` is raised by the first fetch of a run, when set; `db` is the session whose open
    transaction is recorded at each fetch."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.db: AsyncSession | None = None
        self.in_transaction: list[bool] = []
        self.fail: Exception | None = None

    async def __call__(self, image_id: str) -> bytes:
        self.asked.append(image_id)
        if self.db is not None:
            self.in_transaction.append(self.db.in_transaction())
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        rng = random.Random(image_id)
        image = Image.frombytes("RGB", (16, 9), rng.randbytes(16 * 9 * 3))
        out = BytesIO()
        image.resize((320, 180), Image.Resampling.BICUBIC).save(out, "JPEG")
        return out.getvalue()


class StubS3Writer:
    """A writer that says it is a bucket and records what it was given."""

    kind = "s3"

    def __init__(self) -> None:
        self.puts: list[str] = []

    def exists(self, key: str) -> bool:
        return False

    def put(self, key: str, path: Path, content_type: str) -> bool:
        self.puts.append(key)
        return True


@dataclass
class Env:
    db: AsyncSession
    settings: ContentSettings
    storage: Path
    images: FakeImages
    game_id: int

    def builder(
        self,
        igdb: FakeIgdb,
        *,
        dry_run: bool = False,
        target: str = "local",
        settings: ContentSettings | None = None,
        writer: Any = None,
    ) -> Builder:
        return Builder(
            self.db,
            game_slug="game-guesser",
            target=target,
            settings=settings or self.settings,
            app_settings=app_settings,
            now=NOW,
            games=igdb,
            fetch_image=self.images,
            writer=writer if writer is not None else LocalMediaWriter(self.storage),
            dry_run=dry_run,
        )


def content_settings(tmp_path: Path, **fields: Any) -> ContentSettings:
    values: dict[str, Any] = {
        "PUZZLE_SEED_SALT": SecretStr(TEST_SALT),
        "CONTENT_WORK_DIR": tmp_path / "work",
        **fields,
    }
    return ContentSettings(_env_file=None, **values)


@pytest.fixture
async def env(db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(media, "clip", partial(media.clip, **TINY_CLIP))
    game = await create_game(db_session)
    await db_session.commit()
    images = FakeImages()
    images.db = db_session
    return Env(db_session, content_settings(tmp_path), tmp_path / "storage", images, game.id)


async def count(db: AsyncSession, model: type) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def clues_of(db: AsyncSession, puzzle: Puzzle) -> list[PuzzleClue]:
    result = await db.execute(
        select(PuzzleClue).where(PuzzleClue.puzzle_id == puzzle.id).order_by(PuzzleClue.step)
    )
    return list(result.scalars())


async def files_of(db: AsyncSession, clue: PuzzleClue) -> list[ClueMediaFile]:
    result = await db.execute(
        select(ClueMediaFile).where(ClueMediaFile.clue_id == clue.id).order_by(ClueMediaFile.id)
    )
    return list(result.scalars())


async def puzzle_in_slot(db: AsyncSession, slot: int) -> Puzzle:
    return (await db.execute(select(Puzzle).where(Puzzle.build_slot == slot))).scalar_one()


# (a) A real build ---------------------------------------------------------------------------


async def test__build__two_slots_write_in_review_drafts_and_a_second_run_adds_nothing(
    env: Env,
) -> None:
    igdb = FakeIgdb([make_game(1), make_game(2), make_game(3)], env.db)

    report = await env.builder(igdb).run(SlotRequest(slots=range(1, 3)))

    assert [outcome.status for outcome in report.outcomes] == [SlotStatus.BUILT] * 2
    puzzles = list((await env.db.execute(select(Puzzle).order_by(Puzzle.build_slot))).scalars())
    assert [(p.build_slot, p.status, p.date) for p in puzzles] == [
        (1, PuzzleStatus.IN_REVIEW, None),
        (2, PuzzleStatus.IN_REVIEW, None),
    ]
    assert {p.generator_version for p in puzzles} == {GENERATOR_VERSION}
    assert len({p.answer_title_id for p in puzzles}) == 2  # AC-25: slot 2 sees slot 1's answer
    for puzzle in puzzles:
        title = await env.db.get(Title, puzzle.answer_title_id)
        assert title is not None and title.igdb_id is not None
        clues = await clues_of(env.db, puzzle)
        assert [c.step for c in clues] == [1, 2, 3, 4, 5, 6]
        assert [c.media_type for c in clues] == ["image"] * 5 + ["video"]
        assert [c.hint_kind for c in clues] == [
            None, "critic_score", "platform", "genre", "year", "developer",
        ]  # fmt: skip
        assert clues[1].hint_value == "85"  # 84.5 rounds half up
        assert {c.source_url for c in clues} == {
            f"https://www.igdb.com/games/test-game-{title.igdb_id}"
        }
        image_ids = [image_id for c in clues for image_id in c.source_image_ids or []]
        assert len(image_ids) == len(set(image_ids)) >= 7  # no screenshot twice (AC-34, AC-37)
        stored = [f for c in clues for f in await files_of(env.db, c)]
        assert [(f.role, f.content_type) for f in stored] == [("source", "image/webp")] * 5 + [
            ("source", "video/mp4"),
            ("poster", "image/webp"),
        ]
        for file in stored:  # AC-39, AC-41: measured facts, keyed by the file's own SHA-256
            data = (env.storage / file.key).read_bytes()
            assert hashlib.sha256(data).hexdigest() == file.key
            assert len(data) == file.byte_size
            assert (file.duration_ms is not None) == (file.content_type == "video/mp4")
    # No transaction stays open across the IGDB read or the fetches (they can take minutes).
    assert igdb.in_transaction == [False] and env.images.in_transaction == [False] * 14
    asked_before, reads_before = list(env.images.asked), igdb.calls

    again = await env.builder(igdb).run(SlotRequest(slots=range(1, 3)))

    assert [outcome.status for outcome in again.outcomes] == [SlotStatus.EXISTS] * 2
    assert (env.images.asked, igdb.calls) == (asked_before, reads_before)  # nothing fetched or read
    assert await count(env.db, Puzzle) == 2 and await count(env.db, Title) == 2


# (b) Atomic write, dry run, played puzzles --------------------------------------------------


async def test__build__a_failure_while_writing_the_fifth_media_row_leaves_files_and_no_row(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = crud_puzzle.add_clue_media_file
    calls = 0

    async def fail_on_the_fifth(db: AsyncSession, clue_id: int, file: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise RuntimeError("the disk is full")
        await real(db, clue_id, file)

    monkeypatch.setattr(crud_puzzle, "add_clue_media_file", fail_on_the_fifth)
    igdb = FakeIgdb([make_game(1)])

    with pytest.raises(RuntimeError, match="disk is full"):
        await env.builder(igdb).run(SlotRequest(slots=range(1, 2)))

    # No half draft: no puzzle, clue, media row, and not the new title either (AC-46).
    assert [await count(env.db, m) for m in (Puzzle, PuzzleClue, ClueMediaFile, Title)] == [0] * 4
    # The files went first and stay (non-goal: clean-up).
    assert len(list(env.storage.iterdir())) == 7

    monkeypatch.setattr(crud_puzzle, "add_clue_media_file", real)
    report = await env.builder(igdb).run(SlotRequest(slots=range(1, 2)))

    assert report.outcomes[0].status == SlotStatus.BUILT
    assert [await count(env.db, m) for m in (Puzzle, PuzzleClue, ClueMediaFile, Title)] == [
        1, 6, 7, 1,
    ]  # fmt: skip


async def test__dry_run__does_every_step_but_writes_nothing_and_hides_the_answer(
    env: Env,
) -> None:
    igdb = FakeIgdb([make_game(1)], env.db)

    report = await env.builder(igdb, dry_run=True).run(SlotRequest(next_count=1))

    # A dry run leaves no transaction open either, during the read, the fetches or the encodes.
    assert igdb.in_transaction == [False] and env.images.in_transaction == [False] * 7
    assert not env.db.in_transaction()
    outcome = report.outcomes[0]
    assert outcome.status == SlotStatus.PLANNED and len(outcome.files) == 7
    assert [await count(env.db, m) for m in (Puzzle, PuzzleClue, ClueMediaFile, Title)] == [0] * 4
    assert not env.storage.exists()  # AC-45: not even the folder
    assert len(list((env.settings.content_work_path / "out" / "1").iterdir())) == 7
    assert "Test Game 1" not in "\n".join(report.lines())
    assert '"Test Game 1"' in "\n".join(report.lines(show_answers=True))


async def test__build__never_touches_a_slot_that_has_a_puzzle_with_stored_games(env: Env) -> None:
    game = await env.db.get(Game, env.game_id)
    assert game is not None
    answer = await create_title(env.db, name="Hades", igdb_id=1)
    played = await create_puzzle(env.db, game=game, answer=answer, puzzle_date=dt.date(2026, 11, 1))
    played.build_slot = 3
    account = User(email="player@example.com", hashed_password="not-a-real-hash")
    env.db.add(account)
    await env.db.flush()
    env.db.add(
        Progress(
            user_id=account.id,
            puzzle_id=played.id,
            status="in_progress",
            started_at=NOW,
            counts_for_streak=True,
            origin="play",
        )
    )
    await env.db.commit()
    clues_before = [(c.id, c.hint_value, c.source_url) for c in await clues_of(env.db, played)]
    answer_id = answer.id  # the run ends its read transaction, which expires loaded objects
    igdb = FakeIgdb([make_game(1), make_game(2)])

    report = await env.builder(igdb).run(SlotRequest(slots=range(3, 4)))

    assert report.outcomes[0].status == SlotStatus.EXISTS
    assert (igdb.calls, env.images.asked) == (0, [])
    await env.db.refresh(played)
    assert (played.answer_title_id, played.status, played.date) == (
        answer_id, PuzzleStatus.SCHEDULED, dt.date(2026, 11, 1),
    )  # fmt: skip
    assert [(c.id, c.hint_value, c.source_url) for c in await clues_of(env.db, played)] == (
        clues_before
    )
    assert (await count(env.db, Puzzle), await count(env.db, Title)) == (1, 1)


async def test__build__skips_and_reports_a_candidate_that_would_be_a_near_duplicate(
    env: Env,
) -> None:
    first, second = make_game(1), make_game(2)
    # A hand-made title with the name and year of the first game: the import dropped it (R-20).
    await create_title(env.db, name=first.name.upper(), year=first.release_year)
    await env.db.commit()

    report = await env.builder(FakeIgdb([first])).run(SlotRequest(slots=range(1, 2)))

    outcome = report.outcomes[0]
    assert (outcome.status, outcome.detail, outcome.near_duplicates) == (
        SlotStatus.FAILED, "no_candidate", 1,
    )  # fmt: skip
    assert (await count(env.db, Title), await count(env.db, Puzzle)) == (1, 0)

    report = await env.builder(FakeIgdb([first, second])).run(SlotRequest(slots=range(1, 2)))

    answer = await env.db.get(Title, (await puzzle_in_slot(env.db, 1)).answer_title_id)
    assert answer is not None and answer.igdb_id == second.id
    assert await count(env.db, Title) == 2


@pytest.mark.parametrize("dry_run", [False, True], ids=["real_run", "dry_run"])
async def test__build__a_draft_that_breaks_the_media_rule_fails_its_slot_not_the_run(
    env: Env, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    monkeypatch.setattr(builder_module, "layout_problem", lambda clues: "step 3 breaks the rules")
    igdb = FakeIgdb([make_game(1), make_game(2)])

    report = await env.builder(igdb, dry_run=dry_run).run(SlotRequest(slots=range(1, 3)))

    # Both slots are reported (the first one did not crash the run), also in a dry run.
    assert [(o.status, o.detail) for o in report.outcomes] == [
        (SlotStatus.FAILED, "media_layout")
    ] * 2
    assert [await count(env.db, m) for m in (Puzzle, PuzzleClue, ClueMediaFile, Title)] == [0] * 4


def media_file(role: str, content_type: str) -> crud_puzzle.MediaFileInput:
    return crud_puzzle.MediaFileInput(role, "0" * 64, content_type, 100, 640, 360, None)


def draft_clues(
    *, replace: dict[int, tuple[str, list[crud_puzzle.MediaFileInput]]] | None = None
) -> list[crud_puzzle.ClueInput]:
    """Six valid clues, steps 1 to 5 images and step 6 a video; `replace` swaps one step's media
    type and files."""
    webp = media_file("source", "image/webp")
    clues = []
    for step in range(1, 7):
        media_type, files = (
            ("image", [webp])
            if step < 6
            else ("video", [media_file("source", "video/mp4"), media_file("poster", "image/webp")])
        )
        media_type, files = (replace or {}).get(step, (media_type, files))
        clues.append(
            crud_puzzle.ClueInput(step, media_type, None, None, "https://example.com", [], files)
        )
    return clues


@pytest.mark.parametrize(
    ("replace", "is_valid"),
    [
        ({}, True),
        # The shared AC-13b rule: an image clue has one WebP and no poster.
        ({2: ("image", [media_file("source", "image/webp")] * 2)}, False),
        # The builder's own shape on top of it: steps 1 to 5 images, no WebM.
        (
            {3: ("video", [media_file("source", "video/mp4"), media_file("poster", "image/webp")])},
            False,
        ),
        (
            {
                6: (
                    "video",
                    [
                        media_file("source", "video/mp4"),
                        media_file("source", "video/webm"),
                        media_file("poster", "image/webp"),
                    ],
                )
            },
            False,
        ),
    ],
    ids=["valid", "two_images_in_one_clue", "video_at_step_3", "webm_at_step_6"],
)
def test__layout_problem__applies_the_shared_rule_and_the_builders_shape(
    replace: dict[int, tuple[str, list[crud_puzzle.MediaFileInput]]], is_valid: bool
) -> None:
    assert (builder_module.layout_problem(draft_clues(replace=replace)) is None) is is_valid


@pytest.mark.parametrize(
    ("error", "stops_the_run"),
    [
        (ImageUnavailableError("IGDB's image host does not have it (404)"), False),
        (ForbiddenHostError("x"), True),
        (ImageHostUnavailableError("IGDB's image host is unavailable: 503"), True),
    ],
    ids=["one_image_missing", "host_refused", "host_down"],
)
async def test__build__a_missing_image_fails_its_slot_but_a_refused_host_stops_the_run(
    env: Env, error: Exception, stops_the_run: bool
) -> None:
    env.images.fail = error
    builder = env.builder(FakeIgdb([make_game(1), make_game(2), make_game(3)]))
    report = BuildReport()

    if stops_the_run:
        with pytest.raises(type(error)):
            await builder.run(SlotRequest(slots=range(1, 3)), report)
        assert report.outcomes == []
        return
    await builder.run(SlotRequest(slots=range(1, 3)), report)

    assert [(o.status, o.detail) for o in report.outcomes] == [
        (SlotStatus.FAILED, "image: ImageUnavailableError"),
        (SlotStatus.BUILT, ""),
    ]


async def test__build__a_run_that_stops_keeps_the_report_of_the_slots_it_finished(
    env: Env,
) -> None:
    class FailingWriter(LocalMediaWriter):
        """The storage breaks while the second draft's files go in."""

        puts = 0

        def put(self, key: str, path: Path, content_type: str) -> bool:
            self.puts += 1
            if self.puts > 7:
                raise OSError("the disk is full")
            return super().put(key, path, content_type)

    report = BuildReport()

    # The storage error is named a `MediaWriteError` where the file is put.
    with pytest.raises(MediaWriteError, match="OSError"):
        await env.builder(
            FakeIgdb([make_game(1), make_game(2)]), writer=FailingWriter(env.storage)
        ).run(SlotRequest(slots=range(1, 3)), report)

    assert (report.slots, [(o.slot, o.status) for o in report.outcomes]) == (
        [1, 2],
        [(1, SlotStatus.BUILT)],
    )
    assert "slot 1: built" in report.lines()


# (c) Gates ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "storage", "fields", "error", "message"),
    [
        ("staging", "local", {}, TargetRefusedError, "IGDB_IMAGES_CLEARED_BY"),
        ("local", "s3", {}, TargetRefusedError, "IGDB_IMAGES_CLEARED_BY"),
        ("local", "local", {"PUZZLE_SEED_SALT": None}, SaltError, "PUZZLE_SEED_SALT is not set"),
        (
            "local",
            "local",
            {"PUZZLE_SEED_SALT": SecretStr("short")},
            SaltError,
            "placeholder or too short",
        ),
    ],
    ids=["images_gate_closed", "local_target_with_a_bucket", "no_salt", "weak_salt"],
)
async def test__builder__refuses_before_any_read_or_fetch(
    env: Env,
    tmp_path: Path,
    target: str,
    storage: str,
    fields: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    igdb = FakeIgdb([make_game(1)])
    refused = content_settings(tmp_path, **fields)
    writer = StubS3Writer() if storage == "s3" else None

    with pytest.raises(error, match=message):
        await env.builder(igdb, target=target, settings=refused, writer=writer).run(
            SlotRequest(next_count=1)
        )

    assert (igdb.calls, env.images.asked, await count(env.db, Puzzle)) == (0, [], 0)
    # The gate is the only thing that stopped it: with the decision named, the run goes on.
    cleared = content_settings(tmp_path, IGDB_IMAGES_CLEARED_BY="SPEC.md decision 72")
    if error is TargetRefusedError:
        report = await env.builder(
            igdb, target=target, settings=cleared, writer=writer, dry_run=True
        ).run(SlotRequest(next_count=1))
        assert report.outcomes[0].status == SlotStatus.PLANNED


def load_script() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "build_puzzles.py"
    spec = importlib.util.spec_from_file_location("p3_build_puzzles", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("argv", "environ", "message"),
    [
        (["--next", "3"], {"ENVIRONMENT": "local"}, "--target is required"),
        (["--dry-run", "--next", "3"], {"ENVIRONMENT": "local"}, "--target is required"),
        (["--give-up", "3"], {"ENVIRONMENT": "local"}, "--target is required"),
        (["--target", "local", "--next", "3"], {"ENVIRONMENT": "local", "CI": "true"}, "CI"),
        (
            ["--target", "local", "--next", "3"],
            {"ENVIRONMENT": "staging"},
            "ENVIRONMENT variable is 'staging'",
        ),
        (
            ["--target", "staging", "--confirm-target", "staging", "--next", "3"],
            {"ENVIRONMENT": "staging"},
            "IGDB_IMAGES_CLEARED_BY",
        ),
    ],
    ids=["no_target", "dry_run_no_target", "give_up_no_target", "ci", "environment", "images_gate"],
)
def test__build_script__guard_runs_first_and_prints_the_target_before_it_refuses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    environ: dict[str, str],
    message: str,
) -> None:
    for name in ("CI", "ENVIRONMENT", "IGDB_IMAGES_CLEARED_BY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("sys.argv", ["build_puzzles.py", *argv])

    code = load_script().main()

    captured = capsys.readouterr()
    assert code == 1 and message in captured.err
    # A refusal by the target guard prints nothing first; a refusal by the image gate comes after
    # the target line (AC-44).
    assert captured.out.startswith("Target: staging;") == (message == "IGDB_IMAGES_CLEARED_BY")


@pytest.mark.parametrize(
    "argv",
    [
        ["--target", "local", "--slots", "1-501"],
        ["--target", "local", "--next", "501"],
        ["--target", "local", "--give-up", "0"],
    ],
    ids=["range_over_the_limit", "next_over_the_limit", "slot_zero"],
)
def test__build_script__refuses_a_size_it_cannot_run_as_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["build_puzzles.py", *argv])

    with pytest.raises(SystemExit) as stopped:
        load_script().main()

    assert stopped.value.code == 2 and "usage" in capsys.readouterr().err


async def test__give_up_slot__refuses_rows_that_name_two_titles_and_changes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = (
        await create_title(env.db, name="One", igdb_id=1),
        await create_title(env.db, name="Two", igdb_id=2),
    )
    for title in (first, second):
        env.db.add(
            AnswerExclusion(
                game_id=env.game_id,
                variant="default",
                title_id=title.id,
                reason="rebuild",
                build_slot=4,
                image_ids=["a"],
            )
        )
    await env.db.commit()

    async def broken_rollback() -> None:
        raise OSError("the connection is gone")

    real_rollback = env.db.rollback
    monkeypatch.setattr(env.db, "rollback", broken_rollback)
    # A rollback that fails too does not replace the refusal.
    with pytest.raises(GiveUpError, match="more than one title"):
        await give_up_slot(env.db, "game-guesser", 4)
    monkeypatch.setattr(env.db, "rollback", real_rollback)

    assert [row.reason for row in await list_exclusions(env.db, env.game_id)] == ["rebuild"] * 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--counts"],
        ["--target", "local", "--give-up", "3"],
        ["--target", "local", "--dry-run", "--next", "1"],
    ],
    ids=["counts", "give_up", "dry_run"],
)
def test__build_script__a_database_that_is_down_is_a_database_error_not_a_file_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    for name, value in {
        "ENVIRONMENT": "local",
        "IGDB_CLIENT_ID": "id",
        "IGDB_CLIENT_SECRET": "a-long-test-client-secret-of-no-use",
        "PUZZLE_SEED_SALT": TEST_SALT,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CI", raising=False)
    # Port 1 refuses connections: asyncpg raises ConnectionRefusedError, an OSError.
    down = create_async_engine("postgresql+asyncpg://nobody:nothing@localhost:1/none")
    monkeypatch.setattr("app.core.db.async_session_maker", async_sessionmaker(down))
    monkeypatch.setattr("sys.argv", ["build_puzzles.py", *argv])

    code = load_script().main()

    err = capsys.readouterr().err
    assert code == 1 and "database error" in err and "storage or file error" not in err


@pytest.mark.parametrize(
    ("raised", "message", "extra_argv"),
    [
        (OSError("disk"), "storage or file error: OSError", []),
        (MediaWriteError("a file cannot be stored: OSError"), "storage or file error: a file", []),
        (IgdbError("IGDB is unavailable"), "IGDB is unavailable", []),
        (DatabaseFailure("ConnectionRefusedError"), "database error: ConnectionRefused", []),
        (SlotLimitError("past slot 999999"), "refusing to run: past slot 999999", []),
        (IgdbError("IGDB is unavailable"), "IGDB is unavailable", ["--dry-run"]),
        (RuntimeError("a bug"), None, []),
        (KeyboardInterrupt(), None, []),
    ],
    ids=["oserror", "media", "igdb", "database", "slot_limit", "dry_run", "a_bug", "ctrl_c"],
)
def test__build_script__a_run_that_stops_prints_what_it_finished_then_the_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raised: BaseException,
    message: str | None,
    extra_argv: list[str],
) -> None:
    for name, value in {
        "ENVIRONMENT": "local",
        "IGDB_CLIENT_ID": "id",
        "IGDB_CLIENT_SECRET": "a-long-test-client-secret-of-no-use",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CI", raising=False)
    script = load_script()

    async def stops_after_slot_one(*args: Any) -> None:
        report = args[-1]
        report.slots = [1, 2]
        report.outcomes.append(SlotOutcome(1, SlotStatus.BUILT))
        report.given_up.append(5)
        raise raised

    monkeypatch.setattr(script, "run_build", stops_after_slot_one)
    monkeypatch.setattr(
        "sys.argv", ["build_puzzles.py", "--target", "local", "--next", "2", *extra_argv]
    )

    if message is None:  # Not an error the script names: it is raised again after the report.
        with pytest.raises(type(raised)):
            script.main()
        code = None
    else:
        code = script.main()

    captured = capsys.readouterr()
    assert "slot 1: built" in captured.out
    assert "slot 5: rebuild given up (title rejected as never)" in captured.out
    assert ("Dry run: nothing was written" in captured.out) == bool(extra_argv)
    if message is not None:
        assert code == 1 and message in captured.err


def test__build_script__a_give_up_conflict_is_a_refusal_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.delenv("CI", raising=False)
    script = load_script()

    async def conflict(*args: Any) -> int:
        raise GiveUpError("the rebuild rows of slot 4 name more than one title")

    monkeypatch.setattr(script, "run_give_up", conflict)
    monkeypatch.setattr("sys.argv", ["build_puzzles.py", "--target", "local", "--give-up", "4"])

    assert script.main() == 1
    assert "refusing to run: the rebuild rows of slot 4" in capsys.readouterr().err


def test__build_script__local_runs_never_load_botocore() -> None:
    program = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('s', 'scripts/build_puzzles.py')\n"
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        "from app.core.config import settings\n"
        "from app.media.writer import build_media_writer\n"
        "assert build_media_writer(settings).kind == 'local'\n"
        "print('botocore' in sys.modules, 'boto3' in sys.modules)\n"
    )
    root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [sys.executable, "-c", program], cwd=root, capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "False False"


# AC-85: the fetch ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url_template", "error", "status", "body_size"),
    [
        (None, None, 200, 10),
        ("https://evil.example/{image_id}.jpg", ForbiddenHostError, 200, 10),
        ("http://images.igdb.com/{image_id}.jpg", ForbiddenHostError, 200, 10),
        (None, ResponseTooLargeError, 200, 5000),
        (None, ImageHostUnavailableError, 302, 10),
        (None, ImageUnavailableError, 404, 10),
        (None, ImageUnavailableError, 410, 10),
    ],
    ids=["ok", "foreign_host", "plain_http", "too_large", "redirect_is_not_followed", "404", "410"],
)
async def test__image_fetcher__goes_only_to_the_image_host_within_the_size_cap(
    monkeypatch: pytest.MonkeyPatch,
    url_template: str | None,
    error: type[Exception] | None,
    status: int,
    body_size: int,
) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"Location": "https://evil.example/x"} if status == 302 else {}
        return httpx.Response(status, content=b"x" * body_size, headers=headers)

    if url_template is not None:
        monkeypatch.setattr(builder_module, "IMAGE_URL", url_template)
    monkeypatch.setattr(builder_module, "MAX_IMAGE_BYTES", 1000)

    async with ImageFetcher(httpx.MockTransport(answer)) as fetcher:
        if error is None:
            assert await fetcher.fetch("abc123") == b"x" * 10
            assert [str(r.url) for r in requests] == [
                "https://images.igdb.com/igdb/image/upload/t_1080p/abc123.jpg"
            ]
        else:
            with pytest.raises(error):
                await fetcher.fetch("abc123")
            # One request at most: none for a refused host, and no retry for anything but 429 or 5xx.
            assert len(requests) == (0 if error is ForbiddenHostError else 1)


@pytest.mark.parametrize(
    ("answers", "expected_sleeps", "error"),
    [
        ([503, 429, 200], [1.0, 7.0], None),
        ([503] * (MAX_RETRIES + 1), [1.0, 2.0, 4.0, 8.0, 16.0], ImageHostUnavailableError),
    ],
    ids=["busy_then_fine", "stays_down"],
)
async def test__image_fetcher__retries_a_busy_host_in_bounds_and_gives_up_when_it_stays_down(
    answers: list[int], expected_sleeps: list[float], error: type[Exception] | None
) -> None:
    left = list(answers)
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = left.pop(0) if len(left) > 1 else left[0]
        return httpx.Response(
            status, content=b"image", headers={"Retry-After": "7"} if status == 429 else {}
        )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with ImageFetcher(httpx.MockTransport(answer), sleep=sleep) as fetcher:
        if error is None:
            assert await fetcher.fetch("abc123") == b"image"
        else:
            with pytest.raises(error):
                await fetcher.fetch("abc123")

    # Bounded: the first try and at most MAX_RETRIES more, waiting as the IGDB client does, with
    # Retry-After honoured.
    assert (len(requests), sleeps) == (len(answers), expected_sleeps)


# (d) Rebuilds, decision 71 and the give-up command ------------------------------------------


async def reject_as_rebuild(db: AsyncSession, slot: int) -> list[str]:
    """What the review page's reject does (M1-24a): the exclusion row gets the title, the slot and
    every screenshot of the draft, then the draft is deleted. Returns the image ids."""
    puzzle = await puzzle_in_slot(db, slot)
    image_ids = sorted(
        {i for clue in await clues_of(db, puzzle) for i in clue.source_image_ids or []}
    )
    db.add(
        AnswerExclusion(
            game_id=puzzle.game_id,
            variant=puzzle.variant,
            title_id=puzzle.answer_title_id,
            reason="rebuild",
            build_slot=slot,
            image_ids=image_ids,
        )
    )
    await db.execute(delete(Puzzle).where(Puzzle.id == puzzle.id))
    await db.commit()
    return image_ids


async def test__rebuild__avoids_every_rejected_screenshot_and_the_title_stays_in_its_slot(
    env: Env,
) -> None:
    # One game with 12 screenshots: exactly one rebuild fits (decisions 71 and 74): the draft uses 7
    # and the rebuild's 5 are all that is left, so its clip pans over its own stills.
    first = make_game(1, screenshots=12)
    other = make_game(2)
    igdb = FakeIgdb([first])
    await env.builder(igdb).run(SlotRequest(slots=range(1, 2)))
    answer_id = (await puzzle_in_slot(env.db, 1)).answer_title_id
    first_ids = await reject_as_rebuild(env.db, 1)

    # Another slot cannot draw the title that waits for slot 1 (AC-25).
    report = await env.builder(igdb).run(SlotRequest(slots=range(2, 3)))
    assert (report.outcomes[0].status, report.outcomes[0].detail) == (
        SlotStatus.FAILED, "no_candidate",
    )  # fmt: skip

    report = await env.builder(igdb).run(SlotRequest(rebuilds=True))
    assert report.outcomes[0].status == SlotStatus.BUILT and report.outcomes[0].is_rebuild
    rebuilt = await puzzle_in_slot(env.db, 1)
    assert rebuilt.answer_title_id == answer_id
    clues = await clues_of(env.db, rebuilt)
    stills = {clue.source_image_ids[0] for clue in clues[:5]}  # the crop and steps 2 to 5
    assert len(stills) == 5
    # Step 6 names the stills its clip was made from, so a rejected rebuild records no new ones.
    assert 2 <= len(clues[5].source_image_ids) <= 3 and set(clues[5].source_image_ids) <= stills
    second_ids = await reject_as_rebuild(env.db, 1)
    assert len(second_ids) == 5
    assert not set(first_ids) & set(second_ids)  # AC-52: other screenshots, other crop

    # The union of both rows leaves nothing: not rebuildable, and the row stays.
    report = await env.builder(igdb).run(SlotRequest(rebuilds=True))
    assert (report.outcomes[0].status, report.outcomes[0].detail) == (
        SlotStatus.FAILED, "cannot_rebuild",
    )  # fmt: skip
    assert await count(env.db, Puzzle) == 0

    # Giving up turns both rows into one "never" and frees the slot.
    assert await give_up_slot(env.db, "game-guesser", 1) == 2
    rows = await list_exclusions(env.db, env.game_id)
    assert [(row.reason, row.build_slot) for row in rows] == [("never", None)]
    assert never_igdb_ids(rows) == {first.id}
    assert await give_up_slot(env.db, "game-guesser", 1) == 0  # nothing left to give up

    igdb = FakeIgdb([first, other])
    report = await env.builder(igdb).run(SlotRequest(slots=range(1, 2)))
    assert report.outcomes[0].status == SlotStatus.BUILT and not report.outcomes[0].is_rebuild
    new_answer = await env.db.get(Title, (await puzzle_in_slot(env.db, 1)).answer_title_id)
    assert new_answer is not None and new_answer.igdb_id == other.id

    # A rebuilt draft the reviewer then rejects as "never" leaves rebuild rows for a title that is
    # out for good. The next run gives the slot up itself and builds a new answer (a dry run only
    # says so), and the title has one "never" row, not two.
    third, fourth = make_game(3), make_game(4)
    pool = [first, other, third, fourth]
    await env.builder(FakeIgdb(pool)).run(SlotRequest(slots=range(2, 3)))
    slot_two = await puzzle_in_slot(env.db, 2)
    rejected_title = slot_two.answer_title_id
    await reject_as_rebuild(env.db, 2)
    env.db.add(
        AnswerExclusion(
            game_id=env.game_id, variant="default", title_id=rejected_title, reason="never"
        )
    )
    await env.db.commit()
    never_rows = len([r for r in await list_exclusions(env.db, env.game_id) if r.reason == "never"])

    planned = await env.builder(FakeIgdb(pool), dry_run=True).run(SlotRequest(rebuilds=True))
    assert planned.outcomes[0].status == SlotStatus.PLANNED
    assert (planned.given_up, planned.would_give_up) == ([], [2])
    assert "would be given up (title rejected as never)" in "\n".join(planned.lines())
    assert [r.reason for r in await list_exclusions(env.db, env.game_id)].count("rebuild") == 1

    built = await env.builder(FakeIgdb(pool)).run(SlotRequest(rebuilds=True))
    assert built.outcomes[0].status == SlotStatus.BUILT
    assert (built.given_up, built.would_give_up) == ([2], [])
    assert not built.outcomes[0].is_rebuild
    assert "rebuild given up (title rejected as never)" in "\n".join(built.lines())
    rows = await list_exclusions(env.db, env.game_id)
    assert [r.reason for r in rows].count("rebuild") == 0
    assert [r.reason for r in rows].count("never") == never_rows
    replaced = await env.db.get(Title, (await puzzle_in_slot(env.db, 2)).answer_title_id)
    assert replaced is not None and replaced.id != rejected_title


async def add_rebuild_rows(env: Env, slot: int, *titles: Title, never: Title | None = None) -> None:
    """Rebuild rows of a slot for these titles, and a "never" row for `never`."""
    for title in titles:
        env.db.add(
            AnswerExclusion(
                game_id=env.game_id,
                variant="default",
                title_id=title.id,
                reason="rebuild",
                build_slot=slot,
                image_ids=["a"],
            )
        )
    if never is not None:
        env.db.add(
            AnswerExclusion(
                game_id=env.game_id, variant="default", title_id=never.id, reason="never"
            )
        )
    await env.db.commit()


async def rows_by_reason(env: Env) -> dict[str, int]:
    return dict(Counter(row.reason for row in await list_exclusions(env.db, env.game_id)))


@pytest.mark.parametrize("is_dry_run", [False, True], ids=["real_run", "dry_run"])
@pytest.mark.parametrize("stop", [False, True], ids=["slot_fails", "run_stops"])
async def test__build__a_give_up_is_reported_when_it_happens_whatever_the_slot_does_next(
    env: Env, is_dry_run: bool, stop: bool
) -> None:
    title = await create_title(env.db, name="Rejected", igdb_id=7)
    await add_rebuild_rows(env, 5, title, never=title)
    igdb = FakeIgdb([], fail=IgdbError("IGDB is unavailable") if stop else None)
    report = BuildReport()

    if stop:  # The give-up is committed before IGDB is read; a stop after it still shows it.
        with pytest.raises(IgdbError):
            await env.builder(igdb, dry_run=is_dry_run).run(SlotRequest(slots=range(5, 6)), report)
    else:  # No candidate is left for the freed slot: it fails, the give-up is still reported.
        await env.builder(igdb, dry_run=is_dry_run).run(SlotRequest(slots=range(5, 6)), report)
        assert [(o.status, o.detail) for o in report.outcomes] == [
            (SlotStatus.FAILED, "no_candidate")
        ]

    text = "\n".join(report.lines())
    if is_dry_run:  # Only "would be given up", and nothing is written.
        assert (report.given_up, report.would_give_up) == ([], [5])
        assert "rebuild given up" not in text and "would be given up" in text
        assert await rows_by_reason(env) == {"rebuild": 1, "never": 1}
    else:
        assert (report.given_up, report.would_give_up) == ([5], [])
        assert "slot 5: rebuild given up (title rejected as never)" in text
        assert await rows_by_reason(env) == {"never": 1}


async def test__build__rebuild_rows_that_cannot_be_given_up_fail_their_slot_and_not_the_run(
    env: Env,
) -> None:
    one, two = (
        await create_title(env.db, name="One", igdb_id=1),
        await create_title(env.db, name="Two", igdb_id=2),
    )
    await add_rebuild_rows(env, 6, one, two, never=one)
    igdb = FakeIgdb([])
    # A report that already holds an outcome of slot 7 does not make the run skip slot 7.
    report = BuildReport(outcomes=[SlotOutcome(7, SlotStatus.EXISTS, "an earlier try")])

    await env.builder(igdb).run(SlotRequest(slots=range(6, 8)), report)

    assert [(o.slot, o.status, o.detail) for o in report.outcomes] == [
        (7, SlotStatus.EXISTS, "an earlier try"),
        (6, SlotStatus.FAILED, "bad_rebuild_row"),
        (7, SlotStatus.FAILED, "no_candidate"),
    ]
    assert await rows_by_reason(env) == {"rebuild": 2, "never": 1}  # nothing was changed


async def test__build__rebuilds_are_capped_per_run_and_next_stops_at_the_last_slot_number(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder_module, "MAX_SLOTS_PER_RUN", 2)
    title = await create_title(env.db, name="Waiting", igdb_id=3)
    title_id = title.id
    for slot in (1, 2, 3):
        await add_rebuild_rows(env, slot, title)

    report = await env.builder(FakeIgdb([]), dry_run=True).run(SlotRequest(rebuilds=True))

    assert (report.slots, report.left_over) == ([1, 2], 1)
    assert "1 more slot(s) wait for a rebuild: run again" in "\n".join(report.lines())

    game = await env.db.get(Game, env.game_id)
    answer = await env.db.get(Title, title_id)
    assert game is not None and answer is not None
    last = await create_puzzle(env.db, game=game, answer=answer, puzzle_date=dt.date(2026, 11, 1))
    last.build_slot = MAX_SLOT_NUMBER
    await env.db.commit()
    with pytest.raises(SlotLimitError, match="would go past"):
        await env.builder(FakeIgdb([]), dry_run=True).run(SlotRequest(next_count=1))


async def test__database_session__names_database_errors_and_leaves_storage_errors_alone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Maker:
        def __call__(self) -> "Maker":
            return self

        async def __aenter__(self) -> AsyncSession:
            return env.db

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    class FullDisk(LocalMediaWriter):
        def put(self, key: str, path: Path, content_type: str) -> bool:
            raise OSError("the disk is full")

    monkeypatch.setattr("app.core.db.async_session_maker", Maker())

    with pytest.raises(DatabaseFailure, match="ConnectionRefusedError"):
        async with database_session():
            raise ConnectionRefusedError("down")
    # A storage error is named before it reaches the session: it is not taken for the database's.
    with pytest.raises(MediaWriteError):
        async with database_session() as db:
            builder = env.builder(FakeIgdb([make_game(1)]), writer=FullDisk(env.storage))
            assert builder.db is db
            await builder.run(SlotRequest(slots=range(1, 2)))


# (e) What a run keeps and prints ------------------------------------------------------------


async def test__build__stores_the_seeds_choices_with_every_screenshot_at_its_own_step(
    env: Env, tmp_path: Path
) -> None:
    games = [make_game(number, screenshots=21) for number in range(1, 5)]
    # The seed is the slug, the variant, the slot and the generator version: nothing else decides.
    seed = SlotSeed(
        SecretStr(TEST_SALT), "game-guesser", "default", 1, GENERATOR_VERSION, other_secrets=()
    )
    answer = choose_answer(games, seed.stream(Purpose.ANSWER))
    plan = assign_screenshots(
        answer, seed.stream(Purpose.SCREENSHOTS), set(), settings=env.settings
    )
    assert len(plan.clip) == 3  # a clip of three, so its order can show

    await env.builder(FakeIgdb(games)).run(SlotRequest(slots=range(1, 2)))

    puzzle = await puzzle_in_slot(env.db, 1)
    title = await env.db.get(Title, puzzle.answer_title_id)
    assert title is not None and title.igdb_id == answer.id
    clues = await clues_of(env.db, puzzle)
    assert [clue.source_image_ids for clue in clues] == [
        [plan.crop.image_id],
        *([shot.image_id] for shot in plan.steps),
        [shot.image_id for shot in plan.clip],
    ]
    # Each file is made from the screenshot its clue names: make them again from the raw cache.
    raw = env.settings.content_work_path / "raw"
    again = [tmp_path / f"step{step}.webp" for step in range(1, 6)]
    media.pixelated_crop(raw / plan.crop.image_id, again[0], seed.stream(Purpose.CROP))
    for path, shot in zip(again[1:], plan.steps, strict=True):
        media.still(raw / shot.image_id, path)
    assert [(await files_of(env.db, clue))[0].key for clue in clues[:5]] == [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in again
    ]


async def test__dry_run__previews_the_real_run_and_leaves_its_screenshots_for_it(env: Env) -> None:
    igdb = FakeIgdb([make_game(1)])
    request = SlotRequest(slots=range(1, 3))

    planned = await env.builder(igdb, dry_run=True).run(request)

    fetched = list(env.images.asked)
    assert len(fetched) == 7
    # A planned answer is taken, as a written one is (AC-25): slot 2 has nothing left to draw.
    assert [(o.status, o.detail) for o in planned.outcomes] == [
        (SlotStatus.PLANNED, ""),
        (SlotStatus.FAILED, "no_candidate"),
    ]
    assert planned.outcomes[0].hint_steps == [2, 3, 4, 5, 6]

    built = await env.builder(igdb).run(request)

    assert [(o.status, o.detail) for o in built.outcomes] == [
        (SlotStatus.BUILT, ""),
        (SlotStatus.FAILED, "no_candidate"),
    ]
    assert built.outcomes[0].answer_name == planned.outcomes[0].answer_name
    assert env.images.asked == fetched  # kept by image id in the working folder, not fetched again


async def test__build__near_duplicates_are_counted_for_the_slot_that_met_them(env: Env) -> None:
    games = [make_game(number) for number in (1, 2, 3, 4)]
    for game in games[:3]:  # hand-made titles with these names and years: near-duplicates (R-20)
        await create_title(env.db, name=game.name.upper(), year=game.release_year)
    await env.db.commit()

    report = await env.builder(FakeIgdb(games), dry_run=True).run(SlotRequest(slots=range(1, 3)))

    # Slot 1 takes the only clean game; slot 2 meets all three near-duplicates, and only them.
    assert [(o.slot, o.status, o.detail) for o in report.outcomes] == [
        (1, SlotStatus.PLANNED, ""),
        (2, SlotStatus.FAILED, "no_candidate"),
    ]
    assert report.outcomes[1].near_duplicates == 3


async def test__build__rebuilds_of_two_slots_in_one_run_each_avoid_their_own_rejected_drafts(
    env: Env,
) -> None:
    # 21 screenshots, two rejected drafts of 7 each per slot: exactly the last 7 remain.
    games = [make_game(number, screenshots=21) for number in (1, 2)]
    for game in games:
        title = await create_title(env.db, name=game.name, igdb_id=game.id)
        for first in (0, 7):
            env.db.add(
                AnswerExclusion(
                    game_id=env.game_id,
                    variant="default",
                    title_id=title.id,
                    reason="rebuild",
                    build_slot=game.id,
                    image_ids=[f"g{game.id}s{number:02d}" for number in range(first, first + 7)],
                )
            )
    await env.db.commit()

    report = await env.builder(FakeIgdb(games)).run(SlotRequest(rebuilds=True))

    assert [(o.slot, o.status, o.is_rebuild) for o in report.outcomes] == [
        (1, SlotStatus.BUILT, True),
        (2, SlotStatus.BUILT, True),
    ]
    for slot in (1, 2):
        clues = await clues_of(env.db, await puzzle_in_slot(env.db, slot))
        used = {image_id for clue in clues for image_id in clue.source_image_ids or []}
        assert used == {f"g{slot}s{number:02d}" for number in range(14, 21)}


async def test__build__rebuild_rows_of_another_variant_neither_wait_nor_force_nor_give_up(
    env: Env,
) -> None:
    rejected, other = make_game(7), make_game(8)
    title = await create_title(env.db, name=rejected.name, igdb_id=rejected.id)
    for reason, slot in (("rebuild", 5), ("never", None)):
        env.db.add(
            AnswerExclusion(
                game_id=env.game_id,
                variant="other" if reason == "rebuild" else "default",
                title_id=title.id,
                reason=reason,
                build_slot=slot,
                image_ids=["a"] if reason == "rebuild" else None,
            )
        )
    await env.db.commit()
    igdb = FakeIgdb([rejected, other])

    waiting = await env.builder(igdb, dry_run=True).run(SlotRequest(rebuilds=True))
    report = await env.builder(igdb, dry_run=True).run(SlotRequest(slots=range(5, 6)))

    assert (waiting.slots, waiting.left_over) == ([], 0)  # no slot of this variant waits
    assert (report.given_up, report.would_give_up) == ([], [])
    outcome = report.outcomes[0]
    assert (outcome.status, outcome.is_rebuild, outcome.answer_name) == (
        SlotStatus.PLANNED, False, other.name,
    )  # fmt: skip
    assert await rows_by_reason(env) == {"rebuild": 1, "never": 1}


async def test__build__rebuilds_that_wait_do_not_count_a_slot_that_already_has_its_puzzle(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder_module, "MAX_SLOTS_PER_RUN", 2)
    title = await create_title(env.db, name="Waiting", igdb_id=3)
    title_id = title.id
    for slot in (1, 2, 3, 4):  # the rows of slot 2 stay after its rebuild was built (decision 71)
        await add_rebuild_rows(env, slot, title)
    game = await env.db.get(Game, env.game_id)
    answer = await env.db.get(Title, title_id)
    assert game is not None and answer is not None
    rebuilt = await create_puzzle(
        env.db, game=game, answer=answer, puzzle_date=dt.date(2026, 11, 1)
    )
    rebuilt.build_slot = 2
    await env.db.commit()

    report = await env.builder(FakeIgdb([]), dry_run=True).run(SlotRequest(rebuilds=True))

    # Slots 1 and 3 are built, slot 4 waits; slot 2 is neither built nor waiting.
    assert (report.slots, report.left_over) == ([1, 3], 1)
    assert [outcome.slot for outcome in report.outcomes] == [1, 3]


def test__slot_request__the_last_slot_number_and_the_largest_run_are_allowed() -> None:
    none: set[int] = set()
    assert SlotRequest(slots=range(MAX_SLOT_NUMBER, MAX_SLOT_NUMBER + 1)).pick(
        puzzle_slots=none, rebuild_slots=none
    ) == [MAX_SLOT_NUMBER]
    assert len(SlotRequest(slots=range(1, 501)).pick(puzzle_slots=none, rebuild_slots=none)) == 500
    assert SlotRequest(next_count=500).next_count == 500
    with pytest.raises(ValueError):
        SlotRequest(slots=range(MAX_SLOT_NUMBER + 1, MAX_SLOT_NUMBER + 2))
    # `--next` may end on the last slot number, and not one past it.
    one_before = {MAX_SLOT_NUMBER - 1}
    assert SlotRequest(next_count=1).pick(puzzle_slots=one_before, rebuild_slots=none) == [
        MAX_SLOT_NUMBER
    ]
    with pytest.raises(SlotLimitError):
        SlotRequest(next_count=2).pick(puzzle_slots=one_before, rebuild_slots=none)


@pytest.mark.parametrize("show_answers", [False, True], ids=["answers_hidden", "answers_shown"])
def test__build_script__prints_a_planned_answer_only_when_asked_and_slots_in_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], show_answers: bool
) -> None:
    for name, value in {
        "ENVIRONMENT": "local",
        "IGDB_CLIENT_ID": "id",
        "IGDB_CLIENT_SECRET": "a-long-test-client-secret-of-no-use",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CI", raising=False)
    script = load_script()

    async def fills_the_report(*args: Any) -> None:
        report = args[-1]
        report.slots = [1, 2, 3]
        report.outcomes += [
            SlotOutcome(2, SlotStatus.EXISTS, "the slot already has a puzzle"),
            SlotOutcome(3, SlotStatus.FAILED, "media_layout", answer_name="Secret Quest"),
            SlotOutcome(1, SlotStatus.PLANNED, answer_name="Secret Quest", hint_steps=[2, 3]),
        ]

    monkeypatch.setattr(script, "run_build", fills_the_report)
    argv = ["build_puzzles.py", "--target", "local", "--dry-run", "--next", "3"]
    monkeypatch.setattr("sys.argv", [*argv, *(["--show-answers"] if show_answers else [])])

    code = script.main()

    captured = capsys.readouterr()
    assert code == 1  # a failed slot
    assert "Secret Quest" not in captured.err
    # Not in any line of the report, a failure's included, unless the answers were asked for.
    assert ("Secret Quest" in captured.out) == show_answers
    assert (
        captured.out.index("slot 1: planned") < captured.out.index("slot 2: exists")
        < captured.out.index("slot 3: failed")
    )  # fmt: skip
    assert "hints at steps: 2, 3" in captured.out


async def test__database_session__always_disposes_the_engine_and_a_failing_dispose_hides_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Engine:
        disposed = 0
        fails = False

        async def dispose(self) -> None:
            self.disposed += 1
            if self.fails:
                raise OSError("the pool is gone")

    class Maker:
        def __call__(self) -> "Maker":
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    engine = Engine()
    monkeypatch.setattr("app.core.db.async_session_maker", Maker())
    monkeypatch.setattr("app.core.db.engine", engine)

    async with database_session():
        pass
    with pytest.raises(DatabaseFailure):
        async with database_session():
            raise ConnectionRefusedError("down")
    assert engine.disposed == 2  # after a good run and after a failed one

    engine.fails = True  # a dispose that fails neither fails a good run nor replaces an error
    async with database_session():
        pass
    with pytest.raises(DatabaseFailure, match="ConnectionRefusedError"):
        async with database_session():
            raise ConnectionRefusedError("down")
    assert engine.disposed == 4


# Storage writer -----------------------------------------------------------------------------


def test__local_writer__stores_once_by_key_and_leaves_no_half_file(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"clue bytes")
    key = hashlib.sha256(b"clue bytes").hexdigest()
    writer = LocalMediaWriter(tmp_path / "storage")

    assert (writer.put(key, source, "image/webp"), writer.put(key, source, "image/webp")) == (
        True, False,
    )  # fmt: skip
    assert [p.name for p in (tmp_path / "storage").iterdir()] == [key]  # no temp file left
    assert (tmp_path / "storage" / key).read_bytes() == b"clue bytes"
    with pytest.raises(MediaWriteError):
        writer.put("../outside", source, "image/webp")
    # A write that fails half way leaves neither the key nor a temp file.
    broken_key = hashlib.sha256(b"never stored").hexdigest()
    with pytest.raises(OSError):
        writer.put(broken_key, tmp_path, "image/webp")  # a folder cannot be read as a file
    assert [p.name for p in (tmp_path / "storage").iterdir()] == [key]


@pytest.mark.parametrize(
    ("head_code", "is_put"),
    [("404", True), ("403", True), ("500", False)],
    ids=["missing", "forbidden_head_of_a_missing_key", "other_error"],
)
def test__s3_writer__skips_a_key_that_exists_and_puts_a_private_object(
    tmp_path: Path, head_code: str, is_put: bool
) -> None:
    writer = S3MediaWriter(
        endpoint_url="https://s3.test",
        region="eu",
        bucket="b",
        access_key_id="a",
        secret_access_key="s",
    )
    stored: list[dict[str, Any]] = []

    class StubClient:
        def head_object(self, **kwargs: Any) -> None:
            if kwargs["Key"] not in {obj["Key"] for obj in stored}:
                raise ClientError({"Error": {"Code": head_code}}, "HeadObject")

        def put_object(self, **kwargs: Any) -> None:
            kwargs["Body"].read()
            stored.append(kwargs)

    writer.client = StubClient()
    source = tmp_path / "f"
    source.write_bytes(b"x")
    key = hashlib.sha256(b"x").hexdigest()

    if not is_put:  # a bucket error that is not "missing" stops the write, as a storage error
        with pytest.raises(MediaWriteError, match="500"):
            writer.put(key, source, "video/mp4")
        assert stored == []
        return
    # The store's "cannot tell" is its own answer for a caller that must not act on "missing".
    assert writer.exists_or_unknown(key) is (None if head_code == "403" else False)
    assert writer.exists(key) is False
    # A 403 on the HEAD is "not known to exist": the object is written (the same bytes again are
    # harmless, the key is the hash of the content). The second put then finds it.
    assert (writer.put(key, source, "video/mp4"), writer.put(key, source, "video/mp4")) == (
        True,
        False,
    )
    assert len(stored) == 1
    assert stored[0]["ContentType"] == "video/mp4" and "ACL" not in stored[0]


def test__s3_writer__names_every_bucket_error_a_media_write_error(tmp_path: Path) -> None:
    writer = S3MediaWriter(
        endpoint_url="https://s3.test",
        region="eu",
        bucket="b",
        access_key_id="a",
        secret_access_key="s",
    )
    source = tmp_path / "f"
    source.write_bytes(b"x")
    key = hashlib.sha256(b"x").hexdigest()
    head_error: Exception = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    put_error: Exception = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    class StubClient:
        def head_object(self, **kwargs: Any) -> None:
            raise head_error

        def put_object(self, **kwargs: Any) -> None:
            raise put_error

    writer.client = StubClient()

    with pytest.raises(MediaWriteError, match="refused a PUT: AccessDenied"):
        writer.put(key, source, "image/webp")
    put_error = EndpointConnectionError(endpoint_url="https://s3.test")
    with pytest.raises(MediaWriteError, match="cannot be reached: EndpointConnectionError"):
        writer.put(key, source, "image/webp")
    head_error = EndpointConnectionError(endpoint_url="https://s3.test")
    with pytest.raises(MediaWriteError, match="cannot be reached"):
        writer.exists(key)
    head_error = ClientError({"Error": {"Code": "500"}}, "HeadObject")
    with pytest.raises(MediaWriteError, match="refused a HEAD: 500"):
        writer.exists(key)
