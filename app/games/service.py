import datetime as dt
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_today
from app.core.db import is_lock_failure
from app.core.exceptions import (
    DuplicateGuessError,
    GameNotFoundError,
    InvalidGuessHistoryError,
    PuzzleAlreadyFinishedError,
    PuzzleDataError,
    PuzzleNotFoundError,
    ServiceUnavailableError,
    TitleNotFoundError,
    VariantNotFoundError,
)
from app.crud.game import get_enabled_game_by_slug, list_enabled_games
from app.crud.progress import (
    append_guess,
    create_progress,
    get_progress,
    insert_merged_progress,
    limit_lock_wait,
    list_finished_plays,
    list_owned_puzzle_ids,
    lock_progress,
)
from app.crud.puzzle import (
    ArchiveRow,
    get_visible_puzzle_with_details,
    list_archive_puzzles,
    list_visible_puzzles,
)
from app.crud.title import get_titles_by_ids
from app.crud.title import search_titles as search_titles_in_db
from app.games import daily_proof
from app.games.enums import ArchiveState, MergeRejectionCode, ProgressStatus
from app.games.modules import MODULES
from app.games.modules.base import (
    ClueData,
    GameModule,
    GuessError,
    MediaFileData,
    MediaSigner,
    ProgressState,
    PuzzleData,
    SignedLink,
    TitleAlreadyGuessedError,
    TitleData,
    TitleKindMismatchError,
)
from app.games.normalize import normalize_text
from app.games.schemas import (
    ArchiveItem,
    ArchiveList,
    GameList,
    GameRead,
    MergeEntry,
    MergeRead,
    PuzzleView,
    RejectedEntryRead,
    StatsRead,
    TitleList,
    TitleRead,
)
from app.games.stats import Play, compute_stats
from app.media.storage import MediaStorage
from app.models.game import Game
from app.models.progress import Progress
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue
from app.models.title import Title, TitleKind
from app.models.user import User

logger = logging.getLogger(__name__)

MIN_QUERY_LENGTH = 2


async def list_games(db: AsyncSession) -> GameList:
    games = await list_enabled_games(db)
    # Only games with a module, so the list agrees with the puzzle routes (`game_not_found`).
    return GameList(items=[GameRead.model_validate(game) for game in games if game.slug in MODULES])


async def list_archive(
    db: AsyncSession,
    *,
    slug: str,
    variant: str | None,
    before: dt.date | None,
    limit: int,
    now: dt.datetime,
    user: User | None,
) -> ArchiveList:
    """`before` is clamped to today, so neither today nor a future day is listed. A missing day is
    left out, with no `puzzle_not_found` (AC-63 to AC-65)."""
    game, module = await _resolve_game(db, slug)
    variant = _resolve_variant(game, module, variant)

    today = utc_today(now)
    rows = await list_archive_puzzles(
        db,
        game_id=game.id,
        variant=variant,
        before=today if before is None else min(before, today),
        today=today,
        # One more than a page, to know whether there is a next one.
        limit=limit + 1,
        user_id=None if user is None else user.id,
    )
    page = rows[:limit]
    return ArchiveList(
        items=[_archive_item(row, game, is_signed_in=user is not None) for row in page],
        next_before=page[-1].date if len(rows) > limit else None,
    )


def _puzzle_number(puzzle_date: dt.date, game: Game) -> int:
    """Day 1 is the launch date (decision Q16)."""
    return (puzzle_date - game.launch_date).days + 1


def _archive_item(row: ArchiveRow, game: Game, *, is_signed_in: bool) -> ArchiveItem:
    state = None
    guesses_used = None
    if is_signed_in:
        # No progress row means the puzzle was never started.
        state = ArchiveState.NOT_PLAYED if row.status is None else ArchiveState(row.status)
        guesses_used = row.guesses_used
    return ArchiveItem(
        date=row.date,
        number=_puzzle_number(row.date, game),
        state=state,
        guesses_used=guesses_used,
    )


