"""The local review page (M1-24a, SPEC-04 AC-47 to AC-54, AC-86).

The app is driven through httpx over its own ASGI app, with the test session as its database
session (`actions` commit, which only releases the test's savepoint)."""

import datetime as dt
import re
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.content.review import actions
from app.content.review import html as review_html
from app.content.review.app import SessionFactory, create_review_app
from app.content.target import TargetRefusedError
from app.crud.exclusion import add_exclusion, list_exclusions
from app.games.enums import ExclusionReason
from app.models.exclusion import AnswerExclusion
from app.models.game import Game
from app.models.progress import Progress
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue, PuzzleStatus
from app.models.title import Title
from app.models.user import User
from tests.factories import create_game, create_puzzle, create_titles, media_key

PORT = 8765
ORIGIN = f"http://localhost:{PORT}"
TOKEN = "t" * 43
NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.UTC)
LOCAL = {"ENVIRONMENT": "local"}
HOSTILE = "<script>alert(1)</script>"


@dataclass
class World:
    db: AsyncSession
    game: Game
    titles: list[Title]
    media_dir: Path
    game_id: int  # read once: a rollback expires the ORM objects

    async def draft(
        self,
        slot: int | None,
        *,
        title: int = 0,
        image_ids: list[list[str]] | None = None,
        status: PuzzleStatus = PuzzleStatus.IN_REVIEW,
    ) -> Puzzle:
        puzzle = await create_puzzle(
            self.db,
            game=self.game,
            answer=self.titles[title],
            puzzle_date=dt.date(2000, 1, 1),
            status=status,
        )
        puzzle.date = None
        puzzle.build_slot = slot
        clues = (
            await self.db.execute(select(PuzzleClue).where(PuzzleClue.puzzle_id == puzzle.id))
        ).scalars()
        # By default a draft from a game with many screenshots: seven different ones.
        default_ids = [[f"img{step}"] for step in range(1, 6)] + [["img6", "img7"]]
        for clue in clues:
            clue.source_image_ids = (image_ids or default_ids)[clue.step - 1]
        await self.db.commit()
        return puzzle

    async def count(self, model: type) -> int:
        return await self.db.scalar(select(func.count()).select_from(model)) or 0

    async def status_of(self, puzzle_id: int) -> str | None:
        return await self.db.scalar(select(Puzzle.status).where(Puzzle.id == puzzle_id))


@pytest.fixture
async def world(db_session: AsyncSession, tmp_path: Path) -> World:
    game = await create_game(db_session)
    titles = await create_titles(db_session, "Portal", "Doom", "Quake")
    await db_session.commit()
    return World(db_session, game, titles, tmp_path, game.id)


def make_app(
    world: World,
    environ: Mapping[str, str] = LOCAL,
    session_factory: SessionFactory | None = None,
    token: str | None = TOKEN,
):
    @asynccontextmanager
    async def test_session() -> AsyncIterator[AsyncSession]:
        yield world.db

    return create_review_app(
        session_factory=session_factory or test_session,
        media_dir=world.media_dir,
        port=PORT,
        environ=environ,
        clock=lambda: NOW,
        token=token,
    )


@pytest.fixture
async def page(world: World) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=make_app(world))
    async with AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as ac:
        yield ac


# (a) one happy path per route


async def test__queue__lists_the_draft(world: World, page: AsyncClient) -> None:
    puzzle = await world.draft(7)
    response = await page.get("/")
    assert response.status_code == 200
    assert f"/drafts/{puzzle.id}" in response.text
    assert "Portal" in response.text


async def test__draft_page__shows_six_clues_the_answer_and_escapes_igdb_text(
    world: World, page: AsyncClient
) -> None:
    world.titles[0].name = f"Portal {HOSTILE}"
    world.titles[0].franchise = '"><img src=x onerror=alert(2)>'
    puzzle = await world.draft(7)
    no_slot = await world.draft(None, title=1)
    clue = (
        (
            await world.db.execute(
                select(PuzzleClue).where(PuzzleClue.puzzle_id == puzzle.id, PuzzleClue.step == 2)
            )
        )
        .scalars()
        .one()
    )
    clue.hint_kind, clue.hint_value = "developer", f"{HOSTILE} & Co"
    clue.source_url = "javascript:alert(3)"
    await world.db.commit()

    response = await page.get(f"/drafts/{puzzle.id}")

    body = response.text
    assert response.status_code == 200
    assert body.count("<figure>") == 6
    assert "<title>Slot 7</title>" in body
    assert "2 in review" in body
    assert "<title>Slot none</title>" in (await page.get(f"/drafts/{no_slot.id}")).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body  # the answer is shown, escaped
    assert "Developer: &lt;script&gt;" in body
    assert "<script" not in body
    assert "<img src=x" not in body
    assert 'href="javascript:' not in body
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test__file__is_served_with_the_stored_type(world: World, page: AsyncClient) -> None:
    puzzle = await world.draft(7)
    key = media_key(puzzle.id, 1, "source")
    (world.media_dir / key).write_bytes(b"RIFFxxxxWEBP")

    response = await page.get(f"/files/{key}")

    assert response.status_code == 200
    assert response.content == b"RIFFxxxxWEBP"
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Only a SHA-256 key is a file name, even if a row says otherwise.
    row = (await world.db.execute(select(ClueMediaFile).limit(1))).scalars().one()
    row.key = "notes.txt"
    await world.db.commit()
    (world.media_dir / "notes.txt").write_text("not a media file")
    assert (await page.get("/files/notes.txt")).status_code == 404


