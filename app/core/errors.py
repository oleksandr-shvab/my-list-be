import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException
from app.core.request_id import REQUEST_ID_HEADER, new_request_id

logger = logging.getLogger(__name__)

HTTP_STATUS_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    429: "too_many_requests",
    503: "service_unavailable",
}

# Pydantic error types mapped to the stable codes the frontend translates.
VALIDATION_CODES = {
    "missing": "required",
    "string_too_short": "too_short",
    "string_too_long": "too_long",
    "string_type": "invalid_type",
    "value_error": "invalid_value",
    "json_invalid": "invalid_json",
}
VALIDATION_PARAMS = {
    "string_too_short": {"min": "min_length"},
    "string_too_long": {"max": "max_length"},
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
    response_headers = {REQUEST_ID_HEADER: request_id, **(headers or {})}
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=response_headers,
        media_type="application/problem+json",
    )


def _validation_error_items(exc: RequestValidationError) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for error in exc.errors():
        context = dict(error.get("ctx") or {})
        # Errors raised by our own validators carry the form field they belong to.
        field = context.pop("field", None)
        if field is None:
            location = [str(part) for part in error["loc"] if part != "body"]
            field = ".".join(location)
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
    return problem_response(request, exc.status_code, exc.code, exc.detail, params=exc.params)


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
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
