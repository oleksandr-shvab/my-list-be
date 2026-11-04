import datetime as dt
import logging
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_today
from app.core.exceptions import (
    DuplicateGuessError,
    GameNotFoundError,
    InvalidGuessHistoryError,
    PuzzleAlreadyFinishedError,
    PuzzleNotFoundError,
    TitleNotFoundError,
    VariantNotFoundError,
)
from app.crud.game import get_enabled_game_by_slug, list_enabled_games
from app.crud.puzzle import get_visible_puzzle_with_details
from app.crud.title import get_titles_by_ids
from app.crud.title import search_titles as search_titles_in_db
from app.games.enums import ProgressStatus
from app.games.modules import MODULES
from app.games.modules.base import (
    ClueData,
    GameModule,
    GuessError,
    ProgressState,
    PuzzleData,
    TitleAlreadyGuessedError,
    TitleData,
    TitleKindMismatchError,
)
from app.games.normalize import normalize_text
from app.games.schemas import GameList, GameRead, PuzzleView, TitleList, TitleRead
from app.models.game import Game
from app.models.puzzle import PuzzleClue
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
) -> PuzzleView:
    """The view of one puzzle for a request with no stored progress. The checks run in the order
    game, variant, puzzle, so a hidden puzzle never shows more than `puzzle_not_found` (AC-17)."""
    module, puzzle = await _load_puzzle(
        db, slug=slug, variant=variant, puzzle_date=puzzle_date, now=now
    )
    return module.get_puzzle(puzzle, ProgressState(), is_signed_in=user is not None)


async def submit_guess(
    db: AsyncSession,
    *,
    slug: str,
    variant: str | None,
    puzzle_date: dt.date,
    now: dt.datetime,
    user: User | None,
    title_id: int | None,
    previous: list[int | None],
) -> PuzzleView:
    """Guess on a puzzle, with the earlier guesses sent by the client. Nothing is stored (AC-37):
    the state is rebuilt by replaying `previous` through the module's own `submit_guess`, then the
    new guess is applied. The checks run in the order game, variant, puzzle, history, finished,
    title, then the duplicate rule of the module."""
    module, puzzle = await _load_puzzle(
        db, slug=slug, variant=variant, puzzle_date=puzzle_date, now=now
    )
    # More earlier guesses than the game has guesses before the last one cannot be a real game
    # (AC-39). Checked first, so a long list is never replayed.
    if len(previous) >= module.max_guesses:
        raise InvalidGuessHistoryError()

    ids = [entry for entry in (*previous, title_id) if entry is not None]
    found = await get_titles_by_ids(db, ids=ids, kind=TitleKind(puzzle.title_kind))
    titles = {title_id: _title_data(title) for title_id, title in found.items()}
    state = ProgressState()
    for earlier_id in previous:
        earlier = None if earlier_id is None else titles.get(earlier_id)
        if earlier is None and earlier_id is not None:
            raise InvalidGuessHistoryError()
        try:
            state, _ = module.submit_guess(puzzle, state, earlier)
        except GuessError as error:
            # A duplicate or an entry after the end: a real client never sends it (AC-39).
            raise InvalidGuessHistoryError() from error

    if state.status != ProgressStatus.IN_PROGRESS:
        raise PuzzleAlreadyFinishedError()
    guess = None if title_id is None else titles.get(title_id)
    if guess is None and title_id is not None:
        raise TitleNotFoundError()
    try:
        state, _ = module.submit_guess(puzzle, state, guess)
    except TitleAlreadyGuessedError as error:
        raise DuplicateGuessError() from error
    except TitleKindMismatchError as error:
        # The SQL kind filter in `get_titles_by_ids` normally stops a foreign title first.
        raise TitleNotFoundError() from error

    # The guest keeps its own copy of the first answer (AC-40), so later requests carry `None`.
    counts_for_streak = utc_today(now) == puzzle.date if not previous else None
    state = replace(state, counts_for_streak=counts_for_streak)
    return module.get_puzzle(puzzle, state, is_signed_in=user is not None)


async def _load_puzzle(
    db: AsyncSession,
    *,
    slug: str,
    variant: str | None,
    puzzle_date: dt.date,
    now: dt.datetime,
) -> tuple[GameModule, PuzzleData]:
    """The game's module and the visible puzzle as plain data."""
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
    return module, puzzle_data


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
    )
