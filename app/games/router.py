from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response

from app.api.deps import OptionalUser, SessionDep
from app.core.clock import NowDep, utc_today
from app.core.errors import ProblemDetails
from app.games import service
from app.games.schemas import GameList, PuzzleView, StrictDate

router = APIRouter()

# `public` only for data that is the same for everyone. Every puzzle response depends on the
# caller and on the time of day, and carries clues that are earned, so it is never stored (AC-77).
PUBLIC_CACHE = "public, max-age=300"
PRIVATE_NO_STORE = "private, no-store"

# `game_not_found`, `variant_not_found` or `puzzle_not_found`. A hidden puzzle gives the same body
# as a missing one (AC-17).
PUZZLE_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ProblemDetails,
        "description": "Game, variant or puzzle not found (or the puzzle is not visible yet)",
    }
}

Slug = Annotated[str, Path(description="The game's slug, for example `game-guesser`")]
Variant = Annotated[str | None, Query(description="Defaults to the game's default variant")]


def set_cache_control(response: Response, value: str) -> None:
    response.headers["Cache-Control"] = value


@router.get("", response_model=GameList)
async def list_games(db: SessionDep, response: Response) -> GameList:
    set_cache_control(response, PUBLIC_CACHE)
    return await service.list_games(db)


# Declared before the dated route, or "today" would be read as a date and fail with 422.
@router.get("/{slug}/puzzles/today", response_model=PuzzleView, responses=PUZZLE_NOT_FOUND)
async def get_today_puzzle(
    slug: Slug,
    db: SessionDep,
    response: Response,
    now: NowDep,
    user: OptionalUser,
    variant: Variant = None,
) -> PuzzleView:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.get_puzzle_view(
        db, slug=slug, variant=variant, puzzle_date=utc_today(now), now=now, user=user
    )


@router.get("/{slug}/puzzles/{date}", response_model=PuzzleView, responses=PUZZLE_NOT_FOUND)
async def get_puzzle(
    slug: Slug,
    date: Annotated[StrictDate, Path(description="A day as YYYY-MM-DD")],
    db: SessionDep,
    response: Response,
    now: NowDep,
    user: OptionalUser,
    variant: Variant = None,
) -> PuzzleView:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.get_puzzle_view(
        db, slug=slug, variant=variant, puzzle_date=date, now=now, user=user
    )
