import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt
from pydantic_core import PydanticCustomError

from app.games.enums import ArchiveState, GuessResult, MediaType, MergeRejectionCode, ProgressStatus

DATE_FORMAT = "YYYY-MM-DD"


DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def _strict_date(value: Any) -> dt.date:
    """Only a real `YYYY-MM-DD` day. Pydantic's lax `date` also reads an integer as a Unix
    timestamp and `date.fromisoformat` accepts `20261102`, so the shape is checked first (R-13)."""
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError(f"Expected a date as {DATE_FORMAT}")
    return dt.date.fromisoformat(value)


StrictDate = Annotated[dt.date, BeforeValidator(_strict_date)]

WHOLE_NUMBER_PATTERN = re.compile(r"-?\d+", re.ASCII)


def _whole_number(value: Any) -> Any:
    """A query integer written as plain digits (a leading minus is allowed, so `-1` is `too_small`,
    not `invalid_type`). Pydantic's lax `int` also reads `1_0`, `+5`, ` 7 `, `100.0` and `1e2`. The
    error type is `int_type`, so the answer is `invalid_type` like any other non-integer. A string
    of more than 4300 digits passes here and fails in pydantic as `int_parsing_size`."""
    if isinstance(value, str) and not WHOLE_NUMBER_PATTERN.fullmatch(value):
        raise PydanticCustomError("int_type", "Expected a whole number")
    return value


# Metadata for an `int` query parameter, placed after `Query(ge=..., le=...)`: the validator then
# wraps the whole `int`, bounds included, and the schema keeps `minimum` and `maximum`.
WHOLE_NUMBER = BeforeValidator(_whole_number)


class GameRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    default_variant: str
    launch_date: dt.date


class GameList(BaseModel):
    items: list[GameRead]


