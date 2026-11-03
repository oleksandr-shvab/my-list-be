from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import serialize_origin, settings
from app.core.errors import problem_response
from app.core.exceptions import AppException, CsrfOriginMismatchError, UnsupportedMediaTypeError

UNCHECKED_METHODS = {"GET", "HEAD", "OPTIONS"}
JSON_BODY_METHODS = {"POST", "PUT", "PATCH"}
JSON_MEDIA_TYPE = "application/json"


def _request_origin(headers: Headers) -> str | None:
    """The origin the request came from: `Origin`, or the origin of `Referer` if `Origin` is
    absent. None if there is neither, the header used appears twice, or its value is `null` or
    not an http(s) URL. Only the header used must be unique."""
    origins = headers.getlist("origin")
    if origins:
        return serialize_origin(origins[0], strict=True) if len(origins) == 1 else None
    referers = headers.getlist("referer")
    return serialize_origin(referers[0]) if len(referers) == 1 else None


def _has_body(headers: Headers) -> bool:
    """Any `Transfer-Encoding` means a body, whatever `Content-Length` says. An unreadable or
    negative `Content-Length` also counts as a body."""
    if "transfer-encoding" in headers:
        return True
    content_length = headers.get("content-length")
    if content_length is None:
        return False
    try:
        return int(content_length) != 0
    except ValueError:
        return True


def _media_type(headers: Headers) -> str:
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


class CsrfOriginMiddleware:
    """Checks every method except GET, HEAD and OPTIONS before routing, dependencies or body parsing.

    The request's origin (`Origin`, else the origin of `Referer`) must be exactly one of
    `settings.allowed_origins`, else 403 `csrf_origin_mismatch`. A POST, PUT or PATCH that carries a
    body must send `application/json`, else 415 `unsupported_media_type`. It reads only headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in UNCHECKED_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        error: AppException | None = None
        if _request_origin(headers) not in settings.allowed_origins:
            error = CsrfOriginMismatchError()
        elif (
            scope["method"] in JSON_BODY_METHODS
            and _has_body(headers)
            and _media_type(headers) != JSON_MEDIA_TYPE
        ):
            error = UnsupportedMediaTypeError()

        if error is None:
            await self.app(scope, receive, send)
            return
        response = problem_response(Request(scope), error.status_code, error.code, error.detail)
        await response(scope, receive, send)