async def test__approve__moves_the_draft_to_approved_at_the_clock(
    world: World, page: AsyncClient
) -> None:
    puzzle = await world.draft(7)
    other = await world.draft(8, title=1)

    response = await page.post(f"/drafts/{puzzle.id}/approve", data={"token": TOKEN})

    assert response.status_code == 303
    assert response.headers["location"] == f"/drafts/{other.id}"
    row = (await world.db.execute(select(Puzzle).where(Puzzle.id == puzzle.id))).scalar_one()
    assert (row.status, row.approved_at, row.date) == (PuzzleStatus.APPROVED, NOW, None)


async def test__reject_never__leaves_an_exclusion_and_no_puzzle(
    world: World, page: AsyncClient
) -> None:
    puzzle = await world.draft(7)

    response = await page.post(
        f"/drafts/{puzzle.id}/reject", data={"token": TOKEN, "reason": "never"}
    )

    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert [await world.count(m) for m in (Puzzle, PuzzleClue, ClueMediaFile)] == [0, 0, 0]
    (row,) = await list_exclusions(world.db, world.game_id)
    assert (row.title_id, row.reason) == (world.titles[0].id, ExclusionReason.NEVER)


# (b) the rules, at the service level


async def test__reject__records_variant_slot_and_every_image_id_in_step_order(
    world: World,
) -> None:
    image_ids = [["c"], ["b"], ["a"], ["e"], ["d"], ["z", "y", "x"]]
    puzzle = await world.draft(7, image_ids=image_ids)
    await world.db.commit()

    await actions.reject(world.db, puzzle.id, ExclusionReason.REBUILD)

    (row,) = (await world.db.execute(select(AnswerExclusion))).scalars()
    assert (row.game_id, row.variant, row.title_id) == (
        world.game_id,
        "default",
        world.titles[0].id,
    )
    assert (row.reason, row.build_slot) == (ExclusionReason.REBUILD, 7)
    assert row.image_ids == ["c", "b", "a", "e", "d", "z", "y", "x"]
    assert await world.count(Puzzle) == 0


@pytest.mark.parametrize("has_earlier_never", [False, True])
async def test__reject_never__turns_its_slots_rebuild_rows_into_one_never_row(
    world: World, has_earlier_never: bool
) -> None:
    first = await world.draft(7)
    await actions.reject(world.db, first.id, ExclusionReason.REBUILD)
    other = await world.draft(8, title=1)
    await actions.reject(world.db, other.id, ExclusionReason.REBUILD)
    if has_earlier_never:
        await add_exclusion(
            world.db,
            game_id=world.game_id,
            variant="default",
            title_id=world.titles[0].id,
            reason=ExclusionReason.NEVER,
            build_slot=None,
            image_ids=[],
        )
    rebuilt = await world.draft(7)  # the rebuilt draft of slot 7, same answer
    await actions.reject(world.db, rebuilt.id, ExclusionReason.NEVER)

    rows = await list_exclusions(world.db, world.game_id)

    # Slot 8's row stays; slot 7's became the title's only `never` row.
    assert sorted((r.title_id, r.reason, r.build_slot) for r in rows) == sorted(
        [
            (world.titles[1].id, ExclusionReason.REBUILD, 8),
            (world.titles[0].id, ExclusionReason.NEVER, None),
        ]
    )


async def test__reject_never__refuses_when_the_slots_rebuild_rows_name_another_title(
    world: World,
) -> None:
    earlier = await world.draft(7, title=1)
    await actions.reject(world.db, earlier.id, ExclusionReason.REBUILD)
    puzzle_id = (await world.draft(7, title=0)).id  # not what the builder would make

    with pytest.raises(actions.RebuildRowsMismatchError):
        await actions.reject(world.db, puzzle_id, ExclusionReason.NEVER)

    assert await world.status_of(puzzle_id) == PuzzleStatus.IN_REVIEW
    rows = await list_exclusions(world.db, world.game_id)
    assert [(r.reason, r.build_slot) for r in rows] == [(ExclusionReason.REBUILD, 7)]


