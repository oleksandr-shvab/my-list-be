import datetime as dt
import importlib.util
import uuid
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import get_now
from app.core.config import settings
from app.core.exceptions import PuzzleDataError
from app.crud.session import create_session
from app.crud.title import create_title
from app.crud.user import deactivate_user, get_user_by_id
from app.games.enums import ClueContentType, ClueRole, GuessResult, ProgressStatus
from app.games.modules.base import (
    GuessEntry,
    MediaFileData,
    ProgressState,
    PuzzleData,
    SignedLink,
    TitleData,
)
from app.games.modules.game_guesser import GameGuesserModule
from app.main import app
from app.models.puzzle import Puzzle, PuzzleClue, PuzzleStatus
from app.models.title import Title
from tests.conftest import SetNow
from tests.factories import (
    IMAGE_TTL_SECONDS,
    TEST_MEDIA_BASE_URL,
    create_game,
    create_puzzle,
    create_titles,
    fake_sign,
    make_clue,
    media_key,
)

NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
LAUNCH = TODAY - dt.timedelta(days=3)
ANSWER_NAME = "Half-Life 2"


@pytest.fixture
async def seeded(db_session: AsyncSession, set_now: SetNow) -> None:
    """The game with puzzles for today and yesterday, tomorrow, an approved and a draft one."""
    set_now(NOW)
    game = await create_game(db_session, launch_date=LAUNCH)
    answer, other = await create_titles(db_session, ANSWER_NAME, "Portal 2")
    for offset, title, status in [
        (0, answer, PuzzleStatus.SCHEDULED),
        (-1, other, PuzzleStatus.SCHEDULED),
        (1, other, PuzzleStatus.SCHEDULED),
        (2, other, PuzzleStatus.APPROVED),
        (3, other, PuzzleStatus.DRAFT),
        (-5, other, PuzzleStatus.SCHEDULED),  # before the launch date
    ]:
        await create_puzzle(
            db_session,
            game=game,
            answer=title,
            puzzle_date=TODAY + dt.timedelta(days=offset),
            status=status,
        )


def _without_request_id(response_body: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in response_body.items() if key != "request_id"}


async def test__list_games__only_enabled_games_with_a_module_and_public_cache(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_game(db_session, slug="game-guesser", launch_date=LAUNCH)
    await create_game(db_session, slug="off-game", is_enabled=False)
    await create_game(db_session, slug="no-module-game")  # Enabled, but no game module.

    response = await client.get("/v1/games")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "slug": "game-guesser",
                "default_variant": "default",
                "launch_date": LAUNCH.isoformat(),
            }
        ]
    }
    assert response.headers["cache-control"] == "public, max-age=300"


@pytest.mark.usefixtures("seeded")
async def test__get_today_puzzle__step_one_only_and_no_answer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.get("/v1/games/game-guesser/puzzles/today")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert body["type"] == "game_guesser"
    assert body["slug"] == "game-guesser"
    assert body["variant"] == "default"
    assert body["date"] == TODAY.isoformat()
    assert body["number"] == 4  # Three days after the launch date, plus 1.
    assert body["status"] == "in_progress"
    assert (body["max_guesses"], body["guesses_used"], body["guesses_left"]) == (6, 0, 6)
    assert body["guesses"] == []
    assert body["counts_for_streak"] is None
    assert body["signed_in"] is False
    # AC-4: one step, no answer, no source, and nothing of the answer anywhere in the body.
    assert [step["step"] for step in body["steps"]] == [1]
    assert body["steps"][0]["source_url"] is None
    assert body["answer"] is None
    assert ANSWER_NAME not in response.text
    assert '"title_id"' not in response.text

    # AC-25 and AC-71: step 1 has its signed image link, and nothing of the later steps or of the
    # video's poster (no key and no link) is anywhere in the body.
    puzzle = await db_session.scalar(select(Puzzle).where(Puzzle.date == TODAY))
    assert puzzle is not None
    first_key = media_key(puzzle.id, 1, "source")
    media = body["steps"][0]["media"]
    assert (media["type"], media["width"], media["height"]) == ("image", 640, 360)
    expected_expiry = NOW + dt.timedelta(seconds=IMAGE_TTL_SECONDS)
    assert media["expires_at"] == expected_expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert media["poster"] is None
    assert [source["content_type"] for source in media["sources"]] == ["image/webp"]
    assert media["sources"][0]["url"].startswith(f"{TEST_MEDIA_BASE_URL}/v1/media/{first_key}?")
    for step, name in [(2, "source"), (3, "source"), (4, "source"), (5, "source"), (6, "source")]:
        assert media_key(puzzle.id, step, name) not in response.text
    assert media_key(puzzle.id, 6, "poster") not in response.text
    assert response.text.count("/v1/media/") == 1


