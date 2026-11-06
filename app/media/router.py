"""The local media route (AC-72, AC-72b).

With `MEDIA_STORAGE=s3` it answers `invalid_media_link` to everything. It stays out of OpenAPI
(R-17): the frontend takes links from the puzzle view.
"""

from fastapi import APIRouter, Request
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from app.api.deps import FunctionScopedSessionDep, MediaStorageDep
from app.core.clock import NowDep
from app.core.exceptions import InvalidMediaLinkError, RangeNotSatisfiableError
from app.media import service
from app.media.ranges import RangeKind, classify_range
from app.media.storage import LocalMediaStorage

router = APIRouter()

NOSNIFF = {"X-Content-Type-Options": "nosniff"}


class WholeFileResponse(FileResponse):
    """Ignores `Range` and `If-Range`: several ranges get the whole file (R-18), where Starlette
    would send `multipart/byteranges`."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = [
            (name, value) for name, value in scope["headers"] if name not in (b"range", b"if-range")
        ]
        await super().__call__({**scope, "headers": headers}, receive, send)


@router.api_route("/media/{key}", methods=["GET", "HEAD"], include_in_schema=False)
async def get_media(
    key: str,
    request: Request,
    db: FunctionScopedSessionDep,
    now: NowDep,
    storage: MediaStorageDep,
    exp: str = "",
    sig: str = "",
) -> FileResponse:
    if not isinstance(storage, LocalMediaStorage):
        raise InvalidMediaLinkError()
    media = await service.open_media(
        db, storage=storage, key=key, expires=exp, signature=sig, now=now
    )
    range_kind = classify_range(request.headers.get("range"), media.stat_result.st_size)
    if range_kind == RangeKind.NOT_SATISFIABLE:
        raise RangeNotSatisfiableError(media.stat_result.st_size)

    response_class = FileResponse if range_kind == RangeKind.SINGLE else WholeFileResponse
    return response_class(
        media.path,
        media_type=media.content_type,
        stat_result=media.stat_result,
        headers={**NOSNIFF, "Cache-Control": f"private, max-age={media.remaining_seconds}"},
    )
