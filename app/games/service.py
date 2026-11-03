import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_today
from app.core.exceptions import GameNotFoundError, PuzzleNotFoundError, VariantNotFoundError
from app.crud.game import get_enabled_game_by_slug, list_enabled_games
from app.crud.puzzle import get_visible_puzzle_with_details
from app.games.modules import MODULES
from app.games.modules.base import ClueData, GameModule, ProgressState, PuzzleData, TitleData
from app.games.schemas import GameList, GameRead, PuzzleView
from app.models.game import Game
from app.models.puzzle import PuzzleClue
from app.models.title import Title
from app.models.user import User

logger = logging.getLogger(__name__)


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
        date=puzzle.date,
        # Days since the launch date, plus 1 (Q16).
        number=(puzzle.date - game.launch_date).days + 1,
        answer=_title_data(puzzle.answer_title),
        clues=tuple(_clue_data(clue) for clue in puzzle.clues),
    )
    return module.get_puzzle(puzzle_data, ProgressState(), is_signed_in=user is not None)


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