async def test__reject__is_one_transaction(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    earlier = await world.draft(7)
    await actions.reject(world.db, earlier.id, ExclusionReason.REBUILD)
    puzzle_id = (await world.draft(7)).id

    async def failing_delete(db: AsyncSession, puzzle: Puzzle) -> None:
        raise RuntimeError("the delete failed")

    monkeypatch.setattr(actions, "delete_puzzle", failing_delete)
    with pytest.raises(RuntimeError):
        await actions.reject(world.db, puzzle_id, ExclusionReason.NEVER)

    assert await world.status_of(puzzle_id) == PuzzleStatus.IN_REVIEW
    rows = await list_exclusions(world.db, world.game_id)
    assert [r.reason for r in rows] == [ExclusionReason.REBUILD]  # no never row, no row deleted


async def test__approve__a_failed_commit_leaves_the_draft_in_review(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    puzzle_id = (await world.draft(7)).id

    async def failing_commit() -> None:
        raise RuntimeError("the commit failed")

    monkeypatch.setattr(world.db, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        await actions.approve(world.db, puzzle_id, NOW)
    monkeypatch.undo()

    assert await world.status_of(puzzle_id) == PuzzleStatus.IN_REVIEW


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("approved", actions.NotInReviewError),
        ("stored_game", actions.StoredGameError),
        ("missing", actions.PuzzleNotFoundError),
    ],
)
async def test__action__refused_cases_change_nothing(
    world: World, action: str, case: str, error: type[Exception]
) -> None:
    puzzle = await world.draft(
        7, status=PuzzleStatus.APPROVED if case == "approved" else PuzzleStatus.IN_REVIEW
    )
    if case == "stored_game":
        account = User(email="p@example.com", hashed_password="x")
        world.db.add(account)
        await world.db.flush()
        world.db.add(
            Progress(
                user_id=account.id,
                puzzle_id=puzzle.id,
                status="in_progress",
                started_at=NOW,
                counts_for_streak=True,
                origin="play",
            )
        )
        await world.db.commit()
    real_id = puzzle.id
    puzzle_id = 0 if case == "missing" else real_id

    with pytest.raises(error):
        if action == "approve":
            await actions.approve(world.db, puzzle_id, NOW)
        else:
            await actions.reject(world.db, puzzle_id, ExclusionReason.NEVER)

    if case != "missing":
        assert await world.status_of(real_id) == (
            PuzzleStatus.APPROVED if case == "approved" else PuzzleStatus.IN_REVIEW
        )
    assert await world.count(AnswerExclusion) == 0


async def test__reject_rebuild__needs_a_slot(world: World) -> None:
    puzzle_id = (await world.draft(None)).id
    with pytest.raises(actions.NoBuildSlotError):
        await actions.reject(world.db, puzzle_id, ExclusionReason.REBUILD)
    assert await world.status_of(puzzle_id) == PuzzleStatus.IN_REVIEW


# Decision 74: a draft from a game of 5 or 6 screenshots used 5 different ones and can never be
# rebuilt (fewer than 5 would remain); from a larger pool it used 7 or more.
STILLS_ONLY = [["c"], ["b"], ["a"], ["e"], ["d"], ["a", "e"]]  # 5 different, the clip reuses two
SIX_IDS = [["c"], ["b"], ["a"], ["e"], ["d"], ["f", "a"]]


async def test__reject_rebuild__refuses_a_draft_of_fewer_than_seven_screenshots(
    world: World,
) -> None:
    refused = [
        (await world.draft(slot, image_ids=image_ids)).id  # read once: a rollback expires objects
        for slot, image_ids in enumerate([STILLS_ONLY, SIX_IDS, [[]] * 6], start=1)
    ]
    seven = (await world.draft(9)).id
    for puzzle_id in refused:
        with pytest.raises(actions.TooFewScreenshotsError):
            await actions.reject(world.db, puzzle_id, ExclusionReason.REBUILD)
        assert await world.status_of(puzzle_id) == PuzzleStatus.IN_REVIEW
    assert await world.count(AnswerExclusion) == 0
    # Never is still allowed, and so is a rebuild of a draft with seven different screenshots.
    await actions.reject(world.db, refused[0], ExclusionReason.NEVER)
    await actions.reject(world.db, seven, ExclusionReason.REBUILD)
    assert await world.count(AnswerExclusion) == 2


# (c) the page takes requests only from itself


@pytest.mark.parametrize(
    ("headers", "data"),
    [
        ({"Origin": "http://evil.example"}, {"token": TOKEN}),
        ({"Origin": "http://localhost:9999"}, {"token": TOKEN}),
        ({"Origin": "null"}, {"token": TOKEN}),
        ({"Origin": ""}, {"token": TOKEN}),
        ({}, {"token": TOKEN}),
        ({"Origin": ORIGIN}, {"other": "1"}),
        ({"Origin": ORIGIN}, {"token": "wrong"}),
        ({"Origin": ORIGIN, "Host": "evil.example:8765"}, {"token": TOKEN}),
        ({"Origin": ORIGIN, "Host": "localhost:9999"}, {"token": TOKEN}),
        ({"Origin": ORIGIN, "Host": "localhost"}, {"token": TOKEN}),
    ],
    ids=[
        "foreign-origin",
        "own-host-other-port-origin",
        "null-origin",
        "empty-origin",
        "no-origin",
        "no-token",
        "wrong-token",
        "foreign-host",
        "other-port-host",
        "no-port-host",
    ],
)
async def test__post__from_anywhere_but_the_page_is_refused_and_changes_nothing(
    world: World,
    page: AsyncClient,
    headers: dict[str, str],
    data: dict[str, str],
) -> None:
    puzzle = await world.draft(7)
    page.headers.pop("Origin")

    for path, form in (("approve", data), ("reject", {**data, "reason": "never"})):
        response = await page.post(f"/drafts/{puzzle.id}/{path}", data=form, headers=headers)
        assert response.status_code == 403

    assert await world.status_of(puzzle.id) == PuzzleStatus.IN_REVIEW
    assert await world.count(AnswerExclusion) == 0


async def test__get__with_a_foreign_host_is_refused(world: World, page: AsyncClient) -> None:
    puzzle = await world.draft(7)
    for path in ("/", f"/drafts/{puzzle.id}", f"/files/{media_key(puzzle.id, 1, 'source')}"):
        response = await page.get(path, headers={"Host": "evil.example:8765"})
        assert response.status_code == 403
        assert "Portal" not in response.text


async def test__reject__with_another_reason_is_422_and_changes_nothing(
    world: World, page: AsyncClient
) -> None:
    puzzle = await world.draft(7)
    response = await page.post(
        f"/drafts/{puzzle.id}/reject", data={"token": TOKEN, "reason": "delete"}
    )
    assert response.status_code == 422
    assert await world.status_of(puzzle.id) == PuzzleStatus.IN_REVIEW


@pytest.mark.parametrize(
    "environ",
    [
        {"ENVIRONMENT": "staging"},
        {"ENVIRONMENT": "production"},
        {},
        {"ENVIRONMENT": "local", "CI": "true"},
    ],
    ids=["staging", "production", "unset", "ci"],
)
async def test__app__refuses_to_start_outside_local(
    world: World, environ: Mapping[str, str]
) -> None:
    with pytest.raises(TargetRefusedError):
        make_app(world, environ)


def post_origin_header(policy: str, *, is_same_origin: bool = True) -> str:
    """The `Origin` a browser sends on a form POST of an http page to an http URL, by the Fetch
    standard ("append a request `Origin` header"): `no-referrer` makes it `null`, `same-origin`
    makes it `null` only across origins, every other policy sends the origin."""
    if policy == "no-referrer" or (policy == "same-origin" and not is_same_origin):
        return "null"
    return ORIGIN


async def test__page_referrer_policy__keeps_the_origin_on_its_own_form_posts(
    world: World, page: AsyncClient
) -> None:
    puzzle = await world.draft(7)
    assert post_origin_header("no-referrer") == "null"  # the rule the helper encodes

    for path in ("/", f"/drafts/{puzzle.id}"):
        policy = (await page.get(path)).headers["referrer-policy"]
        # A list of policies: the browser uses the last one it knows.
        assert post_origin_header(policy.split(",")[-1].strip()) == ORIGIN

    policy = (await page.get(f"/drafts/{puzzle.id}")).headers["referrer-policy"]
    origin = post_origin_header(policy)
    response = await page.post(
        f"/drafts/{puzzle.id}/approve", data={"token": TOKEN}, headers={"Origin": origin}
    )
    assert response.status_code == 303


@pytest.mark.parametrize("puzzle_id", [0, 2_147_483_648, 10**30])
async def test__puzzle_id__outside_the_key_range_is_404(page: AsyncClient, puzzle_id: int) -> None:
    assert (await page.get(f"/drafts/{puzzle_id}")).status_code == 404
    response = await page.post(f"/drafts/{puzzle_id}/approve", data={"token": TOKEN})
    assert response.status_code == 404


async def test__unhandled_error__answers_500_with_the_security_headers(world: World) -> None:
    @asynccontextmanager
    async def broken_session() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("the database is down")
        yield  # pragma: no cover

    transport = ASGITransport(
        app=make_app(world, session_factory=broken_session), raise_app_exceptions=False
    )
    async with AsyncClient(transport=transport, base_url=ORIGIN) as client:
        response = await client.get("/")

    assert response.status_code == 500
    assert "database" not in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test__action__a_failing_rollback_does_not_hide_the_error(
    world: World, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    puzzle_id = (await world.draft(7)).id

    async def failing(*args: object) -> None:
        raise RuntimeError("the commit failed")

    async def failing_rollback() -> None:
        raise ConnectionError("the connection is gone")

    monkeypatch.setattr(world.db, "commit", failing)
    monkeypatch.setattr(world.db, "rollback", failing_rollback)
    with pytest.raises(RuntimeError, match="the commit failed"):
        if action == "approve":
            await actions.approve(world.db, puzzle_id, NOW)
        else:
            await actions.reject(world.db, puzzle_id, ExclusionReason.NEVER)


# (d) gaps found by mutating the code (M1-24a test-writer pass). Each test below turns red when
# the code it names is changed; the one-line reasons say how.


class Markup(HTMLParser):
    """The tags and text of a page as a browser reads them: attribute values are unescaped, so an
    attribute that was not escaped shows up as extra attributes or tags."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text = ""
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text += data

    def find(self, name: str) -> list[dict[str, str | None]]:
        return [attrs for tag, attrs in self.tags if tag == name]


@dataclass
class Form:
    action: str
    hidden: dict[str, str]
    buttons: list[tuple[str, str]]  # (name, value) of each submit button, "" when it has none


def read_forms(text: str) -> list[Form]:
    forms: list[Form] = []
    for tag, attrs in Markup(text).tags:
        if tag == "form":
            forms.append(Form(attrs["action"] or "", {}, []))
        elif tag == "input" and attrs.get("type") == "hidden":
            forms[-1].hidden[attrs["name"] or ""] = attrs["value"] or ""
        elif tag == "button":
            forms[-1].buttons.append((attrs.get("name") or "", attrs.get("value") or ""))
    return forms


FORM = "application/x-www-form-urlencoded"
PAD = "&pad="


def sized_form(size: int) -> bytes:
    """A form with a valid token, padded to exactly `size` bytes."""
    head = f"token={TOKEN}{PAD}"
    return (head + "a" * (size - len(head))).encode()


@pytest.mark.parametrize(
    ("path", "content_type", "body", "status"),
    [
        ("approve", FORM, sized_form(2048), 303),  # the cap is 2 KiB, inclusive
        ("approve", FORM, sized_form(2049), 413),
        ("approve", FORM, f"token={TOKEN}&a=1&b=2&c=3".encode(), 303),  # 4 fields
        ("approve", FORM, f"token={TOKEN}&a=1&b=2&c=3&d=4".encode(), 400),  # 5 fields
        ("approve", FORM + "; charset=UTF-8", f"token={TOKEN}".encode(), 303),
        ("approve", FORM.upper(), f"token={TOKEN}".encode(), 303),
        ("approve", "text/plain", f"token={TOKEN}".encode(), 415),  # a no-preflight cross-site type
        ("approve", None, f"token={TOKEN}".encode(), 415),
        ("approve", FORM, f"token=wrong&token={TOKEN}".encode(), 403),  # the first value wins
        ("approve", FORM, f"token={TOKEN}&token=wrong".encode(), 303),
        ("reject", FORM, f"token={TOKEN}&reason=".encode() + b"\xff\xfe", 422),  # not a 500
    ],
    ids=[
        "cap-exact",
        "cap-over",
        "four-fields",
        "five-fields",
        "charset-parameter",
        "uppercase-type",
        "text-plain",
        "no-type",
        "duplicate-wrong-first",
        "duplicate-right-first",
        "invalid-utf8",
    ],
)
async def test__post__form_reader_limits_and_rules(
    world: World,
    page: AsyncClient,
    path: str,
    content_type: str | None,
    body: bytes,
    status: int,
) -> None:
    puzzle = await world.draft(7)
    headers = {"Content-Type": content_type} if content_type else {}

    response = await page.post(f"/drafts/{puzzle.id}/{path}", content=body, headers=headers)

    assert response.status_code == status
    expected = PuzzleStatus.APPROVED if status == 303 else PuzzleStatus.IN_REVIEW
    assert await world.status_of(puzzle.id) == expected


LOCAL_NAME = f"localhost:{PORT}"
LOCAL_IP = f"127.0.0.1:{PORT}"


@pytest.mark.parametrize(
    ("headers", "get_status", "post_status"),
    [
        ([("Host", LOCAL_IP), ("Origin", f"http://{LOCAL_IP}")], 200, 303),
        ([("Host", LOCAL_NAME), ("Origin", f"http://{LOCAL_IP}")], 200, 303),
        ([("Host", LOCAL_NAME), ("Origin", f"https://{LOCAL_NAME}")], 200, 403),
        ([("Host", f"[::1]:{PORT}"), ("Origin", ORIGIN)], 403, 403),
        ([("Host", f"0.0.0.0:{PORT}"), ("Origin", ORIGIN)], 403, 403),
        ([("Host", f"evil.localhost:{PORT}"), ("Origin", ORIGIN)], 403, 403),
        ([("Host", f"{LOCAL_NAME}.evil.example"), ("Origin", ORIGIN)], 403, 403),
        ([("Host", LOCAL_NAME), ("Host", "evil.example"), ("Origin", ORIGIN)], 403, 403),
        ([("Host", LOCAL_NAME), ("Origin", ORIGIN), ("Origin", "http://evil.example")], 200, 403),
    ],
    ids=[
        "ip-host-and-origin",
        "name-host-ip-origin",
        "https-origin",
        "ipv6-host",
        "any-address-host",
        "localhost-subdomain-host",
        "host-with-suffix",
        "two-hosts-good-first",
        "two-origins-good-first",
    ],
)
async def test__host_and_origin__only_the_page_own_names_pass(
    world: World,
    page: AsyncClient,
    headers: list[tuple[str, str]],
    get_status: int,
    post_status: int,
) -> None:
    puzzle = await world.draft(7)
    page.headers.pop("Origin")

    get_response = await page.get("/", headers=headers)
    post_response = await page.post(
        f"/drafts/{puzzle.id}/approve", data={"token": TOKEN}, headers=headers
    )

    assert (get_response.status_code, post_response.status_code) == (get_status, post_status)
    approved = PuzzleStatus.APPROVED if post_status == 303 else PuzzleStatus.IN_REVIEW
    assert await world.status_of(puzzle.id) == approved


async def test__draft_page_forms__work_with_the_pages_own_token_and_buttons(
    world: World, page: AsyncClient
) -> None:
    rebuilt = await world.draft(7)
    approved = await world.draft(8, title=1)
    few = await world.draft(9, title=2, image_ids=STILLS_ONLY)
    mixed = await world.draft(10, title=2, image_ids=[*STILLS_ONLY[:5], ["a", "z"]])
    # Read once: a refused action rolls back and expires the objects.
    crop_clip = await world.draft(11, title=2, image_ids=[*STILLS_ONLY[:5], ["c", "a"]])
    rebuilt_id, approved_id, few_id, mixed_id, crop_clip_id = (
        p.id for p in (rebuilt, approved, few, mixed, crop_clip)
    )

    # A game with too few screenshots has no Rebuild button, a note, and the clip is labelled.
    few_page = (await page.get(f"/drafts/{few_id}")).text
    assert [(f.action, f.buttons) for f in read_forms(few_page)] == [
        (f"/drafts/{few_id}/approve", [("", "")]),
        (f"/drafts/{few_id}/reject", [("reason", "never")]),
    ]
    assert "This game has too few screenshots to rebuild; reject as Never" in few_page
    assert few_page.count("Clip made from the stills (decision 74)") == 1
    # The server refuses it too, with the button hidden or not.
    forms = read_forms(few_page)
    response = await page.post(forms[1].action, data={**forms[1].hidden, "reason": "rebuild"})
    assert response.status_code == 409
    assert await world.status_of(few_id) == PuzzleStatus.IN_REVIEW
    assert await world.count(AnswerExclusion) == 0

    # A draft with a clip of its own has both, and no label or note. A clip that has one still
    # and one other screenshot is not "made from the stills".
    own_page = (await page.get(f"/drafts/{rebuilt_id}")).text
    assert "too few screenshots" not in own_page and "made from the stills" not in own_page
    assert "made from the stills" not in (await page.get(f"/drafts/{mixed_id}")).text
    # Only steps 2 to 5 count: a clip that reuses the crop's screenshot is not from the stills.
    assert "made from the stills" not in (await page.get(f"/drafts/{crop_clip_id}")).text

    forms = read_forms((await page.get(f"/drafts/{rebuilt_id}")).text)

    assert [(f.action, f.buttons) for f in forms] == [
        (f"/drafts/{rebuilt_id}/approve", [("", "")]),
        (f"/drafts/{rebuilt_id}/reject", [("reason", "rebuild"), ("reason", "never")]),
    ]
    response = await page.post(forms[1].action, data={**forms[1].hidden, "reason": "rebuild"})
    assert (response.status_code, response.headers["location"]) == (303, f"/drafts/{approved_id}")
    (row,) = await list_exclusions(world.db, world.game_id)
    assert (row.reason, row.build_slot) == (ExclusionReason.REBUILD, 7)
    forms = read_forms((await page.get(f"/drafts/{approved_id}")).text)
    response = await page.post(forms[0].action, data=forms[0].hidden)
    assert response.status_code == 303
    assert await world.status_of(approved_id) == PuzzleStatus.APPROVED


async def test__token__is_made_per_process_and_kept_for_its_life(world: World) -> None:
    puzzle_id = (await world.draft(7)).id
    pages = []
    for _ in range(2):
        transport = ASGITransport(app=make_app(world, token=None))
        client = AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN})
        async with client:
            tokens = {
                read_forms((await client.get(f"/drafts/{puzzle_id}")).text)[0].hidden["token"]
                for _ in range(2)
            }
            assert len(tokens) == 1  # the same token on every page of one process
            pages.append((tokens.pop(), transport))

    (first, first_transport), (second, _) = pages
    assert first != second
    assert min(len(first), len(second)) >= 43  # secrets.token_urlsafe(32)
    async with AsyncClient(
        transport=first_transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        wrong = await client.post(f"/drafts/{puzzle_id}/approve", data={"token": second})
        right = await client.post(f"/drafts/{puzzle_id}/approve", data={"token": first})
    assert (wrong.status_code, right.status_code) == (403, 303)


async def test__queue__is_in_slot_order_with_drafts_only_and_the_next_draft_follows_it(
    world: World, page: AsyncClient
) -> None:
    world.titles[0].name = f"Portal {HOSTILE}"
    no_slot = await world.draft(None)
    slot_9 = await world.draft(9, title=1)
    slot_8 = await world.draft(8, title=2)
    await world.draft(5, status=PuzzleStatus.APPROVED)  # not a draft: not listed, not counted

    queue = (await page.get("/")).text

    assert [int(i) for i in re.findall(r"/drafts/(\d+)", queue)] == [
        slot_8.id,
        slot_9.id,
        no_slot.id,
    ]
    assert "Drafts in review: 3" in queue
    assert "&lt;script&gt;" in queue
    assert "<script" not in queue
    assert "3 in review" in (await page.get(f"/drafts/{slot_8.id}")).text
    approve = await page.post(f"/drafts/{slot_8.id}/approve", data={"token": TOKEN})
    assert approve.headers["location"] == f"/drafts/{slot_9.id}"
    reject = await page.post(
        f"/drafts/{slot_9.id}/reject", data={"token": TOKEN, "reason": "never"}
    )
    assert reject.headers["location"] == f"/drafts/{no_slot.id}"


async def test__file__is_served_only_with_a_media_row_and_the_stored_type(
    world: World, page: AsyncClient
) -> None:
    puzzle = await world.draft(7)
    clip, no_file = media_key(puzzle.id, 6, "source"), media_key(puzzle.id, 2, "source")
    orphan = media_key(0, 0, "orphan")  # a SHA-256 in no row
    (world.media_dir / clip).write_bytes(b"RIFFxxxxWEBP")  # looks like WebP, the row says MP4
    (world.media_dir / orphan).write_bytes(b"left over from a rejected draft")

    served = await page.get(f"/files/{clip}")

    assert (served.status_code, served.headers["content-type"]) == (200, "video/mp4")
    assert (await page.get(f"/files/{orphan}")).status_code == 404
    assert (await page.get(f"/files/{no_file}")).status_code == 404  # a row, but no file


def assert_security_headers(response: Response) -> None:
    csp = dict(
        part.strip().split(" ", 1)
        for part in response.headers["content-security-policy"].split(";")
    )
    assert csp["default-src"] == "'self'"
    assert (csp["frame-ancestors"], csp["form-action"], csp["base-uri"]) == (
        "'none'",
        "'self'",
        "'none'",
    )
    assert "unsafe" not in response.headers["content-security-policy"]
    for name, value in {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "same-origin",
        "cache-control": "no-store",
    }.items():
        assert response.headers.get_list(name) == [value]


async def test__every_response__has_the_security_headers_and_the_page_has_no_script_or_style(
    world: World, page: AsyncClient
) -> None:
    puzzle = await world.draft(7)
    key = media_key(puzzle.id, 1, "source")
    (world.media_dir / key).write_bytes(b"x")
    page.headers.pop("Origin")

    responses = {
        "queue": await page.get("/"),
        "draft": await page.get(f"/drafts/{puzzle.id}"),
        "file": await page.get(f"/files/{key}"),
        "stylesheet": await page.get("/review.css"),
        "foreign host": await page.get("/", headers={"Host": "evil.example"}),
        "no origin": await page.post(
            f"/drafts/{puzzle.id}/approve", headers={"Content-Type": FORM}
        ),
        "redirect": await page.post(
            f"/drafts/{puzzle.id}/approve",
            data={"token": TOKEN},
            headers={"Origin": ORIGIN},
        ),
    }

    for name, response in responses.items():
        try:
            assert_security_headers(response)
        except AssertionError as error:
            raise AssertionError(f"{name}: {error}") from error
    assert responses["stylesheet"].status_code == 200
    assert responses["stylesheet"].headers["content-type"].startswith("text/css")
    for name in ("queue", "draft"):
        markup = Markup(responses[name].text)
        assert {"rel": "stylesheet", "href": "/review.css"} in markup.find("link")
        assert not markup.find("script")
        assert not any("style" in attrs or "onclick" in attrs for _, attrs in markup.tags)


async def test__draft_page__shows_each_clue_as_the_player_sees_it(
    world: World, page: AsyncClient
) -> None:
    world.titles[0].year, world.titles[0].franchise = 2007, "Aperture"
    puzzle = await world.draft(7)
    other = await world.draft(8, title=1)
    await world.db.execute(  # the second draft's step 2 has no file
        delete(ClueMediaFile).where(
            ClueMediaFile.key == media_key(other.id, 2, "source"),
        )
    )
    await world.db.commit()

    markup = Markup((await page.get(f"/drafts/{puzzle.id}")).text)

    images = markup.find("img")
    assert [i["src"] for i in images] == [
        f"/files/{media_key(puzzle.id, step, 'source')}" for step in range(1, 6)
    ]
    assert {(i["width"], i["height"]) for i in images} == {("640", "360")}
    (video,) = markup.find("video")
    assert "muted" in video and "loop" in video and "autoplay" in video
    assert video["poster"] == f"/files/{media_key(puzzle.id, 6, 'poster')}"
    assert (video["width"], video["height"]) == ("640", "360")
    (source,) = markup.find("source")
    assert (source["src"], source["type"]) == (
        f"/files/{media_key(puzzle.id, 6, 'source')}",
        "video/mp4",
    )
    new_tab = [a for a in markup.find("a") if a.get("target") == "_blank"]
    assert len(new_tab) == 5 + 6  # the step images, the source links
    assert all("noopener" in (a["rel"] or "") for a in new_tab)
    sources = [a["href"] for a in new_tab if a["rel"] == "noopener noreferrer"]
    assert sources == [f"https://example.com/source/{i}" for i in range(1, 7)]
    assert "2007" in markup.text and "Aperture" in markup.text
    assert markup.text.count("no hint") == 1  # step 3
    assert len(markup.find("input")) - 2 == 5  # the checklist, apart from the two tokens
    # A clip made of the stills is not a defect, so the item does not ask the clip to add something.
    assert (
        "The clip plays (it may reuse the stills when the game has few screenshots)" in markup.text
    )
    assert "adds something" not in markup.text
    other_page = Markup((await page.get(f"/drafts/{other.id}")).text)
    assert other_page.text.count("no file") == 1
    assert len(other_page.find("img")) == 4


@pytest.mark.parametrize(
    ("url", "is_link"),
    [
        ("http://www.igdb.com/games/portal", True),
        ("https://www.igdb.com/games/portal?a=1&b=2", True),
        ("ftp://example.com/x", False),
        ("javascript:alert(1)", False),
        ("javascript://example.com/%0aalert(1)", False),
        ("data:text/html,<b>x</b>", False),
        ("//evil.example/x", False),
        ("/relative/path", False),
        ("https:///no-host", False),
        ("https:no-slashes", False),
        ("", False),
        (None, False),
    ],
)
def test__link_target__is_a_plain_http_url_only(url: str | None, is_link: bool) -> None:
    assert review_html.link_target(url) == (url if is_link else None)


def clue_view(**fields: object) -> review_html.ClueView:
    base: dict[str, object] = {
        "step": 1,
        "is_video": False,
        "hint_kind": None,
        "hint_value": None,
        "source_url": None,
        "source": review_html.FileView("k" * 64, "image/webp", 640, 360),
        "poster": None,
    }
    return review_html.ClueView(**{**base, **fields})  # type: ignore[arg-type]


def render_clue(clue: review_html.ClueView) -> Markup:
    draft = review_html.DraftView(1, 7, "Portal", 1999, None, [clue], 1)
    return Markup(review_html.draft_page(draft, TOKEN))


def test__source_link__is_escaped_and_a_non_link_is_text() -> None:
    quoted = 'https://igdb.example/x"onmouseover="alert(1)<b>'
    markup = render_clue(clue_view(source_url=quoted))
    (link,) = [a for a in markup.find("a") if a.get("rel") == "noopener noreferrer"]
    assert link == {"href": quoted, "target": "_blank", "rel": "noopener noreferrer"}

    markup = render_clue(clue_view(source_url="ftp://x.example/<script>alert(1)</script>"))
    assert not markup.find("script")
    assert "source: ftp://x.example/<script>alert(1)</script>" in markup.text


@pytest.mark.parametrize(
    ("kind", "value", "caption"),
    [
        ("critic_score", "85", "Critic score: 85"),
        ("platform", "Switch", "Platform: Switch"),
        ("genre", "Puzzle", "Genre: Puzzle"),
        ("year", "2007", "Release year: 2007"),
        ("developer", "Valve", "Developer: Valve"),
        ("year", None, "no hint"),
        (None, "2007", "no hint"),
    ],
)
def test__hint__is_shown_with_its_label_or_as_no_hint(
    kind: str | None, value: str | None, caption: str
) -> None:
    text = render_clue(clue_view(hint_kind=kind, hint_value=value)).text
    assert caption in text
    if caption == "no hint":
        assert "None" not in text and "2007" not in text


async def test__post__a_refused_action_answers_409_and_a_missing_draft_404(
    world: World, page: AsyncClient
) -> None:
    approved_id = (await world.draft(7, status=PuzzleStatus.APPROVED)).id  # read once: rollbacks

    for path in ("approve", "reject"):
        form = {"token": TOKEN, "reason": "never"}
        assert (await page.post(f"/drafts/{approved_id}/{path}", data=form)).status_code == 409
        assert (
            await page.post(f"/drafts/{approved_id + 999}/{path}", data=form)
        ).status_code == 404

    assert (await page.get(f"/drafts/{approved_id}")).status_code == 404  # not a draft
    assert (await page.get(f"/drafts/{approved_id + 999}")).status_code == 404
    assert await world.status_of(approved_id) == PuzzleStatus.APPROVED
    assert await world.count(AnswerExclusion) == 0


async def test__reject__image_ids_are_listed_once_in_step_order(world: World) -> None:
    image_ids = [["c"], ["b", "c"], ["a"], ["a", "e"], ["d"], ["z", "y", "x", "z"]]
    puzzle = await world.draft(7, image_ids=image_ids)

    await actions.reject(world.db, puzzle.id, ExclusionReason.REBUILD)

    (row,) = (await world.db.execute(select(AnswerExclusion))).scalars()
    assert row.image_ids == ["c", "b", "a", "e", "d", "z", "y", "x"]


async def test__reject_never__leaves_another_variants_rebuild_rows_alone(world: World) -> None:
    await add_exclusion(
        world.db,
        game_id=world.game_id,
        variant="other",
        title_id=world.titles[0].id,
        reason=ExclusionReason.REBUILD,
        build_slot=7,
        image_ids=["a"],
    )
    puzzle = await world.draft(7)  # the default variant, same slot, same title

    await actions.reject(world.db, puzzle.id, ExclusionReason.NEVER)

    rows = await list_exclusions(world.db, world.game_id)
    assert sorted((r.variant, r.reason, r.build_slot) for r in rows) == [
        ("default", ExclusionReason.NEVER, None),
        ("other", ExclusionReason.REBUILD, 7),
    ]
