from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Response

from app.api.deps import CurrentUser, MediaStorageDep, OptionalUser, SessionDep
from app.core.clock import NowDep, utc_today
from app.core.errors import ProblemDetails
from app.games import service
from app.games.schemas import (
    WHOLE_NUMBER,
    ArchiveList,
    GameList,
    GuessCreate,
    PuzzleView,
    ResumeCreate,
    StatsRead,
    StrictDate,
    TitleList,
)

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

# The guess route's own errors, on top of the 404s and the validation error.
GUESS_ERRORS: dict[int | str, dict[str, Any]] = {
    **PUZZLE_NOT_FOUND,
    409: {
        "model": ProblemDetails,
        "description": "The puzzle is already won or lost (`puzzle_already_finished`)",
    },
    422: {
        "model": ProblemDetails,
        "description": (
            "`validation_error` for a malformed body or date, or a refused guess: "
            "`title_not_found`, `duplicate_guess` or `invalid_guess_history`"
        ),
    },
    503: {
        "model": ProblemDetails,
        "description": (
            "`service_unavailable`: another request kept this game's lock too long. Nothing was "
            "stored, and the same guess can be sent again safely"
        ),
    },
}

# The resume route's own errors: the guess route's, minus what only a new guess can hit.
RESUME_ERRORS: dict[int | str, dict[str, Any]] = {
    **PUZZLE_NOT_FOUND,
    422: {
        "model": ProblemDetails,
        "description": (
            "`validation_error` for a malformed body or date, or `invalid_guess_history` for a "
            "guest's `previous`"
        ),
    },
}

ARCHIVE_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ProblemDetails,
        "description": "`game_not_found` or `variant_not_found`",
    },
    422: {
        "model": ProblemDetails,
        "description": "`validation_error` for a `limit` outside 1 to 100 or a bad `before`",
    },
}

GAME_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetails, "description": "Game not found (`game_not_found`)"}
}

# The stats route needs an account, and checks the game and the variant only.
STATS_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemDetails, "description": "No valid session (`not_authenticated`)"},
    404: {
        "model": ProblemDetails,
        "description": "Game or variant not found (`game_not_found`, `variant_not_found`)",
    },
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
    storage: MediaStorageDep,
    variant: Variant = None,
) -> PuzzleView:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.get_puzzle_view(
        db,
        slug=slug,
        variant=variant,
        puzzle_date=utc_today(now),
        now=now,
        user=user,
        storage=storage,
    )


@router.get("/{slug}/puzzles/{date}", response_model=PuzzleView, responses=PUZZLE_NOT_FOUND)
async def get_puzzle(
    slug: Slug,
    date: Annotated[StrictDate, Path(description="A day as YYYY-MM-DD")],
    db: SessionDep,
    response: Response,
    now: NowDep,
    user: OptionalUser,
    storage: MediaStorageDep,
    variant: Variant = None,
) -> PuzzleView:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.get_puzzle_view(
        db, slug=slug, variant=variant, puzzle_date=date, now=now, user=user, storage=storage
    )


@router.get("/{slug}/archive", response_model=ArchiveList, responses=ARCHIVE_ERRORS)
async def list_archive(
    slug: Slug,
    db: SessionDep,
    response: Response,
    now: NowDep,
    user: OptionalUser,
    variant: Variant = None,
    before: Annotated[
        StrictDate | None,
        Query(
            description="Only days before this one, as YYYY-MM-DD. Defaults to, and is capped at, today"
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Page size"), WHOLE_NUMBER] = 30,
) -> ArchiveList:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.list_archive(
        db, slug=slug, variant=variant, before=before, limit=limit, now=now, user=user
    )


@router.get("/{slug}/titles", response_model=TitleList, responses=GAME_NOT_FOUND)
async def search_titles(
    slug: Slug,
    db: SessionDep,
    response: Response,
    q: Annotated[
        str, Query(max_length=100, description="Text to find in a title's name or franchise")
    ],
) -> TitleList:
    set_cache_control(response, PUBLIC_CACHE)
    return await service.search_titles(db, slug=slug, query=q)


@router.post("/{slug}/puzzles/{date}/guesses", response_model=PuzzleView, responses=GUESS_ERRORS)
async def submit_guess(
    slug: Slug,
    date: Annotated[StrictDate, Path(description="A day as YYYY-MM-DD")],
    body: GuessCreate,
    db: SessionDep,
    response: Response,
    now: NowDep,
    user: OptionalUser,
    storage: MediaStorageDep,
    variant: Variant = None,
) -> PuzzleView:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.submit_guess(
        db,
        slug=slug,
        variant=variant,
        puzzle_date=date,
        now=now,
        user=user,
        storage=storage,
        title_id=body.title_id,
        previous=body.previous,
    )


@router.post("/{slug}/puzzles/{date}/resume", response_model=PuzzleView, responses=RESUME_ERRORS)
async def resume_puzzle(
    slug: Slug,
    date: Annotated[StrictDate, Path(description="A day as YYYY-MM-DD")],
    body: ResumeCreate,
    db: SessionDep,
    response: Response,
    now: NowDep,
    user: OptionalUser,
    storage: MediaStorageDep,
    variant: Variant = None,
) -> PuzzleView:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.resume_puzzle(
        db,
        slug=slug,
        variant=variant,
        puzzle_date=date,
        now=now,
        user=user,
        storage=storage,
        previous=body.previous,
    )


@router.get("/{slug}/stats", response_model=StatsRead, responses=STATS_ERRORS)
async def get_stats(
    slug: Slug,
    db: SessionDep,
    response: Response,
    now: NowDep,
    user: CurrentUser,
    variant: Variant = None,
) -> StatsRead:
    set_cache_control(response, PRIVATE_NO_STORE)
    return await service.get_stats(db, slug=slug, variant=variant, now=now, user=user)