async def get_puzzle_view(
    db: AsyncSession,
    *,
    slug: str,
    variant: str | None,
    puzzle_date: dt.date,
    now: dt.datetime,
    user: User | None,
    storage: MediaStorage,
) -> PuzzleView:
    """Creates nothing. A hidden puzzle never shows more than `puzzle_not_found` (AC-17)."""
    module, puzzle, puzzle_id = await _load_puzzle(
        db, slug=slug, variant=variant, puzzle_date=puzzle_date, now=now
    )
    state = await _stored_state(db, module, puzzle, puzzle_id, user)
    return _view(module, puzzle, state, is_signed_in=user is not None, now=now, storage=storage)


async def resume_puzzle(
    db: AsyncSession,
    *,
    slug: str,
    variant: str | None,
    puzzle_date: dt.date,
    now: dt.datetime,
    user: User | None,
    storage: MediaStorage,
    previous: list[int | None],
) -> PuzzleView:
    """Changes nothing (AC-38). `previous` is ignored for an account (AC-41). For a guest it may
    hold up to `max_guesses` entries, because a whole lost game can be sent here (AC-39)."""
    module, puzzle, puzzle_id = await _load_puzzle(
        db, slug=slug, variant=variant, puzzle_date=puzzle_date, now=now
    )
    if user is not None:
        state = await _stored_state(db, module, puzzle, puzzle_id, user)
    else:
        if len(previous) > module.max_guesses:
            raise InvalidGuessHistoryError()
        titles = await _load_titles(db, puzzle, previous)
        state = _replay_previous(module, puzzle, previous, titles)
    return _view(module, puzzle, state, is_signed_in=user is not None, now=now, storage=storage)


async def submit_guess(
    db: AsyncSession,
    *,
    slug: str,
    variant: str | None,
    puzzle_date: dt.date,
    now: dt.datetime,
    user: User | None,
    storage: MediaStorage,
    title_id: int | None,
    previous: list[int | None],
) -> PuzzleView:
    """Both flows run the module's own `submit_guess`, so the same guesses give the same view
    (AC-44). `previous` is ignored for an account (AC-41)."""
    module, puzzle, puzzle_id = await _load_puzzle(
        db, slug=slug, variant=variant, puzzle_date=puzzle_date, now=now
    )
    if user is not None:
        return await _submit_account_guess(
            db,
            module=module,
            puzzle=puzzle,
            puzzle_id=puzzle_id,
            user=user,
            now=now,
            storage=storage,
            title_id=title_id,
        )
    return await _submit_guest_guess(
        db,
        module=module,
        puzzle=puzzle,
        now=now,
        storage=storage,
        title_id=title_id,
        previous=previous,
    )


async def _submit_guest_guess(
    db: AsyncSession,
    *,
    module: GameModule,
    puzzle: PuzzleData,
    now: dt.datetime,
    storage: MediaStorage,
    title_id: int | None,
    previous: list[int | None],
) -> PuzzleView:
    """Stores nothing (AC-37)."""
    # A real game has at most `max_guesses - 1` earlier guesses (AC-39). Checked first, so a long
    # list is never replayed.
    if len(previous) >= module.max_guesses:
        raise InvalidGuessHistoryError()

    titles = await _load_titles(db, puzzle, [*previous, title_id])
    state = _replay_previous(module, puzzle, previous, titles)
    if state.status != ProgressStatus.IN_PROGRESS:
        raise PuzzleAlreadyFinishedError()
    guess = None if title_id is None else titles.get(title_id)
    if guess is None and title_id is not None:
        raise TitleNotFoundError()
    state = _apply_guess(module, puzzle, state, guess)

    # The guest keeps its own copy of the first answer (AC-40), so later requests carry `None`.
    counts_for_streak = utc_today(now) == puzzle.date if not previous else None
    state = replace(state, counts_for_streak=counts_for_streak)
    return _view(module, puzzle, state, is_signed_in=False, now=now, storage=storage)


