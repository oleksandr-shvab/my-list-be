"""The puzzle builder (SPEC-04 AC-26 to AC-33, AC-39 to AC-46, AC-50 to AC-52, AC-86).

For each slot, in ascending order: choose the answer and the screenshots from the seed (a rebuild
forces its excluded title and leaves out the screenshots of every rejected draft of the slot), fetch
the screenshots from IGDB's image host through the allow-listed client, make the six clues' files
with `app/content/media.py`, then store the files under their SHA-256 and write the draft whole or
not at all: one transaction with the title (when the list does not hold it yet), the puzzle in
`in_review` with no date, six clues and their media rows measured from the files. The files go
first, so a failed write leaves files and no row (non-goal: clean-up). A slot that already has a
puzzle is skipped and never touched, so a re-run adds nothing and a puzzle with stored games is
never edited (AC-46, AC-86). `dry_run` does everything but write to the database and storage
(AC-45).

The `rebuild` exclusion rows of a slot are never deleted here: the next rebuild leaves out the
screenshots of every rejected draft of the slot, the union of all its rows (decision 71). A slot
needs a rebuild while it has such a row and no puzzle; `give_up_slot` ends that.

Library code: it returns a `BuildReport` and the script prints it. Nothing here prints, and no
answer is in the report's lines unless asked for.
"""

import asyncio
import contextlib
import datetime as dt
import json
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Self

import asyncpg
import httpx
from av.error import FFmpegError
from PIL import Image
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.content import media
from app.content.candidates import (
    FunnelCounter,
    NotBuildableError,
    ScreenshotPlan,
    assign_screenshots,
    choose_answer,
    funnel,
    never_igdb_ids,
    rebuild_avoid_ids,
    rebuild_reserved_igdb_ids,
)
from app.content.fetch import FetchError, ResponseTooLargeError, build_client, send_capped
from app.content.hints import hints, source_url
from app.content.igdb import MAX_RETRIES, IgdbGame, retry_delay
from app.content.media import MediaFacts, MediaLimitError
from app.content.seed import Purpose, SlotSeed, check_salt, reserved_secrets
from app.content.settings import ContentSettings
from app.content.target import check_image_gate
from app.content.titles_import import ImportRecord, to_record
from app.core.clock import utc_today
from app.core.config import Settings
from app.crud.exclusion import ExclusionRow, give_up_rebuild, list_exclusions
from app.crud.game import get_game_by_slug
from app.crud.puzzle import (
    ClueInput,
    MediaFileInput,
    add_built_puzzle,
    list_answer_igdb_ids,
    list_puzzle_slots,
)
from app.crud.title import create_title, find_key_holders, get_titles_by_igdb_ids
from app.games.enums import ClueContentType, ClueRole, ExclusionReason, MediaType
from app.games.modules.game_guesser import clue_layout_problem
from app.media.hashing import sha256_file
from app.media.writer import MediaWriter, storage_call, write_atomically
from app.models.puzzle import PuzzleStatus
from app.models.title import TitleKind

# Raise this whenever the draw algorithm or its order changes (R-8): the same inputs must then give
# other choices. Funnel thresholds and candidate data (settings, a new IGDB import) are not covered.
# The golden tests of `tests/test_content_seed.py` and `tests/test_candidates.py` go red first.
GENERATOR_VERSION = "1"

# IGDB's image host, the size the clues are made from (`t_1080p`).
IMAGE_URL = "https://images.igdb.com/igdb/image/upload/t_1080p/{image_id}.jpg"
IMAGE_ID = re.compile(r"[a-z0-9]+", re.ASCII)
MAX_IMAGE_BYTES = 15 * 1024 * 1024
# A typo like `--slots 1-99999999` must not start a run that never ends.
MAX_SLOTS_PER_RUN = 500
# Slot numbers have at most six digits, in `--slots`, `--give-up` and what `--next` may reach.
MAX_SLOT_NUMBER = 999_999
STEPS = 6