class TitleRead(BaseModel):
    """One search result. It has no field that says whether the title is an answer (AC-19)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    franchise: str | None
    year: int | None


class TitleList(BaseModel):
    items: list[TitleRead]


class ArchiveItem(BaseModel):
    """One past day of the archive. `state` and `guesses_used` are null for a guest, whose
    progress lives in the browser (AC-63). No answer and no internal id (AC-5, AC-65)."""

    date: dt.date
    number: int = Field(description="Days since the game's launch date, plus 1")
    state: ArchiveState | None = Field(description="Null for a guest")
    guesses_used: int | None = Field(description="Skips included; null for a guest")


class ArchiveList(BaseModel):
    items: list[ArchiveItem]
    next_before: dt.date | None = Field(
        description="The last item's date: send it as `before` for the next page. Null at the end"
    )


# A size guard for the request list, apart from the game rule of AC-39 (5 earlier guesses on a
# guess): the rule answers `invalid_guess_history`, this answers `validation_error` (R-24).
MAX_PREVIOUS_ENTRIES = 20


# The earlier guesses of a guest, in order (null is a skip).
Previous = Annotated[
    list[StrictInt | None],
    Field(
        max_length=MAX_PREVIOUS_ENTRIES,
        description="The earlier guesses in this game, in order (null is a skip)",
    ),
]


class GuessCreate(BaseModel):
    title_id: StrictInt | None = Field(description="The guessed title, or null to skip")
    previous: Previous


class ResumeCreate(BaseModel):
    previous: Previous


# At most this many entries in one merge request (AC-62). A larger history is sent in several
# requests, which is safe because a merge is idempotent (AC-61).
MAX_MERGE_ENTRIES = 500

# The length of `puzzles.variant`: a longer name cannot exist, and the cap keeps the rejected list
# (which repeats each variant) small.
MAX_VARIANT_LENGTH = 32

# A proof is 43 characters (an unpadded SHA-256 in base64url); the cap only keeps a body small.
MAX_DAILY_PROOF_LENGTH = 100


class MergeEntry(BaseModel):
    """One finished or running game a guest played in this browser. It has no outcome: the server
    replays `guesses` and decides (AC-56)."""

    date: StrictDate = Field(description="The puzzle's day, as YYYY-MM-DD")
    variant: str = Field(max_length=MAX_VARIANT_LENGTH)
    guesses: Previous = Field(
        description="The guesses of the whole game, in order (null is a skip). A game has at most 6"
    )
    played_as_daily: StrictBool = Field(
        description="Whether the guest began this game on the puzzle's own day"
    )
    daily_proof: str | None = Field(
        default=None,
        max_length=MAX_DAILY_PROOF_LENGTH,
        description=(
            "The `daily_proof` of the view the guest got on the puzzle's own day. Without a valid "
            "one, `played_as_daily` is not trusted and the game is stored as an archive play"
        ),
    )


class MergeCreate(BaseModel):
    entries: list[MergeEntry] = Field(max_length=MAX_MERGE_ENTRIES)


class RejectedEntryRead(BaseModel):
    date: dt.date
    variant: str
    code: MergeRejectionCode


class MergeRead(BaseModel):
    """What a merge did (AC-60): `stored + already_present + rejected` is the number of entries."""

    stored: int = Field(description="Entries that became new stored games")
    already_present: int = Field(
        description="Entries skipped because the account already has a game on that puzzle"
    )
    rejected: int = Field(description="Entries left out; see `rejected_entries`")
    daily_unproven: int = Field(
        description=(
            "Stored entries that said `played_as_daily` with no valid `daily_proof`: they were "
            "stored as archive plays (`counts_for_streak` false). Not part of the three counts"
        )
    )
    rejected_entries: list[RejectedEntryRead]


class HintRead(BaseModel):
    kind: str
    value: str


class MediaSourceRead(BaseModel):
    url: str = Field(description="A signed link, used as it is: do not prefix it")
    content_type: str


class PosterRead(BaseModel):
    url: str = Field(description="A signed link to the still frame of a video")
    content_type: str
    expires_at: dt.datetime


class MediaRead(BaseModel):
    """The media of one earned step (AC-25). When `expires_at` has passed, ask for the view again
    (AC-73)."""

    type: MediaType
    width: int
    height: int
    expires_at: dt.datetime = Field(description="When the links in `sources` stop working")
    sources: list[MediaSourceRead] = Field(
        description="Use in order: the first one the browser can play. WebM first, MP4 last"
    )
    poster: PosterRead | None = Field(description="The still frame of a video; null for an image")


class StepRead(BaseModel):
    step: int
    hint: HintRead | None
    media: MediaRead
    source_url: str | None = Field(description="Null until the game ends")


class GuessRead(BaseModel):
    title_id: int | None = Field(description="Null for a skip")
    name: str | None = Field(description="Null for a skip")
    result: GuessResult


class AnswerRead(BaseModel):
    title_id: int
    name: str
    franchise: str | None
    year: int | None


class GameViewBase(BaseModel):
    """What every game's puzzle view has, whatever the game. The service owns these fields, not
    the game module (the module knows nothing of proofs or the clock), so each defaults here and a
    new module cannot forget them. Assignment is validated, and the config keeps a defaulted field
    required in the OpenAPI response schema (always present, maybe null)."""

    model_config = ConfigDict(
        validate_assignment=True, json_schema_serialization_defaults_required=True
    )

    daily_proof: str | None = Field(
        default=None,
        description=(
            "For a guest, on the puzzle's own day only: a proof to keep with the local game and "
            "send as `daily_proof` in a merge entry. Null for an account and on any other day"
        ),
    )


class GameGuesserView(GameViewBase):
    """The puzzle view of the game guesser. The tag `type` lets later games extend this into a
    union without breaking the game guesser's clients (AC-3). No internal puzzle or progress id
    (AC-5)."""

    type: Literal["game_guesser"]
    slug: str
    variant: str
    date: dt.date
    number: int = Field(description="Days since the game's launch date, plus 1")
    status: ProgressStatus
    max_guesses: int
    guesses_used: int
    guesses_left: int
    guesses: list[GuessRead]
    steps: list[StepRead] = Field(description="Only the steps earned so far, all six at the end")
    answer: AnswerRead | None = Field(description="Null until the game ends")
    counts_for_streak: bool | None
    signed_in: bool = Field(
        description="False when the request has no valid session, so the caller is a guest"
    )


# What a puzzle route returns. Today it is the game guesser's view; when a second game arrives
# it becomes a union discriminated on `type` (AC-3), and the routes and the service do not change.
PuzzleView = GameGuesserView


class StatsRead(BaseModel):
    """The account's stats in one game variant (AC-46). The win rate is the client's to compute
    from `wins` and `played`."""

    played: int = Field(description="Finished games, won or lost, daily and archive alike")
    wins: int
    current_streak: int | None = Field(description="Null for a variant that has no streak")
    max_streak: int | None = Field(description="Null for a variant that has no streak")
    distribution: dict[int, int] = Field(
        description="Wins by guesses used: every key from 1 to `max_guesses`, zeros included"
    )