async def _submit_account_guess(
    db: AsyncSession,
    *,
    module: GameModule,
    puzzle: PuzzleData,
    puzzle_id: int,
    user: User,
    now: dt.datetime,
    storage: MediaStorage,
    title_id: int | None,
) -> PuzzleView:
    """The game row is locked until the commit, so two requests for it run one after the other
    (AC-45). The view is built before the commit and any error rolls back, so a refused guess or a
    puzzle that cannot be shown stores nothing and leaves no lock or empty game behind."""
    titles = await _load_titles(db, puzzle, [title_id])
    guess = None if title_id is None else titles.get(title_id)
    is_unknown_title = title_id is not None and guess is None
    async with _locked_unit(db, operation="account guess"):
        progress = await lock_progress(db, user_id=user.id, puzzle_id=puzzle_id)
        if progress is None:
            if is_unknown_title:
                # A game with no row cannot be finished, so nothing outranks this error, and no
                # row is created for a guess that will be refused.
                raise TitleNotFoundError()
            progress = await create_progress(
                db,
                user_id=user.id,
                puzzle_id=puzzle_id,
                now=now,
                # The first guess decides it (AC-36).
                counts_for_streak=utc_today(now) == puzzle.date,
            )
        state = _state_from_progress(module, puzzle, progress)
        # The one place that decides "finished": the state rebuilt from the stored guesses.
        if state.status != ProgressStatus.IN_PROGRESS:
            raise PuzzleAlreadyFinishedError()
        if is_unknown_title:
            raise TitleNotFoundError()
        state = _apply_guess(module, puzzle, state, guess)
        await append_guess(db, progress, title_id=title_id, status=state.status, now=now)
        view = _view(module, puzzle, state, is_signed_in=True, now=now, storage=storage)
    return view


@asynccontextmanager
async def _locked_unit(db: AsyncSession, *, operation: str) -> AsyncIterator[None]:
    """Limits the lock wait, commits at the end, and on any error rolls back and re-raises it. A
    lock timeout or deadlock is 503 with nothing stored. After a rollback the session's ORM
    objects are expired, so nothing reads them again in the request. `operation` names the unit in
    the log."""
    try:
        await limit_lock_wait(db)
        yield
        await db.commit()
    except DBAPIError as error:
        await _rollback_keeping_the_error(db, operation)
        if is_lock_failure(error):
            raise ServiceUnavailableError() from error
        raise
    except Exception:
        await _rollback_keeping_the_error(db, operation)
        raise


