import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt

from app.games.enums import GuessResult, MediaType, ProgressStatus

DATE_FORMAT = "YYYY-MM-DD"


DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def _strict_date(value: Any) -> dt.date:
    """Only a real `YYYY-MM-DD` day. Pydantic's lax `date` also reads an integer as a Unix
    timestamp and `date.fromisoformat` accepts `20261102`, so the shape is checked first (R-13)."""
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError(f"Expected a date as {DATE_FORMAT}")
    return dt.date.fromisoformat(value)


StrictDate = Annotated[dt.date, BeforeValidator(_strict_date)]


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


class GameGuesserView(BaseModel):
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