@pytest.mark.usefixtures("seeded")
async def test__get_puzzle__dated_route_gives_the_same_view_as_today(client: AsyncClient) -> None:
    today = await client.get("/v1/games/game-guesser/puzzles/today")

    dated = await client.get(f"/v1/games/game-guesser/puzzles/{TODAY.isoformat()}")
    yesterday = await client.get(
        f"/v1/games/game-guesser/puzzles/{(TODAY - dt.timedelta(days=1)).isoformat()}",
        params={"variant": "default"},
    )

    assert dated.status_code == 200
    assert dated.json() == today.json()
    assert yesterday.status_code == 200
    assert yesterday.json()["number"] == 3


@pytest.mark.usefixtures("seeded")
async def test__hidden_puzzle__identical_404_bodies_on_every_route(client: AsyncClient) -> None:
    def day(offset: int) -> str:
        return (TODAY + dt.timedelta(days=offset)).isoformat()

    hidden_dates = {
        "tomorrow_scheduled": day(1),
        "approved": day(2),
        "draft": day(3),
        "before_launch_date": day(-5),
        "no_puzzle_that_day": day(-2),
        "far_future": "2099-01-01",
    }
    responses = {
        name: await client.get(f"/v1/games/game-guesser/puzzles/{date}")
        for name, date in hidden_dates.items()
    }
    # A game with no puzzle for today, after the clock moves to a day with none.
    app.dependency_overrides[get_now] = lambda: NOW + dt.timedelta(days=30)
    responses["today_has_no_puzzle"] = await client.get("/v1/games/game-guesser/puzzles/today")

    bodies = {name: _without_request_id(response.json()) for name, response in responses.items()}
    for name, response in responses.items():
        assert response.status_code == 404, name
        assert response.json()["code"] == "puzzle_not_found", name
        assert response.headers["cache-control"] == "no-store", name
        assert response.headers["content-type"].startswith("application/problem+json"), name
        assert bodies[name] == bodies["tomorrow_scheduled"], name


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/v1/games/no-such-game/puzzles/today", "game_not_found"),
        ("/v1/games/no-such-game/puzzles/2026-11-03", "game_not_found"),
        ("/v1/games/game-guesser/puzzles/today?variant=nope", "variant_not_found"),
        ("/v1/games/game-guesser/puzzles/2026-11-03?variant=nope", "variant_not_found"),
        ("/v1/games/game-guesser/puzzles/2026-11-03?variant=", "variant_not_found"),
    ],
)
async def test__unknown_game_or_variant__404_with_own_code(
    client: AsyncClient, path: str, code: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 404
    assert response.json()["code"] == code
    assert response.headers["cache-control"] == "no-store"


async def test__disabled_game__is_game_not_found(
    client: AsyncClient, db_session: AsyncSession, set_now: SetNow
) -> None:
    set_now(NOW)
    game = await create_game(db_session, is_enabled=False, launch_date=LAUNCH)
    (answer,) = await create_titles(db_session, ANSWER_NAME)
    await create_puzzle(db_session, game=game, answer=answer, puzzle_date=TODAY)

    response = await client.get("/v1/games/game-guesser/puzzles/today")

    assert response.status_code == 404
    assert response.json()["code"] == "game_not_found"


@pytest.mark.usefixtures("seeded")
@pytest.mark.parametrize(
    "bad_date",
    ["2026-02-30", "20261103", "2026-1-3", "not-a-date", "1700000000", "2026-11-03T00:00:00"],
)
async def test__invalid_date__422_with_the_bare_field_name(
    client: AsyncClient, bad_date: str
) -> None:
    response = await client.get(f"/v1/games/game-guesser/puzzles/{bad_date}")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"] == [{"field": "date", "code": "invalid_value", "params": {}}]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.usefixtures("seeded")
async def test__view__signed_in_is_false_for_no_cookie_and_a_bad_cookie(
    client: AsyncClient,
) -> None:
    url = "/v1/games/game-guesser/puzzles/today"
    unknown_user_token = await create_session(uuid.uuid4())

    no_cookie = await client.get(url)
    client.cookies.set(settings.SESSION_COOKIE_NAME, "not-a-real-session")
    bad_cookie = await client.get(url)
    client.cookies.set(settings.SESSION_COOKIE_NAME, unknown_user_token)
    unknown_user = await client.get(url)

    for response in (no_cookie, bad_cookie, unknown_user):
        assert response.status_code == 200
        assert response.json()["signed_in"] is False


@pytest.mark.usefixtures("seeded")
async def test__view__signed_in_is_true_with_a_valid_session(client: AsyncClient) -> None:
    await client.post(
        "/v1/auth/register", json={"email": "player@example.com", "password": "correct horse"}
    )

    response = await client.get("/v1/games/game-guesser/puzzles/today")

    assert response.status_code == 200
    assert response.json()["signed_in"] is True


async def test__tomorrows_puzzle__turns_visible_exactly_at_midnight_utc(
    client: AsyncClient, db_session: AsyncSession, set_now: SetNow
) -> None:
    game = await create_game(db_session, launch_date=LAUNCH)
    (answer,) = await create_titles(db_session, ANSWER_NAME)
    tomorrow = TODAY + dt.timedelta(days=1)
    await create_puzzle(db_session, game=game, answer=answer, puzzle_date=tomorrow)
    dated_url = f"/v1/games/game-guesser/puzzles/{tomorrow.isoformat()}"
    today_url = "/v1/games/game-guesser/puzzles/today"
    last_second = dt.datetime.combine(TODAY, dt.time(23, 59, 59), tzinfo=dt.UTC)

    set_now(last_second)
    before_dated = await client.get(dated_url)
    before_today = await client.get(today_url)
    set_now(last_second + dt.timedelta(seconds=1))
    after_dated = await client.get(dated_url)
    after_today = await client.get(today_url)

    assert before_dated.status_code == 404
    assert before_today.status_code == 404
    assert after_dated.status_code == 200
    assert after_today.status_code == 200
    assert after_today.json()["date"] == tomorrow.isoformat()
    assert after_dated.json() == after_today.json()


@pytest.mark.usefixtures("seeded")
async def test__puzzle_with_a_missing_step__500_internal_error_without_the_cause(
    client: AsyncClient,
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    puzzle = await db_session.scalar(select(Puzzle).where(Puzzle.date == TODAY))
    assert puzzle is not None
    await db_session.execute(
        delete(PuzzleClue).where(PuzzleClue.puzzle_id == puzzle.id, PuzzleClue.step == 4)
    )

    with caplog.at_level("ERROR", logger="app.core.errors"):
        response = await client.get("/v1/games/game-guesser/puzzles/today")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["title"] == "Internal server error"
    assert "steps" not in response.text
    assert "clue steps [1, 2, 3, 5, 6]" in caplog.text
    assert body["request_id"] in caplog.text


@pytest.mark.usefixtures("seeded")
async def test__inactive_user__is_a_guest_on_game_routes_and_gets_401_on_me(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    registered = await client.post(
        "/v1/auth/register", json={"email": "player@example.com", "password": "correct horse"}
    )
    user = await get_user_by_id(db_session, uuid.UUID(registered.json()["id"]))
    assert user is not None
    url = "/v1/games/game-guesser/puzzles/today"
    assert (await client.get(url)).json()["signed_in"] is True
    assert (await client.get("/v1/auth/me")).status_code == 200

    await deactivate_user(db_session, user)
    game_route = await client.get(url)
    me = await client.get("/v1/auth/me")

    assert game_route.status_code == 200
    assert game_route.json()["signed_in"] is False
    assert me.status_code == 401
    assert me.json()["code"] == "not_authenticated"


@pytest.mark.usefixtures("seeded")
async def test__puzzle_view__has_exactly_the_ac25_keys_and_no_internal_ids(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/games/game-guesser/puzzles/today")

    body = response.json()
    assert set(body) == {
        "type",
        "slug",
        "variant",
        "date",
        "number",
        "status",
        "max_guesses",
        "guesses_used",
        "guesses_left",
        "guesses",
        "steps",
        "answer",
        "counts_for_streak",
        "signed_in",
    }
    assert set(body["steps"][0]) == {"step", "hint", "media", "source_url"}
    assert set(body["steps"][0]["media"]) == {
        "type",
        "width",
        "height",
        "expires_at",
        "sources",
        "poster",
    }
    # AC-5: a puzzle is addressed by (slug, variant, date) only.
    for internal_id in ("puzzle_id", "game_id", "clue_id", "answer_title_id", "progress_id"):
        assert internal_id not in response.text
    # AC-6: no language anywhere.
    assert "language" not in response.text
    assert '"lang"' not in response.text
    assert '"locale"' not in response.text


@pytest.mark.parametrize(("days_after_launch", "number"), [(0, 1), (1, 2), (3, 4), (100, 101)])
async def test__puzzle_view__number_is_days_since_launch_plus_one(
    client: AsyncClient,
    db_session: AsyncSession,
    set_now: SetNow,
    days_after_launch: int,
    number: int,
) -> None:
    puzzle_day = LAUNCH + dt.timedelta(days=days_after_launch)
    set_now(dt.datetime.combine(puzzle_day, dt.time(8), tzinfo=dt.UTC))
    game = await create_game(db_session, launch_date=LAUNCH)
    (answer,) = await create_titles(db_session, ANSWER_NAME)
    await create_puzzle(db_session, game=game, answer=answer, puzzle_date=puzzle_day)

    dated = await client.get(f"/v1/games/game-guesser/puzzles/{puzzle_day.isoformat()}")
    today = await client.get("/v1/games/game-guesser/puzzles/today")

    assert dated.json()["number"] == number
    assert today.json()["number"] == number


@pytest.mark.usefixtures("seeded")
async def test__puzzle_view__default_variant_and_omitted_variant_are_the_same(
    client: AsyncClient,
) -> None:
    day = TODAY.isoformat()

    omitted = await client.get(f"/v1/games/game-guesser/puzzles/{day}")
    explicit = await client.get(f"/v1/games/game-guesser/puzzles/{day}?variant=default")
    today_omitted = await client.get("/v1/games/game-guesser/puzzles/today")
    today_explicit = await client.get("/v1/games/game-guesser/puzzles/today?variant=default")

    assert omitted.status_code == 200
    assert omitted.json()["variant"] == "default"
    assert explicit.json() == omitted.json()
    assert today_explicit.json() == today_omitted.json() == omitted.json()


@pytest.mark.usefixtures("seeded")
async def test__get_today_puzzle__resolves_the_date_from_the_injected_clock(
    client: AsyncClient, set_now: SetNow
) -> None:
    # Today's puzzle is the one for the date of the injected clock, not the real date; the body
    # names the date it resolved to (AC-15).
    for offset in (0, -1):
        day = TODAY + dt.timedelta(days=offset)
        set_now(dt.datetime.combine(day, dt.time(0, 0, 1), tzinfo=dt.UTC))

        today = await client.get("/v1/games/game-guesser/puzzles/today")
        dated = await client.get(f"/v1/games/game-guesser/puzzles/{day.isoformat()}")

        assert today.status_code == 200
        assert today.json()["date"] == day.isoformat()
        assert today.json() == dated.json()


@pytest.mark.usefixtures("seeded")
async def test__cache_control__puzzle_routes_are_private_no_store_and_errors_no_store(
    client: AsyncClient,
) -> None:
    ok_paths = [
        "/v1/games/game-guesser/puzzles/today",
        f"/v1/games/game-guesser/puzzles/{TODAY.isoformat()}",
    ]
    not_found_paths = [
        "/v1/games/game-guesser/puzzles/2099-01-01",
        "/v1/games/game-guesser/puzzles/2099-01-01?variant=nope",
        "/v1/games/no-such-game/puzzles/today",
    ]

    for path in ok_paths:
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "private, no-store", path
    for path in not_found_paths:
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.headers["cache-control"] == "no-store", path
    invalid = await client.get("/v1/games/game-guesser/puzzles/2026-02-30")
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"


# The game module, on plain data (no database).


def _title(title_id: int, name: str) -> TitleData:
    return TitleData(
        id=title_id,
        kind="video_game",
        name=name,
        normalized_name=name.lower(),
        franchise=None,
        normalized_franchise=None,
        year=2004,
    )


def _puzzle_data(step_count: int = 6) -> PuzzleData:
    return PuzzleData(
        slug="game-guesser",
        variant="default",
        title_kind="video_game",
        date=TODAY,
        number=4,
        answer=_title(1, ANSWER_NAME),
        clues=tuple(
            make_clue(
                step,
                hint_kind=None if step == 1 else "year",
                hint_value=None if step == 1 else "2004",
                source_url=f"https://example.com/{step}",
            )
            for step in range(1, step_count + 1)
        ),
    )


def _guesses(*results: GuessResult) -> tuple[GuessEntry, ...]:
    return tuple(
        GuessEntry(
            title=None
            if result == GuessResult.SKIPPED
            else _title(10 + position, f"Wrong {position}"),
            result=result,
        )
        for position, result in enumerate(results)
    )


def test__get_puzzle__in_progress_shows_one_step_more_than_the_guesses_used() -> None:
    state = ProgressState(guesses=_guesses(GuessResult.SKIPPED, GuessResult.WRONG))

    view = GameGuesserModule().get_puzzle(_puzzle_data(), state, is_signed_in=True, sign=fake_sign)

    assert [step.step for step in view.steps] == [1, 2, 3]
    assert all(step.source_url is None for step in view.steps)
    assert view.answer is None
    assert (view.guesses_used, view.guesses_left) == (2, 4)
    assert [guess.name for guess in view.guesses] == [None, "Wrong 1"]
    assert view.signed_in is True


@pytest.mark.parametrize("status", [ProgressStatus.WON, ProgressStatus.LOST])
def test__get_puzzle__finished_shows_all_steps_with_source_and_the_answer(
    status: ProgressStatus,
) -> None:
    state = ProgressState(status=status, guesses=_guesses(GuessResult.WRONG))

    view = GameGuesserModule().get_puzzle(_puzzle_data(), state, is_signed_in=False, sign=fake_sign)

    assert [step.step for step in view.steps] == [1, 2, 3, 4, 5, 6]
    assert view.steps[0].hint is None
    assert view.steps[1].hint is not None
    assert all(step.source_url for step in view.steps)
    assert view.answer is not None
    assert (view.answer.title_id, view.answer.name) == (1, ANSWER_NAME)


def test__get_puzzle__fewer_than_six_clues_raises() -> None:
    with pytest.raises(PuzzleDataError):
        GameGuesserModule().get_puzzle(
            _puzzle_data(step_count=5), ProgressState(), is_signed_in=False, sign=fake_sign
        )


def _with_steps(steps: tuple[int, ...]) -> PuzzleData:
    return replace(
        _puzzle_data(),
        clues=tuple(make_clue(step) for step in steps),
    )


@pytest.mark.parametrize("guesses_used", range(6))
def test__get_puzzle__in_progress_steps_are_one_to_guesses_used_plus_one(
    guesses_used: int,
) -> None:
    state = ProgressState(guesses=_guesses(*[GuessResult.WRONG] * guesses_used))

    view = GameGuesserModule().get_puzzle(_puzzle_data(), state, is_signed_in=False, sign=fake_sign)

    assert [step.step for step in view.steps] == list(range(1, guesses_used + 2))
    assert (view.guesses_used, view.guesses_left) == (guesses_used, 6 - guesses_used)


@pytest.mark.parametrize("status", [ProgressStatus.WON, ProgressStatus.LOST])
def test__get_puzzle__finished_shows_all_six_steps_whatever_the_guess_count(
    status: ProgressStatus,
) -> None:
    for guesses_used in (0, 1, 6):
        state = ProgressState(status=status, guesses=_guesses(*[GuessResult.WRONG] * guesses_used))

        view = GameGuesserModule().get_puzzle(
            _puzzle_data(), state, is_signed_in=True, sign=fake_sign
        )

        assert [step.step for step in view.steps] == [1, 2, 3, 4, 5, 6]
        assert [step.source_url for step in view.steps] == [
            f"https://example.com/{step}" for step in range(1, 7)
        ]
        assert view.answer is not None
        assert view.status == status


def test__get_puzzle__in_progress_serialized_view_has_nothing_of_the_answer() -> None:
    answer = TitleData(
        id=424242,
        kind="video_game",
        name="Secretname Quest",
        normalized_name="secretname quest",
        franchise="Secretfranchise",
        normalized_franchise="secretfranchise",
        year=1987,
    )
    puzzle = replace(_puzzle_data(), answer=answer)
    for guesses_used in range(6):
        state = ProgressState(guesses=_guesses(*[GuessResult.WRONG] * guesses_used))

        view = GameGuesserModule().get_puzzle(puzzle, state, is_signed_in=True, sign=fake_sign)

        assert view.answer is None
        assert all(step.source_url is None for step in view.steps)
        serialized = view.model_dump_json().lower()
        assert "424242" not in serialized
        assert "secretname" not in serialized
        assert "secretfranchise" not in serialized
        assert "example.com" not in serialized  # AC-4: no source before the end.


@pytest.mark.parametrize(
    ("variant", "default_variant", "expected"),
    [
        ("default", "default", True),
        ("nope", "default", False),
        ("", "default", False),
        ("Default", "default", False),
        ("classic", "classic", True),
        ("default", "classic", False),
    ],
)
def test__has_variant__is_true_only_for_the_games_default_variant(
    variant: str, default_variant: str, expected: bool
) -> None:
    module = GameGuesserModule()

    assert module.has_variant(variant, default_variant=default_variant) is expected


@pytest.mark.parametrize(
    "steps",
    [
        (),
        (1, 2, 3, 4, 5, 6, 7),
        (0, 1, 2, 3, 4, 5),
        (1, 2, 3, 4, 6),
        (1, 1, 2, 3, 4, 5),
        (2, 1, 3, 4, 5, 6),
        (1, 2, 3, 4, 5, 5),
    ],
)
def test__get_puzzle__clue_steps_that_are_not_one_to_six_raise(steps: tuple[int, ...]) -> None:
    with pytest.raises(PuzzleDataError):
        GameGuesserModule().get_puzzle(
            _with_steps(steps), ProgressState(), is_signed_in=False, sign=fake_sign
        )


# Media in the view, on plain data (AC-13b, AC-25, AC-71).


def _file(role: ClueRole, content_type: ClueContentType, name: str) -> MediaFileData:
    return MediaFileData(
        role=role, key=media_key(9, 0, name), content_type=content_type, width=640, height=360
    )


MP4 = _file(ClueRole.SOURCE, ClueContentType.VIDEO_MP4, "mp4")
WEBM = _file(ClueRole.SOURCE, ClueContentType.VIDEO_WEBM, "webm")
POSTER = _file(ClueRole.POSTER, ClueContentType.IMAGE_WEBP, "poster")
IMAGE = _file(ClueRole.SOURCE, ClueContentType.IMAGE_WEBP, "image")


def _with_media(step: int, media_type: str, *files: MediaFileData) -> PuzzleData:
    clue = replace(make_clue(step), media_type=media_type, media_files=files)
    puzzle = _puzzle_data()
    clues = tuple(clue if existing.step == step else existing for existing in puzzle.clues)
    return replace(puzzle, clues=clues)


def test__get_puzzle__step_six_is_a_video_with_webm_first_mp4_last_and_a_poster() -> None:
    puzzle = _with_media(6, "video", MP4, POSTER, WEBM)
    state = ProgressState(status=ProgressStatus.LOST, guesses=_guesses(*[GuessResult.WRONG] * 6))

    view = GameGuesserModule().get_puzzle(puzzle, state, is_signed_in=False, sign=fake_sign)

    media = view.steps[5].media
    assert media.type == "video"
    assert [source.content_type for source in media.sources] == ["video/webm", "video/mp4"]
    assert [source.url for source in media.sources] == [
        f"https://media.test/{file.key}?type={file.content_type}" for file in (WEBM, MP4)
    ]
    assert media.poster is not None
    assert media.poster.url == f"https://media.test/{POSTER.key}?type=image/webp"
    assert media.poster.content_type == "image/webp"
    assert (media.width, media.height) == (640, 360)
    # An image step has one source and no poster.
    image = view.steps[0].media
    assert (image.type, len(image.sources), image.poster) == ("image", 1, None)


def test__get_puzzle__a_video_without_webm_lists_the_mp4_alone() -> None:
    puzzle = _with_media(6, "video", MP4, POSTER)
    state = ProgressState(status=ProgressStatus.WON, guesses=_guesses(GuessResult.WRONG))

    view = GameGuesserModule().get_puzzle(puzzle, state, is_signed_in=False, sign=fake_sign)

    assert [source.content_type for source in view.steps[5].media.sources] == ["video/mp4"]


@pytest.mark.parametrize("guesses_used", range(6))
def test__get_puzzle__only_the_earned_steps_are_signed(guesses_used: int) -> None:
    signed: list[str] = []

    def spy(key: str, content_type: str) -> SignedLink:
        signed.append(key)
        return fake_sign(key, content_type)

    puzzle = _puzzle_data()
    state = ProgressState(guesses=_guesses(*[GuessResult.WRONG] * guesses_used))

    GameGuesserModule().get_puzzle(puzzle, state, is_signed_in=False, sign=spy)

    earned = puzzle.clues[: guesses_used + 1]
    assert signed == [file.key for clue in earned for file in clue.media_files]
    # The video and its poster are signed only when step 6 is earned.
    video_keys = {file.key for file in puzzle.clues[5].media_files}
    assert bool(video_keys & set(signed)) is (guesses_used == 5)


def test__get_puzzle__a_finished_game_signs_all_six_steps() -> None:
    signed: list[str] = []

    def spy(key: str, content_type: str) -> SignedLink:
        signed.append(key)
        return fake_sign(key, content_type)

    puzzle = _puzzle_data()
    state = ProgressState(status=ProgressStatus.WON, guesses=_guesses(GuessResult.WRONG))

    GameGuesserModule().get_puzzle(puzzle, state, is_signed_in=False, sign=spy)

    assert signed == [file.key for clue in puzzle.clues for file in clue.media_files]


@pytest.mark.parametrize(
    ("step", "media_type", "files"),
    [
        pytest.param(6, "video", (POSTER,), id="video_without_mp4"),
        pytest.param(6, "video", (MP4,), id="video_without_poster"),
        pytest.param(6, "video", (MP4, WEBM), id="video_with_webm_but_no_poster"),
        pytest.param(6, "video", (WEBM, POSTER), id="video_with_webm_only"),
        pytest.param(6, "video", (MP4, MP4, POSTER), id="video_with_two_mp4"),
        pytest.param(6, "video", (MP4, POSTER, POSTER), id="video_with_two_posters"),
        pytest.param(6, "video", (MP4, WEBM, WEBM, POSTER), id="video_with_two_webm"),
        pytest.param(6, "video", (MP4, IMAGE, POSTER), id="video_with_an_image_source"),
        pytest.param(6, "video", (), id="video_without_files"),
        pytest.param(1, "image", (), id="image_without_files"),
        pytest.param(1, "image", (IMAGE, IMAGE), id="image_with_two_files"),
        pytest.param(1, "image", (IMAGE, POSTER), id="image_with_a_poster"),
        pytest.param(1, "image", (MP4,), id="image_that_is_an_mp4"),
        pytest.param(1, "audio", (IMAGE,), id="unknown_media_type"),
        # A broken step the player has not reached is found at step 1, not on the day of step 6.
        pytest.param(5, "image", (), id="unearned_image_step_without_files"),
        pytest.param(6, "video", (MP4,), id="unearned_video_step_without_poster"),
    ],
)
def test__get_puzzle__media_that_break_ac13b_raise_even_for_an_unearned_step(
    step: int, media_type: str, files: tuple[MediaFileData, ...]
) -> None:
    puzzle = _with_media(step, media_type, *files)

    with pytest.raises(PuzzleDataError):
        GameGuesserModule().get_puzzle(puzzle, ProgressState(), is_signed_in=False, sign=fake_sign)


# The dev seed.


def _load_seed_script() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "seed_dev.py"
    spec = importlib.util.spec_from_file_location("seed_dev", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test__seed_dev__can_run_twice_and_gives_a_playable_game(
    client: AsyncClient, db_session: AsyncSession, set_now: SetNow, tmp_path: Path
) -> None:
    seed_dev = _load_seed_script()
    set_now(NOW)

    await seed_dev.seed(db_session, TODAY, media_dir=tmp_path)
    await seed_dev.seed(db_session, TODAY, media_dir=tmp_path)

    listing = (await client.get("/v1/games")).json()["items"]
    assert [game["slug"] for game in listing] == ["game-guesser"]
    visible_days = [TODAY - dt.timedelta(days=offset) for offset in range(4)]
    for day in visible_days:
        response = await client.get(f"/v1/games/game-guesser/puzzles/{day.isoformat()}")
        assert response.status_code == 200
        assert len(response.json()["steps"]) == 1
    for hidden in (TODAY + dt.timedelta(days=1), TODAY + dt.timedelta(days=2)):
        response = await client.get(f"/v1/games/game-guesser/puzzles/{hidden.isoformat()}")
        assert response.status_code == 404
    before_launch = TODAY - dt.timedelta(days=4)
    assert (
        await client.get(f"/v1/games/game-guesser/puzzles/{before_launch.isoformat()}")
    ).status_code == 404
    # Six puzzles with six clues each; exactly one has no Metacritic hint at step 2.
    clue_hints = (
        (await db_session.execute(select(PuzzleClue.hint_kind).where(PuzzleClue.step == 2)))
        .scalars()
        .all()
    )
    assert sorted(kind or "" for kind in clue_hints) == [""] + ["metacritic_score"] * 5


async def test__seed_dev__updates_existing_titles_and_survives_duplicates(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    seed_dev = _load_seed_script()
    # An old run left this title with another franchise, year and normalized name, twice.
    stale = [
        await create_title(db_session, name="Half-Life 2", franchise="Old Series", year=1999)
        for _ in range(2)
    ]
    await db_session.execute(
        update(Title).where(Title.id == stale[0].id).values(normalized_franchise="stale")
    )
    await db_session.refresh(stale[0])

    await seed_dev.seed(db_session, TODAY, media_dir=tmp_path)

    rows = (
        (
            await db_session.execute(
                select(Title).where(Title.name == "Half-Life 2").order_by(Title.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in rows] == [stale[0].id, stale[1].id]  # No new row.
    lowest = rows[0]
    await db_session.refresh(lowest)
    assert (lowest.franchise, lowest.normalized_franchise, lowest.year) == (
        "Half-Life",
        "half life",
        2004,
    )


def test__seed_dev__refuses_when_the_environment_variable_is_not_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_dev = _load_seed_script()
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5544/db")

    for value in (None, "test", "production", ""):
        if value is None:
            monkeypatch.delenv("ENVIRONMENT", raising=False)
        else:
            monkeypatch.setenv("ENVIRONMENT", value)
        assert seed_dev.main() == 1


@pytest.mark.parametrize("host", ["db.example.com", "10.0.0.5", "postgres"])
def test__seed_dev__refuses_a_database_on_another_host(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    seed_dev = _load_seed_script()
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "DATABASE_URL", f"postgresql+asyncpg://u:p@{host}:5432/db")

    assert seed_dev.main() == 1
    assert seed_dev.refusal_reason() is not None


def test__seed_dev__refuses_to_write_media_when_the_storage_is_not_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_dev = _load_seed_script()
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5544/db")
    monkeypatch.setattr(settings, "MEDIA_STORAGE", "s3")

    assert seed_dev.main() == 1
    reason = seed_dev.refusal_reason()
    assert reason is not None and "MEDIA_STORAGE" in reason


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test__seed_dev__accepts_a_local_environment_and_host(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    seed_dev = _load_seed_script()
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "DATABASE_URL", f"postgresql+asyncpg://u:p@{host}:5544/db")

    assert seed_dev.refusal_reason() is None