async def _rollback_keeping_the_error(db: AsyncSession, operation: str) -> None:
    """A failing rollback must not replace the error being handled; the broken connection is
    discarded when the session closes."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("Rollback failed after an error in %s", operation, exc_info=True)


def _view(
    module: GameModule,
    puzzle: PuzzleData,
    state: ProgressState,
    *,
    is_signed_in: bool,
    now: dt.datetime,
    storage: MediaStorage,
) -> PuzzleView:
    """Adds the daily proof for a guest on the puzzle's own day (Q2); the module knows nothing of
    proofs or the clock."""
    view = module.get_puzzle(
        puzzle, state, is_signed_in=is_signed_in, sign=_media_signer(storage, now)
    )
    if is_signed_in or utc_today(now) != puzzle.date:
        return view
    view.daily_proof = daily_proof.sign(puzzle.slug, puzzle.variant, puzzle.date)
    return view


def _apply_guess(
    module: GameModule, puzzle: PuzzleData, state: ProgressState, guess: TitleData | None
) -> ProgressState:
    try:
        new_state, _ = module.submit_guess(puzzle, state, guess)
    except TitleAlreadyGuessedError as error:
        raise DuplicateGuessError() from error
    except TitleKindMismatchError as error:
        # The SQL kind filter in `get_titles_by_ids` normally stops a foreign title first.
        raise TitleNotFoundError() from error
    return new_state


async def _load_titles(
    db: AsyncSession, puzzle: PuzzleData, ids: list[int | None]
) -> dict[int, TitleData]:
    return await _load_titles_of_kind(db, TitleKind(puzzle.title_kind), ids)


async def _load_titles_of_kind(
    db: AsyncSession, kind: TitleKind, ids: Iterable[int | None]
) -> dict[int, TitleData]:
    found = await get_titles_by_ids(
        db, ids=[entry for entry in ids if entry is not None], kind=kind
    )
    return {found_id: _title_data(title) for found_id, title in found.items()}


def _replay_previous(
    module: GameModule,
    puzzle: PuzzleData,
    previous: list[int | None],
    titles: dict[int, TitleData],
) -> ProgressState:
    """An entry a real client never sends (unknown title, duplicate, entry after the end) is
    `invalid_guess_history` (AC-39)."""
    state = ProgressState()
    for earlier_id in previous:
        earlier = None if earlier_id is None else titles.get(earlier_id)
        if earlier is None and earlier_id is not None:
            raise InvalidGuessHistoryError()
        try:
            state, _ = module.submit_guess(puzzle, state, earlier)
        except GuessError as error:
            raise InvalidGuessHistoryError() from error
    return state


async def _stored_state(
    db: AsyncSession, module: GameModule, puzzle: PuzzleData, puzzle_id: int, user: User | None
) -> ProgressState:
    if user is None:
        return ProgressState()
    progress = await get_progress(db, user_id=user.id, puzzle_id=puzzle_id)
    if progress is None:
        return ProgressState()
    return _state_from_progress(module, puzzle, progress)


def _state_from_progress(
    module: GameModule, puzzle: PuzzleData, progress: Progress
) -> ProgressState:
    """Replays through the module's own rules, so results, status and earned steps come from one
    place (AC-44)."""
    state = ProgressState(counts_for_streak=progress.counts_for_streak)
    for stored in progress.guesses:
        title = None if stored.title is None else _title_data(stored.title)
        try:
            state, _ = module.submit_guess(puzzle, state, title)
        except GuessError as error:
            # Only this code writes the guesses, so a stored game that breaks the rules is a bug.
            raise PuzzleDataError(
                f"Stored progress {progress.id} cannot be replayed: {type(error).__name__}"
            ) from error
    return state


def _media_signer(storage: MediaStorage, now: dt.datetime) -> MediaSigner:
    def sign(key: str, content_type: str) -> SignedLink:
        url, expires_at = storage.signed_url(key, content_type, now)
        return SignedLink(url=url, expires_at=expires_at)

    return sign


async def _load_puzzle(
    db: AsyncSession,
    *,
    slug: str,
    variant: str | None,
    puzzle_date: dt.date,
    now: dt.datetime,
) -> tuple[GameModule, PuzzleData, int]:
    """The puzzle id never leaves the server (AC-5)."""
    game, module = await _resolve_game(db, slug)
    variant = _resolve_variant(game, module, variant)
    puzzle_data, puzzle_id = await _find_puzzle(
        db, game, variant=variant, puzzle_date=puzzle_date, now=now
    )
    return module, puzzle_data, puzzle_id


async def _find_puzzle(
    db: AsyncSession, game: Game, *, variant: str, puzzle_date: dt.date, now: dt.datetime
) -> tuple[PuzzleData, int]:
    puzzle = await get_visible_puzzle_with_details(
        db,
        game_id=game.id,
        variant=variant,
        puzzle_date=puzzle_date,
        today=utc_today(now),
    )
    if puzzle is None:
        raise PuzzleNotFoundError()

    clues = tuple(_clue_data(clue) for clue in puzzle.clues)
    return _puzzle_data(game, puzzle, clues=clues), puzzle.id


def _puzzle_data(game: Game, puzzle: Puzzle, *, clues: tuple[ClueData, ...]) -> PuzzleData:
    # A visible puzzle is scheduled, and a scheduled puzzle has a date (`ck_puzzles_scheduled_has_date`).
    if puzzle.date is None:
        raise PuzzleDataError(f"puzzle {puzzle.id} has no date")
    return PuzzleData(
        slug=game.slug,
        variant=puzzle.variant,
        title_kind=game.title_kind,
        date=puzzle.date,
        number=_puzzle_number(puzzle.date, game),
        answer=_title_data(puzzle.answer_title),
        clues=clues,
    )


@dataclass(frozen=True)
class _ReadyEntry:
    entry: MergeEntry
    puzzle_id: int
    status: ProgressStatus


async def merge_progress(
    db: AsyncSession,
    *,
    slug: str,
    now: dt.datetime,
    user: User,
    entries: list[MergeEntry],
) -> MergeRead:
    """The status comes from replaying the guesses through the module, never from the client
    (AC-56). A bad entry is skipped and reported, and the others still merge (AC-59). A game the
    account already has is never changed (AC-58), so a repeat changes nothing (AC-61).

    Per-entry checks, in order: guess count and variant (no database yet); the visible puzzle;
    whether the account already has a game on it, in which case the entry is `already_present`
    and never replayed, valid or not, since a replay would tell a client the answer with no guess
    used (AC-58); the replay. `played_as_daily` counts for the streak only with the puzzle's
    `daily_proof` (Q2); without one the game is stored as an archive play and counted in
    `daily_unproven`.

    Entries are stored sorted by `(variant, date)`, so two overlapping merges take their row locks
    in the same order and cannot deadlock. All entries share one transaction: an error stores
    nothing, and a lock timeout or deadlock is 503, so the request can be sent again."""
    game, module = await _resolve_game(db, slug)
    rejections: dict[int, MergeRejectionCode] = {}
    candidates: list[int] = []
    for index, entry in enumerate(entries):
        # No guesses leave nothing to store (R-22); more than a whole lost game is not a game
        # (AC-39). Checked before anything touches the database.
        if not 1 <= len(entry.guesses) <= module.max_guesses:
            rejections[index] = MergeRejectionCode.INVALID_GUESS_HISTORY
        elif not module.has_variant(entry.variant, default_variant=game.default_variant):
            rejections[index] = MergeRejectionCode.VARIANT_NOT_FOUND
        else:
            candidates.append(index)

    already_present = 0
    stored = 0
    daily_unproven = 0
    async with _locked_unit(db, operation="merge"):
        puzzles = {
            (puzzle.variant, puzzle.date): puzzle
            for puzzle in await list_visible_puzzles(
                db,
                game_id=game.id,
                keys={(entries[index].variant, entries[index].date) for index in candidates},
                today=utc_today(now),
            )
        }
        owned = await list_owned_puzzle_ids(
            db, user_id=user.id, puzzle_ids=[puzzle.id for puzzle in puzzles.values()]
        )
        replayable: list[int] = []
        for index in candidates:
            puzzle = puzzles.get((entries[index].variant, entries[index].date))
            if puzzle is None:
                rejections[index] = MergeRejectionCode.PUZZLE_NOT_FOUND
            elif puzzle.id in owned:
                already_present += 1
            else:
                replayable.append(index)

        # An id that is not a title of the game's kind is not in the result, so an entry that
        # uses it is invalid.
        titles = await _load_titles_of_kind(
            db,
            TitleKind(game.title_kind),
            (guess for index in replayable for guess in entries[index].guesses),
        )
        ready: list[_ReadyEntry] = []
        failed_replay: list[int] = []
        for index in replayable:
            entry = entries[index]
            puzzle = puzzles[(entry.variant, entry.date)]
            try:
                state = _replay_previous(
                    module, _puzzle_data(game, puzzle, clues=()), entry.guesses, titles
                )
            except InvalidGuessHistoryError:
                rejections[index] = MergeRejectionCode.INVALID_GUESS_HISTORY
                failed_replay.append(index)
                continue
            ready.append(_ReadyEntry(entry=entry, puzzle_id=puzzle.id, status=state.status))

        # A game begun after the owned read above (a guess in another tab) makes a failed replay
        # `already_present`, not a rejection (AC-58). Read before this request stores anything, so
        # its own entries are never mistaken for the account's.
        owned_now = await list_owned_puzzle_ids(
            db,
            user_id=user.id,
            puzzle_ids=[
                puzzles[(entries[index].variant, entries[index].date)].id for index in failed_replay
            ],
        )
        for index in failed_replay:
            if puzzles[(entries[index].variant, entries[index].date)].id in owned_now:
                del rejections[index]
                already_present += 1

        # A stable sort: of two entries for one puzzle the first one is stored.
        ready.sort(key=lambda item: (item.entry.variant, item.entry.date))
        for item in ready:
            entry = item.entry
            is_daily = entry.played_as_daily and daily_proof.verify(
                entry.daily_proof, game.slug, entry.variant, entry.date
            )
            is_stored = await insert_merged_progress(
                db,
                user_id=user.id,
                puzzle_id=item.puzzle_id,
                status=item.status,
                now=now,
                counts_for_streak=is_daily,
                title_ids=entry.guesses,
            )
            if not is_stored:
                already_present += 1
                continue
            stored += 1
            if entry.played_as_daily and not is_daily:
                daily_unproven += 1

    return MergeRead(
        stored=stored,
        already_present=already_present,
        rejected=len(rejections),
        daily_unproven=daily_unproven,
        rejected_entries=[
            RejectedEntryRead(date=entries[index].date, variant=entries[index].variant, code=code)
            for index, code in sorted(rejections.items())
        ],
    )


async def get_stats(
    db: AsyncSession, *, slug: str, variant: str | None, now: dt.datetime, user: User
) -> StatsRead:
    """Computed from the stored games on every read (AC-53). The streak fields are None for a
    variant other than the default (AC-51)."""
    game, module = await _resolve_game(db, slug)
    variant = _resolve_variant(game, module, variant)
    today = utc_today(now)
    rows = await list_finished_plays(
        db, user_id=user.id, game_id=game.id, variant=variant, today=today
    )
    plays = [
        Play(
            variant=row.variant,
            puzzle_date=row.puzzle_date,
            status=row.status,
            guesses_used=row.guesses_used,
            counts_for_streak=row.counts_for_streak,
        )
        for row in rows
    ]
    stats = compute_stats(
        plays,
        today=today,
        max_guesses=module.max_guesses,
        variant=variant,
        default_variant=game.default_variant,
    )
    # A puzzle date is one row of this account, game and variant, so it finds the progress row.
    progress_ids = {row.puzzle_date: row.progress_id for row in rows}
    for play in stats.unbucketed_wins:
        # A warning, not an error: the same row would log on every read.
        logger.warning(
            "A won play has %s guesses, so it is in no distribution bucket "
            "(progress %s, user %s, puzzle %s)",
            play.guesses_used,
            progress_ids[play.puzzle_date],
            user.id,
            play.puzzle_date,
        )
    return StatsRead.model_validate(stats, from_attributes=True)


def search_query(query: str) -> str | None:
    """None means an empty list: a query under `MIN_QUERY_LENGTH` characters, before or after
    normalizing ("a.", "a-" and a decomposed "é" become one letter), would match most of the
    catalogue (AC-20). The benchmark uses this too."""
    trimmed = query.strip()
    normalized = normalize_text(trimmed)
    if len(trimmed) < MIN_QUERY_LENGTH or len(normalized) < MIN_QUERY_LENGTH:
        return None
    return normalized


async def search_titles(db: AsyncSession, *, slug: str, query: str) -> TitleList:
    """The game is checked first, so an unknown slug is a 404 whatever `q` is."""
    game, _ = await _resolve_game(db, slug)
    normalized = search_query(query)
    if normalized is None:
        return TitleList(items=[])
    titles = await search_titles_in_db(db, kind=game.title_kind, normalized_query=normalized)
    return TitleList(items=[TitleRead.model_validate(title) for title in titles])


async def _resolve_game(db: AsyncSession, slug: str) -> tuple[Game, GameModule]:
    game = await get_enabled_game_by_slug(db, slug)
    if game is None:
        raise GameNotFoundError()
    module = MODULES.get(game.slug)
    if module is None:
        # Debug, not a warning: a game row with no module would otherwise log on every request.
        logger.debug("Game %r has no module, so it is treated as not found", game.slug)
        raise GameNotFoundError()
    return game, module


def _resolve_variant(game: Game, module: GameModule, variant: str | None) -> str:
    variant = variant if variant is not None else game.default_variant
    if not module.has_variant(variant, default_variant=game.default_variant):
        raise VariantNotFoundError()
    return variant


def _title_data(title: Title) -> TitleData:
    return TitleData(
        id=title.id,
        kind=title.kind,
        name=title.name,
        normalized_name=title.normalized_name,
        franchise=title.franchise,
        normalized_franchise=title.normalized_franchise,
        year=title.year,
    )


def _clue_data(clue: PuzzleClue) -> ClueData:
    return ClueData(
        step=clue.step,
        hint_kind=clue.hint_kind,
        hint_value=clue.hint_value,
        source_url=clue.source_url,
        media_type=clue.media_type,
        media_files=tuple(_media_file_data(file) for file in clue.media_files),
    )


def _media_file_data(file: ClueMediaFile) -> MediaFileData:
    return MediaFileData(
        role=file.role,
        key=file.key,
        content_type=file.content_type,
        width=file.width,
        height=file.height,
    )
