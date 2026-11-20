"""The local review page (SPEC-04 AC-47 to AC-54, R-17): a small Starlette app, not an API route.

It is never mounted in `app/main.py`, so the API and the OpenAPI snapshot do not know it. It runs
only in the `local` environment (`create_review_app` refuses to be built in any other) and takes
requests only from itself:

- every request needs a `Host` of `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding: a page on
  another name that resolves to this machine would otherwise read the answers);
- every unsafe request (POST) needs an `Origin` that is the page's own, and a form `token` that was
  made when the process started (any web page in the owner's browser could POST to a local port).

Every response, a 500 included, has a strict `Content-Security-Policy` (no script), `nosniff`,
`X-Frame-Options` and `no-store`. Every text from IGDB goes through `html.esc`.
"""

import datetime as dt
import hmac
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from urllib.parse import parse_qs

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.content.review import actions, html
from app.content.review.actions import ReviewError
from app.content.target import LOCAL_TARGET, check_target
from app.core.clock import get_now
from app.core.config import Settings
from app.crud.puzzle import (
    count_in_review,
    first_in_review_id,
    get_media_file_by_key,
    get_puzzle_with_clues,
    list_in_review_puzzles,
)
from app.games.enums import ClueRole, ExclusionReason, MediaType
from app.media.signing import is_media_key
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleStatus

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

LOCAL_HOST_NAMES = ("127.0.0.1", "localhost")
MAX_FORM_BYTES = 2048
MAX_FORM_FIELDS = 4
REASONS = {reason.value: reason for reason in ExclusionReason}

# No script, nothing from another origin. The rest closes what `default-src` does not cover.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self'; media-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Not `no-referrer`: with it a browser sends `Origin: null` on the page's own form POSTs, which
    # the middleware refuses. `same-origin` keeps the real Origin for those and sends no referrer
    # anywhere else.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


MAX_PUZZLE_ID = 2_147_483_647  # the largest integer key: a bigger id would be a database error


def is_puzzle_id(value: int) -> bool:
    return 1 <= value <= MAX_PUZZLE_ID


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset(f"{name}:{port}" for name in LOCAL_HOST_NAMES)


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset(f"http://{host}" for host in allowed_hosts(port))


def _single_header(scope: Scope, name: bytes) -> str | None:
    """The value of a header that must appear exactly once, else None."""
    values = [value for key, value in scope["headers"] if key == name]
    return values[0].decode("latin-1") if len(values) == 1 else None


class LocalOnlyMiddleware:
    """Checks `Host` on every request and `Origin` on every unsafe one, before routing, and sets
    the security headers on every response, the refusals included. The form token is checked by the
    handler, because it is in the body."""

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.hosts = allowed_hosts(port)
        self.origins = allowed_origins(port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key.lower().encode(), value.encode())
                    for key, value in SECURITY_HEADERS.items()
                ]
                kept = [(k, v) for k, v in message["headers"] if k not in dict(headers)]
                message = {**message, "headers": kept + headers}
            await send(message)

        refusal = self._refusal(scope)
        if refusal is not None:
            response = PlainTextResponse(f"Refused: {refusal}", status_code=403)
            await response(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)

    def _refusal(self, scope: Scope) -> str | None:
        host = _single_header(scope, b"host")
        if host is None or host.lower() not in self.hosts:
            return "this page answers only on 127.0.0.1 or localhost, with its own port"
        if scope["method"] not in SAFE_METHODS:
            origin = _single_header(scope, b"origin")
            if origin is None or origin.lower() not in self.origins:
                return "the request does not come from this page"
        return None