FetchImage = Callable[[str], Awaitable[bytes]]
GamesSource = Callable[[], AsyncIterator[IgdbGame]]


class SlotError(Exception):
    """A slot cannot be built; the run goes on with the next one. `reason` is stable."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnknownGameError(Exception):
    """The database has no game with the slug that was asked for."""


class GiveUpError(Exception):
    """A slot's rebuild rows cannot be given up (they name more than one title)."""


class SlotLimitError(Exception):
    """The slots a run would build go past `MAX_SLOT_NUMBER`."""


class DatabaseFailure(Exception):
    """A database call failed. The message is the error's type only: no statement, no value."""


@contextlib.contextmanager
def database_errors() -> Iterator[None]:
    """Name an `SQLAlchemyError`, `OSError` or asyncpg error raised inside the block
    `DatabaseFailure`. An unreachable Postgres raises `ConnectionRefusedError`, an `OSError` like a
    file error, and a database or a password that is wrong raises an asyncpg error that SQLAlchemy
    does not wrap when it comes from opening the connection, so what is database work has to be
    named in one place. Storage and file work inside the block must be classified before it gets
    here (`MediaWriteError`, a slot error), or it would be named wrongly."""
    try:
        yield
    except (SQLAlchemyError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as error:
        raise DatabaseFailure(type(error).__name__) from error


@contextlib.asynccontextmanager
async def database_session() -> AsyncIterator[AsyncSession]:
    """The session of a script run, for its whole lifetime: opening it, every call made with it
    and closing it (with the engine) are classified by `database_errors`, so a down database is a
    `DatabaseFailure` whichever call finds out. The builder classifies its storage and file errors
    before they reach this."""
    from app.core import db as core_db  # Looked up at call time, so a test can replace the maker.

    try:
        with database_errors():
            async with core_db.async_session_maker() as session:
                yield session
    finally:
        with contextlib.suppress(Exception):
            await core_db.engine.dispose()


class ImageUnavailableError(FetchError):
    """IGDB's image host does not have this one screenshot (404 or 410). It fails the slot, not
    the run; a refused host, which is a `ForbiddenHostError`, stops the run."""


class ImageHostUnavailableError(FetchError):
    """The image host keeps answering 429 or 5xx, or something unexpected: it stops the run, as a
    network error does, so the run does not hammer a host that is down."""


# Only these mean "this screenshot is missing".
MISSING_IMAGE_STATUSES = frozenset({404, 410})


class ImageFetcher:
    """Fetches IGDB screenshots through the allow-listed client (AC-85): `images.igdb.com` only,
    https only, no redirect, a size cap. A 429 or 5xx is retried like the IGDB client does (at most
    `MAX_RETRIES` times, `Retry-After` honoured). The bytes are untrusted; `media` decodes and
    re-encodes them."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = build_client(transport)
        self._sleep = sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._http.aclose()

    async def fetch(self, image_id: str) -> bytes:
        if IMAGE_ID.fullmatch(image_id) is None:
            raise FetchError("an IGDB image id has only lowercase letters and digits")
        retries = 0
        while True:
            request = self._http.build_request("GET", IMAGE_URL.format(image_id=image_id))
            response = await send_capped(self._http, request, max_bytes=MAX_IMAGE_BYTES)
            status = response.status_code
            if status == 200:
                return response.content
            if status in MISSING_IMAGE_STATUSES:
                raise ImageUnavailableError(f"IGDB's image host does not have it ({status})")
            if status != 429 and status < 500:
                raise ImageHostUnavailableError(f"IGDB's image host answered {status}")
            if retries >= MAX_RETRIES:
                raise ImageHostUnavailableError(
                    f"IGDB's image host is unavailable: {status}, after {retries} retries"
                )
            await self._sleep(retry_delay(retries, response.headers.get("retry-after")))
            retries += 1


class SlotStatus(StrEnum):
    BUILT = "built"
    PLANNED = "planned"  # dry run: everything but the writes
    EXISTS = "exists"  # the slot already has a puzzle
    FAILED = "failed"


@dataclass(frozen=True)
class FilePlan:
    step: int
    role: str
    facts: MediaFacts


@dataclass
class SlotOutcome:
    slot: int
    status: SlotStatus
    detail: str = ""
    answer_name: str | None = None
    is_rebuild: bool = False
    files: list[FilePlan] = field(default_factory=list)
    hint_steps: list[int] = field(default_factory=list)
    near_duplicates: int = 0


@dataclass
class BuildReport:
    """Filled as the run goes, so a run that stops half way still shows the slots it finished."""

    slots: list[int] = field(default_factory=list)
    outcomes: list[SlotOutcome] = field(default_factory=list)
    # Slots whose rebuild title was rejected as "never": the rebuild rows are given up (written), or
    # in a dry run would be. Recorded the moment it happens, whatever the slot does afterwards.
    given_up: list[int] = field(default_factory=list)
    would_give_up: list[int] = field(default_factory=list)
    # `--rebuilds` builds at most `MAX_SLOTS_PER_RUN` slots a run; the rest wait for the next run.
    left_over: int = 0

    @property
    def has_content(self) -> bool:
        return bool(self.slots or self.outcomes or self.given_up or self.would_give_up)

    @property
    def is_ok(self) -> bool:
        return all(outcome.status != SlotStatus.FAILED for outcome in self.outcomes)

    def lines(self, *, show_answers: bool = False) -> list[str]:
        """What the script prints. The answer's name only with `show_answers` (AC-45, AC-83), and
        then as JSON text, so a control character in IGDB's data cannot reach the terminal. Never a
        media key, a path or a screenshot id."""
        lines = [f"slots: {_range_text(self.slots)}"]
        if self.left_over:
            lines.append(f"{self.left_over} more slot(s) wait for a rebuild: run again")
        lines += [
            f"slot {slot}: rebuild given up (title rejected as never)"
            for slot in sorted(self.given_up)
        ]
        lines += [
            f"slot {slot}: rebuild would be given up (title rejected as never)"
            for slot in sorted(self.would_give_up)
        ]
        for outcome in sorted(self.outcomes, key=lambda outcome: outcome.slot):
            head = f"slot {outcome.slot}: {outcome.status}"
            if outcome.is_rebuild:
                head += " (rebuild)"
            if outcome.detail:
                head += f": {outcome.detail}"
            lines.append(head)
            if outcome.status in (SlotStatus.BUILT, SlotStatus.PLANNED):
                lines.append(f"  hints at steps: {', '.join(map(str, outcome.hint_steps)) or '-'}")
                for plan in outcome.files:
                    facts = plan.facts
                    size = f"{facts.byte_size / media.MB:.2f} MB, {facts.width}x{facts.height}"
                    duration = "" if facts.duration_ms is None else f", {facts.duration_ms} ms"
                    lines.append(
                        f"  step {plan.step} {plan.role}: {facts.content_type}, {size}{duration}"
                    )
                if show_answers and outcome.answer_name is not None:
                    lines.append(f"  answer: {json.dumps(outcome.answer_name)}")
            if outcome.near_duplicates:
                lines.append(
                    f"  candidates left out (their name and year are taken by another title,"
                    f" or the name does not fit the list): {outcome.near_duplicates}"
                )
        counts = {status: sum(o.status == status for o in self.outcomes) for status in SlotStatus}
        lines.append(", ".join(f"{status}: {count}" for status, count in counts.items() if count))
        return lines


def _range_text(slots: Sequence[int]) -> str:
    if not slots:
        return "none"
    return str(slots[0]) if len(slots) == 1 else f"{slots[0]} to {slots[-1]}"


@dataclass(frozen=True)
class SlotRequest:
    """Which slots a run builds: a range (`--slots`), the next N after the highest slot in use
    (`--next`), or every slot waiting for a rebuild (`--rebuilds`). Exactly one."""

    slots: range | None = None
    next_count: int | None = None
    rebuilds: bool = False

    def __post_init__(self) -> None:
        if (self.slots is not None) + (self.next_count is not None) + self.rebuilds != 1:
            raise ValueError("ask for exactly one of a slot range, a next count and the rebuilds")
        count = len(self.slots) if self.slots is not None else self.next_count or 0
        if self.slots is not None and (
            self.slots.start < 1 or count < 1 or self.slots.stop - 1 > MAX_SLOT_NUMBER
        ):
            raise ValueError(f"slots go from 1 to {MAX_SLOT_NUMBER} and a range has at least one")
        if not 0 <= count <= MAX_SLOTS_PER_RUN:
            raise ValueError(f"a run builds at most {MAX_SLOTS_PER_RUN} slots")

    def pick(self, *, puzzle_slots: set[int], rebuild_slots: set[int]) -> list[int]:
        """The slots of the run. `--rebuilds` takes the first `MAX_SLOTS_PER_RUN` waiting slots;
        `SlotLimitError` when `--next` would go past `MAX_SLOT_NUMBER`."""
        if self.rebuilds:
            return sorted(rebuild_slots - puzzle_slots)[:MAX_SLOTS_PER_RUN]
        if self.next_count is not None:
            highest = max(puzzle_slots | rebuild_slots, default=0)
            if highest + self.next_count > MAX_SLOT_NUMBER:
                raise SlotLimitError(
                    f"{self.next_count} more slots after slot {highest} would go past"
                    f" slot {MAX_SLOT_NUMBER}"
                )
            return list(range(highest + 1, highest + 1 + self.next_count))
        assert self.slots is not None
        return list(self.slots)


@dataclass(frozen=True)
class BuiltFile:
    step: int
    role: str
    path: Path
    key: str
    facts: MediaFacts


@dataclass(frozen=True)
class SlotPlan:
    slot: int
    game: IgdbGame
    shots: ScreenshotPlan
    title_id: int | None  # None: the answer is not in the title list yet
    record: ImportRecord | None  # what a new title is made from
    is_rebuild: bool


def make_files(
    shots: ScreenshotPlan, raw: dict[str, Path], seed: SlotSeed, out_dir: Path
) -> list[BuiltFile]:
    """The six clues' files, in step order: the step-1 crop, the four stills, the clip and its
    poster. Sync and CPU-bound, so the caller runs it in a thread. Each function measures its file
    and refuses one that breaks a limit (AC-39)."""
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    built: list[BuiltFile] = []

    def add(step: int, role: str, path: Path, facts: MediaFacts) -> None:
        built.append(BuiltFile(step, role, path, sha256_file(path), facts))

    path = out_dir / "step1.webp"
    add(
        1,
        ClueRole.SOURCE,
        path,
        media.pixelated_crop(raw[shots.crop.image_id], path, seed.stream(Purpose.CROP)),
    )
    for step, shot in enumerate(shots.steps, start=2):
        path = out_dir / f"step{step}.webp"
        add(step, ClueRole.SOURCE, path, media.still(raw[shot.image_id], path))
    clip_path = out_dir / "step6.mp4"
    sources = [raw[shot.image_id] for shot in shots.clip]
    add(6, ClueRole.SOURCE, clip_path, media.clip(sources, clip_path, seed.stream(Purpose.CLIP)))
    poster_path = out_dir / "step6-poster.webp"
    add(6, ClueRole.POSTER, poster_path, media.poster(clip_path, poster_path))
    return built


def clue_inputs(plan: SlotPlan, files: Sequence[BuiltFile]) -> list[ClueInput]:
    """The clue rows. `source_image_ids` follow the step order: step 1 the crop's screenshot,
    steps 2 to 5 their own, step 6 the clip's screenshots in clip order."""
    hint_of = hints(plan.game)
    url = source_url(plan.game)
    image_ids = {
        1: [plan.shots.crop.image_id],
        **{step: [shot.image_id] for step, shot in enumerate(plan.shots.steps, start=2)},
        6: [shot.image_id for shot in plan.shots.clip],
    }
    clues = []
    for step in range(1, STEPS + 1):
        hint = hint_of[step]
        clues.append(
            ClueInput(
                step=step,
                media_type=MediaType.VIDEO if step == STEPS else MediaType.IMAGE,
                hint_kind=None if hint is None else hint.kind,
                hint_value=None if hint is None else hint.value,
                source_url=url,
                source_image_ids=image_ids[step],
                files=[
                    MediaFileInput(
                        role=file.role,
                        key=file.key,
                        content_type=file.facts.content_type,
                        byte_size=file.facts.byte_size,
                        width=file.facts.width,
                        height=file.facts.height,
                        duration_ms=file.facts.duration_ms,
                    )
                    for file in files
                    if file.step == step
                ],
            )
        )
    return clues


def layout_problem(clues: Sequence[ClueInput]) -> str | None:
    """The media rule for a draft about to be written. The AC-13b part is the shared
    `clue_layout_problem`, the same function the view, scheduling and the buffer check use. On top
    of it a draft has the builder's own shape (AC-29): steps 1 to 5 are images, step 6 is a video,
    the builder makes no WebM (Open point 6) and every poster is a WebP image. `None` when it holds."""
    if [clue.step for clue in clues] != list(range(1, STEPS + 1)):
        return "a draft has exactly the steps 1 to 6"
    for clue in clues:
        expected_type = MediaType.IMAGE if clue.step < STEPS else MediaType.VIDEO
        is_builder_shape = (
            clue.media_type == expected_type
            and all(f.content_type != ClueContentType.VIDEO_WEBM for f in clue.files)
            and all(
                f.content_type == ClueContentType.IMAGE_WEBP
                for f in clue.files
                if f.role == ClueRole.POSTER
            )
        )
        if not is_builder_shape or clue_layout_problem(clue.step, clue.media_type, clue.files):
            return f"step {clue.step} breaks the media rules"
    return None


# What fails one slot, not the run: a screenshot IGDB's image host does not have or sends too large,
# a screenshot that cannot be decoded, a file over a limit. A refused host, a network error and any
# database or storage error stop the run.
SLOT_ERRORS = (
    ImageUnavailableError,
    ResponseTooLargeError,
    MediaLimitError,
    OSError,
    Image.DecompressionBombError,
    FFmpegError,
)


class Builder:
    """One run. The checks come first: the IGDB image gate (R-12, AC-2), then the salt (AC-27), both
    before IGDB is read or anything is fetched. `target` is the target's name; the script has
    already run the target guard (`check_target`).

    No database transaction stays open across the IGDB read, the fetches or the encodes: the reads
    are ended with a rollback (nothing is pending then) before those start. Database errors are
    not classified here but around the session of the run (`database_session`)."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        game_slug: str,
        target: str,
        settings: ContentSettings,
        app_settings: Settings,
        now: dt.datetime,
        games: GamesSource,
        fetch_image: FetchImage,
        writer: MediaWriter | None,
        dry_run: bool = False,
    ) -> None:
        if writer is None and not dry_run:
            raise ValueError("a run that writes needs a media writer")
        self.db = db
        self.game_slug = game_slug
        self.target = target
        self.settings = settings
        self.app_settings = app_settings
        self.today = utc_today(now)
        self.games = games
        self.fetch_image = fetch_image
        self.writer = writer
        self.dry_run = dry_run
        # Set by `run`, which owns the game's rows.
        self.game_id = 0
        self.variant = ""
        self.exclusions: list[ExclusionRow] = []
        self.earlier: set[int] = set()
        # Candidates the slot being built left out as near-duplicates of a stored title (R-20).
        self.near_duplicates = 0
        self.salt = settings.PUZZLE_SEED_SALT

    async def run(self, request: SlotRequest, report: BuildReport | None = None) -> BuildReport:
        """Pass a `report` to keep what a run that stops half way has finished: outcomes are added
        to it slot by slot."""
        report = BuildReport() if report is None else report
        storage = self.writer.kind if self.writer is not None else "local"
        check_image_gate(self.target, self.settings.IGDB_IMAGES_CLEARED_BY, storage)
        self.salt = check_salt(
            self.settings.PUZZLE_SEED_SALT,
            reserved_secrets(self.app_settings, self.settings.IGDB_CLIENT_SECRET),
        )
        game = await get_game_by_slug(self.db, self.game_slug)
        if game is None:
            raise UnknownGameError(f"no game with the slug {self.game_slug!r} in the database")
        self.game_id, self.variant = game.id, game.default_variant
        puzzle_slots = await list_puzzle_slots(self.db, self.game_id, self.variant)
        self.exclusions = await list_exclusions(self.db, self.game_id)
        self.earlier = await list_answer_igdb_ids(self.db, self.game_id)
        await self.db.rollback()  # No transaction stays open through the IGDB read.
        rebuild_slots = {
            row.build_slot
            for row in self.exclusions
            if row.reason == ExclusionReason.REBUILD
            and row.variant == self.variant
            and row.build_slot is not None
        }
        slots = request.pick(puzzle_slots=puzzle_slots, rebuild_slots=rebuild_slots)
        report.slots = slots
        if request.rebuilds:
            report.left_over = len(rebuild_slots - puzzle_slots) - len(slots)
        # A slot that has a puzzle is never touched, whatever its status or games (AC-46, AC-86).
        # When every slot has one, IGDB is not even read.
        todo = [slot for slot in slots if slot not in puzzle_slots]
        for slot in slots:
            if slot in puzzle_slots:
                report.outcomes.append(
                    SlotOutcome(slot, SlotStatus.EXISTS, "the slot already has a puzzle")
                )
        failed = await self._give_up_rejected(todo, report)
        todo = [slot for slot in todo if slot not in failed]
        pool = await self._read_pool() if todo else []
        for slot in todo:
            report.outcomes.append(await self._build_slot(slot, pool))
        return report

    async def _give_up_rejected(self, slots: list[int], report: BuildReport) -> set[int]:
        """A rebuilt draft that the reviewer then rejected as "never" leaves its slot with rebuild
        rows for a title that can no longer be an answer. Such a slot is given up here, as
        `--give-up` does, and is free for a new answer. Each one is recorded in the report the
        moment it is committed (`given_up`), so a run that stops later still shows it. A dry run
        only reports it (`would_give_up`) and plans the slot as free. Returns the slots whose rows
        cannot be given up (they name more than one title): they have a FAILED outcome in the
        report and are not built."""
        never_titles = {
            row.title_id for row in self.exclusions if row.reason == ExclusionReason.NEVER
        }
        failed: set[int] = set()
        for slot in slots:
            rows = [
                row
                for row in self.exclusions
                if row.reason == ExclusionReason.REBUILD
                and (row.variant, row.build_slot) == (self.variant, slot)
            ]
            if not any(row.title_id in never_titles for row in rows):
                continue
            if self.dry_run:
                report.would_give_up.append(slot)
            else:
                try:
                    await give_up_rebuild(
                        self.db, game_id=self.game_id, variant=self.variant, slot=slot
                    )
                    await self.db.commit()
                except ValueError:
                    with contextlib.suppress(Exception):
                        await self.db.rollback()
                    report.outcomes.append(SlotOutcome(slot, SlotStatus.FAILED, "bad_rebuild_row"))
                    failed.add(slot)
                    continue
                report.given_up.append(slot)
            gone = {id(row) for row in rows}
            self.exclusions = [row for row in self.exclusions if id(row) not in gone]
        return failed

    async def _read_pool(self) -> list[IgdbGame]:
        """The games that pass the filters that do not depend on the slot. Read once for the run;
        the exclusions are applied per slot."""
        counter = FunnelCounter(
            today=self.today,
            settings=self.settings,
            earlier_answer_igdb_ids=(),
            never_igdb_ids=(),
        )
        async for game in self.games():
            counter.add(game)
        return list(counter.result().candidates)

    def _seed(self, slot: int) -> SlotSeed:
        assert self.salt is not None
        return SlotSeed(
            self.salt,
            self.game_slug,
            self.variant,
            slot,
            GENERATOR_VERSION,
            other_secrets=reserved_secrets(self.app_settings, self.settings.IGDB_CLIENT_SECRET),
        )

    async def _build_slot(self, slot: int, pool: list[IgdbGame]) -> SlotOutcome:
        self.near_duplicates = 0
        try:
            plan = await self._plan(slot, pool)
            # The planning reads are over: no transaction stays open through the fetches and encodes.
            await self.db.rollback()
            files = await self._make_files(plan)
            # The media rule is checked for a dry run too, before anything is written (AC-13b).
            clues = clue_inputs(plan, files)
            problem = layout_problem(clues)
            if problem is not None:
                raise SlotError("media_layout", problem)
        except (NotBuildableError, SlotError) as error:
            return self._failed(slot, error.reason)
        except SLOT_ERRORS as error:
            kind = "image" if isinstance(error, FetchError) else "media"
            return self._failed(slot, f"{kind}: {type(error).__name__}")
        # The next slot of the run must not draw this answer (AC-25), written or only planned.
        self.earlier.add(plan.game.id)
        outcome = SlotOutcome(
            slot,
            SlotStatus.PLANNED if self.dry_run else SlotStatus.BUILT,
            answer_name=plan.game.name,
            is_rebuild=plan.is_rebuild,
            files=[FilePlan(file.step, file.role, file.facts) for file in files],
            hint_steps=[step for step, hint in hints(plan.game).items() if hint is not None],
            near_duplicates=self.near_duplicates,
        )
        if not self.dry_run:
            await self._write(plan, files, clues)
        return outcome

    def _failed(self, slot: int, detail: str) -> SlotOutcome:
        return SlotOutcome(slot, SlotStatus.FAILED, detail, near_duplicates=self.near_duplicates)

    async def _plan(self, slot: int, pool: list[IgdbGame]) -> SlotPlan:
        seed = self._seed(slot)
        own_rows = [
            row
            for row in self.exclusions
            if row.reason == ExclusionReason.REBUILD
            and (row.variant, row.build_slot) == (self.variant, slot)
        ]
        candidates = funnel(
            pool,
            today=self.today,
            settings=self.settings,
            earlier_answer_igdb_ids=self.earlier,
            never_igdb_ids=never_igdb_ids(self.exclusions),
            reserved_igdb_ids=rebuild_reserved_igdb_ids(
                self.exclusions, own_slot=(self.variant, slot)
            ),
        ).candidates
        # The screenshots of every rejected draft of the slot (decision 71); a row with no image ids
        # raises `bad_rebuild_row`.
        avoid = rebuild_avoid_ids(self.exclusions, variant=self.variant, slot=slot)
        if own_rows:
            game, title_id, record = self._forced(own_rows, candidates)
        else:
            game, title_id, record = await self._draw(candidates, seed)
        # Raises `cannot_rebuild` when fewer than 5 usable screenshots are left.
        shots = assign_screenshots(
            game, seed.stream(Purpose.SCREENSHOTS), avoid, settings=self.settings
        )
        return SlotPlan(slot, game, shots, title_id, record, bool(own_rows))

    def _forced(
        self, rows: list[ExclusionRow], candidates: Sequence[IgdbGame]
    ) -> tuple[IgdbGame, int, None]:
        """A rebuild builds the title its slot was rejected with (R-9)."""
        titles = {(row.title_id, row.igdb_id) for row in rows}
        if len(titles) != 1 or next(iter(titles))[1] is None:
            raise SlotError("bad_rebuild_row", "the slot's rebuild rows do not name one IGDB title")
        title_id, igdb_id = next(iter(titles))
        game = next((candidate for candidate in candidates if candidate.id == igdb_id), None)
        if game is None:
            raise SlotError(
                "rebuild_title_not_a_candidate",
                "IGDB's data no longer makes the slot's title a candidate",
            )
        return game, title_id, None

    async def _draw(
        self, candidates: Sequence[IgdbGame], seed: SlotSeed
    ) -> tuple[IgdbGame, int | None, ImportRecord | None]:
        """Draw an answer. One whose name and year another stored title holds is a near-duplicate
        the import dropped: it is left out and counted, and the draw goes on (R-20)."""
        remaining = list(candidates)
        stream = seed.stream(Purpose.ANSWER)
        while True:
            game = choose_answer(remaining, stream)
            stored = (await get_titles_by_igdb_ids(self.db, [game.id])).get(game.id)
            if stored is not None:
                return game, stored.id, None
            record = to_record(game)
            if isinstance(record, ImportRecord):
                holders = await find_key_holders(
                    self.db, kind=TitleKind.VIDEO_GAME, keys=[record.key]
                )
                if not holders:
                    return game, None, record
            remaining = [candidate for candidate in remaining if candidate.id != game.id]
            self.near_duplicates += 1

    async def _raw_path(self, image_id: str) -> Path:
        """The screenshot in the working folder, fetched once and kept by its IGDB image id."""
        path = self.settings.content_work_path / "raw" / image_id
        if not path.is_file():
            data = await self.fetch_image(image_id)
            await asyncio.to_thread(write_atomically, path, lambda out: out.write(data))
        return path

    async def _make_files(self, plan: SlotPlan) -> list[BuiltFile]:
        raw = {image_id: await self._raw_path(image_id) for image_id in plan.shots.image_ids}
        out_dir = self.settings.content_work_path / "out" / str(plan.slot)
        return await asyncio.to_thread(make_files, plan.shots, raw, self._seed(plan.slot), out_dir)

    async def _put(self, file: BuiltFile) -> None:
        """Store one file. A storage `OSError` is named a `MediaWriteError` here, before it can be
        taken for a database error by the session's wrapper."""
        assert self.writer is not None
        await storage_call(
            "a file cannot be stored",
            self.writer.put,
            file.key,
            file.path,
            file.facts.content_type,
        )

    async def _write(self, plan: SlotPlan, files: list[BuiltFile], clues: list[ClueInput]) -> None:
        """Files first, then the rows in one transaction (AC-46). A failure rolls the rows back and
        stops the run; the files stay."""
        for file in files:
            await self._put(file)
        try:
            title_id = plan.title_id
            if title_id is None:
                assert plan.record is not None
                title = await create_title(
                    self.db,
                    name=plan.record.name,
                    franchise=plan.record.franchise,
                    year=plan.record.year,
                    igdb_id=plan.record.igdb_id,
                )
                title_id = title.id
            await add_built_puzzle(
                self.db,
                game_id=self.game_id,
                variant=self.variant,
                build_slot=plan.slot,
                generator_version=GENERATOR_VERSION,
                answer_title_id=title_id,
                status=PuzzleStatus.IN_REVIEW,
                clues=clues,
            )
            await self.db.commit()
        except BaseException:
            # A failing rollback (the database is gone) never replaces the original error.
            with contextlib.suppress(Exception):
                await self.db.rollback()
            raise


async def give_up_slot(db: AsyncSession, game_slug: str, slot: int) -> int:
    """Give up rebuilding a slot (decision 71): its `rebuild` rows become one `never` exclusion of
    their title, which frees the slot for a new answer. Returns the number of rebuild rows it
    replaced, 0 when there was nothing to give up. A slot that has a puzzle is left alone (AC-86).
    `GiveUpError` when the slot's rows name more than one title."""
    game = await get_game_by_slug(db, game_slug)
    if game is None:
        raise UnknownGameError(f"no game with the slug {game_slug!r} in the database")
    if slot in await list_puzzle_slots(db, game.id, game.default_variant):
        return 0
    try:
        count = await give_up_rebuild(db, game_id=game.id, variant=game.default_variant, slot=slot)
        await db.commit()
    except ValueError as error:
        with contextlib.suppress(Exception):
            await db.rollback()
        raise GiveUpError(str(error)) from error
    except BaseException:
        with contextlib.suppress(Exception):
            await db.rollback()
        raise
    return count
