import datetime as dt
import logging
from dataclasses import replace

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_today
from app.core.db import is_lock_timeout
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
from app.crud.progress import append_guess, create_progress, get_progress, lock_progress
from app.crud.puzzle import get_visible_puzzle_with_details
from app.crud.title import get_titles_by_ids
from app.crud.title import search_titles as search_titles_in_db
from app.games.enums import ProgressStatus
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
from app.games.schemas import GameList, GameRead, PuzzleView, TitleList, TitleRead
from app.media.storage import MediaStorage
from app.models.game import Game
from app.models.progress import Progress
from app.models.puzzle import ClueMediaFile, PuzzleClue
from app.models.title import Title, TitleKind
from app.models.user import User

logger = logging.getLogger(__name__)

MIN_QUERY_LENGTH = 2


async def list_games(db: AsyncSession) -> GameList:
    games = await list_enabled_games(db)
    # Only games with a module, so the list agrees with the puzzle routes (`game_not_found`).
    return GameList(items=[GameRead.model_validate(game) for game in games if game.slug in MODULES])


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
    """The view of one puzzle: the account's stored game when there is one, else an empty one. The
    checks run in the order game, variant, puzzle, so a hidden puzzle never shows more than
    `puzzle_not_found` (AC-17). Nothing is created or changed."""
    module, puzzle, puzzle_id = await _load_puzzle(
        db, slug=slug, variant=variant, puzzle_date=puzzle_date, now=now
    )
    state = await _stored_state(db, module, puzzle, puzzle_id, user)
    return module.get_puzzle(
        puzzle, state, is_signed_in=user is not None, sign=_media_signer(storage, now)
    )


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
    """The view after a reload, with nothing changed (AC-38). An account gets its stored game and
    `previous` is ignored (AC-41). A guest gets `previous` replayed: up to `max_guesses` entries,
    because a whole lost game may be sent here, and a replay that ends the game gives the finished
    view (AC-39)."""
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
    return module.get_puzzle(
        puzzle, state, is_signed_in=user is not None, sign=_media_signer(storage, now)
    )


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
    """Guess on a puzzle. An account's guess is stored (`_submit_account_guess`) and `previous` is
    ignored (AC-41); a guest's is not (`_submit_guest_guess`). Both run the module's own
    `submit_guess`, so the same guesses give the same view (AC-44). The checks run in the order
    game, variant, puzzle, history (guest only), finished, title, then the duplicate rule."""
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
    """Rebuild the state by replaying `previous`, then apply the new guess. Nothing is stored
    (AC-37)."""
    # More earlier guesses than the game has guesses before the last one cannot be a real game
    # (AC-39). Checked first, so a long list is never replayed.
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
    return module.get_puzzle(puzzle, state, is_signed_in=False, sign=_media_signer(storage, now))


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
    """Store the guess on the account's game (AC-36, AC-45). The game row is locked until the
    commit, so two requests for it run one after the other and the guess list never loses or
    repeats a guess. The checks are the guest's, minus the history that does not exist here.

    Everything from the lock to the commit is one unit: the view is built before the commit, and
    any error rolls the transaction back, so a refused guess (or a puzzle that cannot be shown)
    stores nothing and leaves neither the lock nor a new empty game behind. A lock timeout
    anywhere in the unit (the lock, the guess insert, the commit) is 503, and a retry is safe."""
    titles = await _load_titles(db, puzzle, [title_id])
    guess = None if title_id is None else titles.get(title_id)
    is_unknown_title = title_id is not None and guess is None
    try:
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
                # The server's clock at this first guess against the puzzle's date (AC-36).
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
        view = module.get_puzzle(puzzle, state, is_signed_in=True, sign=_media_signer(storage, now))
        await db.commit()
    except DBAPIError as error:
        await _rollback_keeping_the_error(db)
        if is_lock_timeout(error):
            raise ServiceUnavailableError() from error
        raise
    except Exception:
        await _rollback_keeping_the_error(db)
        raise
    return view


async def _rollback_keeping_the_error(db: AsyncSession) -> None:
    """Roll back, and never let a failing rollback replace the error being handled: the caller
    re-raises the original. The connection is broken then, and closing the session discards it."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("Rollback failed after an error in an account guess", exc_info=True)


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
    """The titles of the puzzle's kind among `ids` (skips ignored), in one query. An id that is
    missing, of another kind or too large for a key is not in the result."""
    found = await get_titles_by_ids(
        db,
        ids=[entry for entry in ids if entry is not None],
        kind=TitleKind(puzzle.title_kind),
    )
    return {found_id: _title_data(title) for found_id, title in found.items()}


def _replay_previous(
    module: GameModule,
    puzzle: PuzzleData,
    previous: list[int | None],
    titles: dict[int, TitleData],
) -> ProgressState:
    """The state a guest's `previous` leads to. Any entry a real client never sends (an unknown
    title, a duplicate, an entry after the end) is `invalid_guess_history` (AC-39)."""
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
    """The account's stored state; an empty one for a guest or an account with no row."""
    if user is None:
        return ProgressState()
    progress = await get_progress(db, user_id=user.id, puzzle_id=puzzle_id)
    if progress is None:
        return ProgressState()
    return _state_from_progress(module, puzzle, progress)


def _state_from_progress(
    module: GameModule, puzzle: PuzzleData, progress: Progress
) -> ProgressState:
    """Rebuild the state from the stored guesses through the module's own rules, so the results,
    the status and the earned steps come from one place (AC-44). The stored `counts_for_streak`
    is the one the first guess set (AC-36)."""
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
    """The signer a game module calls for the files of the steps it shows (R-6)."""

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
    """The game's module, the visible puzzle as plain data, and its id (which never leaves the
    server, AC-5)."""
    game, module = await _resolve_game(db, slug)
    variant = variant if variant is not None else game.default_variant
    if not module.has_variant(variant, default_variant=game.default_variant):
        raise VariantNotFoundError()

    puzzle = await get_visible_puzzle_with_details(
        db,
        game_id=game.id,
        variant=variant,
        puzzle_date=puzzle_date,
        today=utc_today(now),
    )
    if puzzle is None:
        raise PuzzleNotFoundError()

    puzzle_data = PuzzleData(
        slug=game.slug,
        variant=puzzle.variant,
        title_kind=game.title_kind,
        date=puzzle.date,
        # Days since the launch date, plus 1 (Q16).
        number=(puzzle.date - game.launch_date).days + 1,
        answer=_title_data(puzzle.answer_title),
        clues=tuple(_clue_data(clue) for clue in puzzle.clues),
    )
    return module, puzzle_data, puzzle.id


def search_query(query: str) -> str | None:
    """The normalized text to search for, or None when the search must return an empty list: a
    trimmed `query` under `MIN_QUERY_LENGTH` characters, or a normalized text under it ("a.", "a-"
    and a decomposed "é" all become one letter), which would match most of the catalogue (AC-20).
    The one place that rule lives; the benchmark uses it too."""
    trimmed = query.strip()
    normalized = normalize_text(trimmed)
    if len(trimmed) < MIN_QUERY_LENGTH or len(normalized) < MIN_QUERY_LENGTH:
        return None
    return normalized


async def search_titles(db: AsyncSession, *, slug: str, query: str) -> TitleList:
    """Titles of the game's kind that fuzzy-match `query` on the name or the franchise. The game
    is checked first, so an unknown slug is a 404 whatever `q` is."""
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