class FormError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def read_form(request: Request) -> dict[str, str]:
    """A small urlencoded form, read with a size cap. (Starlette's `request.form()` needs
    python-multipart, which this app does not need.) The first value of a repeated field wins."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise FormError(415, "the form must be application/x-www-form-urlencoded")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise FormError(413, "the form is too large")
    try:
        parsed = parse_qs(
            body.decode("utf-8", errors="replace"),
            keep_blank_values=True,
            max_num_fields=MAX_FORM_FIELDS,
        )
    except ValueError:
        raise FormError(400, "the form has too many fields") from None
    return {name: values[0] for name, values in parsed.items()}


def is_token_valid(form: Mapping[str, str], token: str) -> bool:
    return hmac.compare_digest(form.get("token", "").encode(), token.encode())


def _file_view(file: ClueMediaFile | None) -> html.FileView | None:
    if file is None:
        return None
    return html.FileView(
        key=file.key, content_type=file.content_type, width=file.width, height=file.height
    )


def _draft_view(puzzle: Puzzle, queue_size: int) -> html.DraftView:
    clues = []
    for clue in puzzle.clues:
        sources = [file for file in clue.media_files if file.role == ClueRole.SOURCE]
        posters = [file for file in clue.media_files if file.role == ClueRole.POSTER]
        clues.append(
            html.ClueView(
                step=clue.step,
                is_video=clue.media_type == MediaType.VIDEO,
                hint_kind=clue.hint_kind,
                hint_value=clue.hint_value,
                source_url=clue.source_url,
                source=_file_view(sources[0] if sources else None),
                poster=_file_view(posters[0] if posters else None),
            )
        )
    title = puzzle.answer_title
    per_step = {clue.step: clue.source_image_ids or [] for clue in puzzle.clues}
    return html.DraftView(
        puzzle_id=puzzle.id,
        build_slot=puzzle.build_slot,
        answer_name=title.name,
        answer_year=title.year,
        answer_franchise=title.franchise,
        clues=clues,
        queue_size=queue_size,
        can_rebuild=actions.can_rebuild(i for ids in per_step.values() for i in ids),
        clip_from_stills=actions.clip_is_from_stills(per_step),
    )


def create_review_app(
    *,
    session_factory: SessionFactory,
    media_dir: Path,
    port: int,
    environ: Mapping[str, str] | None = None,
    app_settings: Settings | None = None,
    clock: Callable[[], dt.datetime] = get_now,
    token: str | None = None,
) -> Starlette:
    """Build the page, or raise `TargetRefusedError`: it is local only (AC-47), reads the
    `ENVIRONMENT` variable itself, wants a database on this machine and local media storage, and
    does not run in CI. `token` is for tests; a run makes its own."""
    check_target(
        LOCAL_TARGET, None, environ=environ, app_settings=app_settings, needs_local_storage=True
    )
    form_token = token or secrets.token_urlsafe(32)

    def page_response(content: str, status_code: int = 200) -> HTMLResponse:
        return HTMLResponse(content, status_code=status_code)

    async def next_location(db: AsyncSession) -> str:
        next_id = await first_in_review_id(db)
        return f"/drafts/{next_id}" if next_id is not None else "/"

    async def queue(request: Request) -> Response:
        async with session_factory() as db:
            puzzles = await list_in_review_puzzles(db)
        items = [html.QueueItem(p.id, p.build_slot, p.answer_title.name) for p in puzzles]
        return page_response(html.queue_page(items))

    async def draft(request: Request) -> Response:
        puzzle_id = request.path_params["puzzle_id"]
        if not is_puzzle_id(puzzle_id):
            return page_response(html.message_page("This draft is not in review"), 404)
        async with session_factory() as db:
            puzzle = await get_puzzle_with_clues(db, puzzle_id)
            if puzzle is None or puzzle.status != PuzzleStatus.IN_REVIEW:
                return page_response(html.message_page("This draft is not in review"), 404)
            queue_size = await count_in_review(db)
            view = _draft_view(puzzle, queue_size)
        return page_response(html.draft_page(view, form_token))

    async def serve_file(request: Request) -> Response:
        key = request.path_params["key"]
        if not is_media_key(key):
            return PlainTextResponse("Not found", status_code=404)
        async with session_factory() as db:
            media_file = await get_media_file_by_key(db, key)
        path = media_dir / key
        if media_file is None or not path.is_file():
            return PlainTextResponse("Not found", status_code=404)
        # The type comes from the media row, never from the bytes or the name.
        return FileResponse(path, media_type=media_file.content_type)

    async def post_action(
        request: Request, run: Callable[[AsyncSession, int, Mapping[str, str]], Awaitable[None]]
    ) -> Response:
        """The one path of every POST: the token, then the action, then a redirect to the next
        draft (post, redirect, get). The `Host` and `Origin` were checked before routing."""
        try:
            form = await read_form(request)
            if not is_token_valid(form, form_token):
                raise FormError(403, "the form token is missing or wrong")
            puzzle_id = request.path_params["puzzle_id"]
            if not is_puzzle_id(puzzle_id):
                raise actions.PuzzleNotFoundError(f"no puzzle {puzzle_id}")
            async with session_factory() as db:
                await run(db, puzzle_id, form)
                location = await next_location(db)
        except FormError as error:
            return PlainTextResponse(f"Refused: {error.message}", status_code=error.status_code)
        except actions.PuzzleNotFoundError as error:
            return page_response(html.message_page(str(error)), 404)
        except ReviewError as error:
            return page_response(html.message_page(str(error)), 409)
        return RedirectResponse(location, status_code=303)

    async def approve(request: Request) -> Response:
        async def run(db: AsyncSession, puzzle_id: int, form: Mapping[str, str]) -> None:
            await actions.approve(db, puzzle_id, clock())

        return await post_action(request, run)

    async def reject(request: Request) -> Response:
        async def run(db: AsyncSession, puzzle_id: int, form: Mapping[str, str]) -> None:
            reason = REASONS.get(form.get("reason", ""))
            if reason is None:
                raise FormError(422, "the reason must be rebuild or never")
            await actions.reject(db, puzzle_id, reason)

        return await post_action(request, run)

    async def stylesheet_route(request: Request) -> Response:
        return Response(html.STYLESHEET, media_type="text/css")

    async def server_error(request: Request, error: Exception) -> Response:
        """An unhandled error. Starlette's error middleware sits outside `LocalOnlyMiddleware`, so
        this answer carries the security headers itself. It says nothing about the cause."""
        return PlainTextResponse("Internal Server Error", status_code=500, headers=SECURITY_HEADERS)

    return Starlette(
        exception_handlers={Exception: server_error},
        routes=[
            Route("/", queue),
            Route("/review.css", stylesheet_route),
            Route("/drafts/{puzzle_id:int}", draft),
            Route("/files/{key}", serve_file),
            Route("/drafts/{puzzle_id:int}/approve", approve, methods=["POST"]),
            Route("/drafts/{puzzle_id:int}/reject", reject, methods=["POST"]),
        ],
        middleware=[Middleware(LocalOnlyMiddleware, port=port)],
    )
