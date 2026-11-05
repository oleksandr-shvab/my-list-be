import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException, RangeNotSatisfiableError
from app.core.request_id import REQUEST_ID_HEADER, new_request_id

logger = logging.getLogger(__name__)


class FieldError(BaseModel):
    field: str = Field(description="Request field name; empty if the error is not about one field")
    code: str = Field(description="Stable code, translated by the frontend")
    params: dict[str, Any] = Field(default_factory=dict)


class ProblemDetails(BaseModel):
    """The body of every error response (RFC 9457 plus extension members), see CONTRACTS.md."""

    type: str = "about:blank"
    status: int
    title: str = Field(description="English, for developers and logs only")
    code: str = Field(description="Stable code, translated by the frontend")
    params: dict[str, Any] = Field(default_factory=dict)
    request_id: str
    errors: list[FieldError] = Field(default_factory=list)


# Documented on every route (OpenAPI). 422 replaces FastAPI's default validation schema, which
# this API does not return.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ProblemDetails, "description": "Validation error"},
    500: {"model": ProblemDetails, "description": "Unexpected error"},
}

HTTP_STATUS_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "too_many_requests",
    503: "service_unavailable",
}

# Pydantic error types mapped to the stable codes the frontend translates.
VALIDATION_CODES = {
    "missing": "required",
    "string_too_short": "too_short",
    "string_too_long": "too_long",
    "string_type": "invalid_type",
    "int_type": "invalid_type",
    "list_type": "invalid_type",
    # A body that is not a JSON object.
    "model_attributes_type": "invalid_type",
    "value_error": "invalid_value",
    "json_invalid": "invalid_json",
}
REQUEST_PARTS = ("body", "path", "query", "header", "cookie")
VALIDATION_PARAMS = {
    "string_too_short": {"min": "min_length"},
    "string_too_long": {"max": "max_length"},
    # A list over its `max_length` (the guess body's `previous`).
    "too_long": {"max": "max_length"},
}


def _request_id(request: Request) -> str:
    return request.scope.get("state", {}).get("request_id") or new_request_id()


def problem_response(
    request: Request,
    status_code: int,
    code: str,
    title: str,
    params: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """An RFC 9457 problem-details body plus the extension members the frontend needs."""
    request_id = _request_id(request)
    body: dict[str, Any] = {
        "type": "about:blank",
        "status": status_code,
        "title": title,
        "code": code,
        "params": params or {},
        "request_id": request_id,
        "errors": errors or [],
    }
    response_headers = {
        REQUEST_ID_HEADER: request_id,
        "Cache-Control": "no-store",
        # No error body is ever read as anything but JSON, whatever route it came from.
        "X-Content-Type-Options": "nosniff",
        **(headers or {}),
    }
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=response_headers,
        media_type="application/problem+json",
    )


def field_from_location(location: Sequence[str | int]) -> str:
    """The request field name of a validation error. The first part of `loc` says where the value
    came from (`body`, `path`, `query`, `header`, `cookie`); it is dropped, and a later part with the same name stays."""
    parts = list(location)
    if parts and parts[0] in REQUEST_PARTS:
        parts = parts[1:]
    return ".".join(str(part) for part in parts)


def _validation_error_items(exc: RequestValidationError) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for error in exc.errors():
        context = dict(error.get("ctx") or {})
        # Errors raised by our own validators carry the form field they belong to.
        field = context.pop("field", None)
        if field is None:
            field = field_from_location(error["loc"])
        error_type = error["type"]
        code = VALIDATION_CODES.get(error_type, error_type)
        params = {
            name: context[key]
            for name, key in VALIDATION_PARAMS.get(error_type, {}).items()
            if key in context
        }
        items.append({"field": field, "code": code, "params": params})
    return items


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    if exc.status_code == 500:
        logger.error(
            "Server error on %s %s (request %s): %s",
            request.method,
            request.url.path,
            _request_id(request),
            exc,
            exc_info=exc,
        )
    return problem_response(request, exc.status_code, exc.code, exc.detail, params=exc.params)


async def range_not_satisfiable_handler(
    request: Request, exc: RangeNotSatisfiableError
) -> JSONResponse:
    # A 416 says how big the file is (RFC 9110 section 15.5.17).
    return problem_response(
        request,
        exc.status_code,
        exc.code,
        exc.detail,
        headers={"Content-Range": f"bytes */{exc.size}"},
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Never echo the submitted values back (they can be passwords); only codes and field names.
    return problem_response(
        request,
        422,
        "validation_error",
        "Request validation failed",
        errors=_validation_error_items(exc),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = HTTP_STATUS_CODES.get(exc.status_code, "http_error")
    return problem_response(
        request, exc.status_code, code, str(exc.detail), headers=dict(exc.headers or {})
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id(request)
    logger.error(
        "Unhandled exception on %s %s (request %s)",
        request.method,
        request.url.path,
        request_id,
        exc_info=exc,
    )
    return problem_response(request, 500, "internal_error", "Internal server error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppException, app_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RangeNotSatisfiableError, range_not_satisfiable_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
