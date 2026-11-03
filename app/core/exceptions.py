from typing import Any


class AppException(Exception):
    """A handled error. `code` is the stable machine-readable name the frontend translates;
    `detail` is English, for developers and logs only."""

    status_code: int = 500
    code: str = "internal_error"
    detail: str = "Internal server error"

    def __init__(self, detail: str | None = None, params: dict[str, Any] | None = None) -> None:
        if detail is not None:
            self.detail = detail
        self.params = params or {}
        super().__init__(self.detail)


class EmailAlreadyRegisteredError(AppException):
    status_code = 409
    code = "email_already_registered"
    detail = "Email already registered"


class InvalidCredentialsError(AppException):
    status_code = 401
    code = "invalid_credentials"
    detail = "Invalid email or password"


class NotAuthenticatedError(AppException):
    status_code = 401
    code = "not_authenticated"
    detail = "Not authenticated"


class InvalidOrExpiredResetTokenError(AppException):
    status_code = 400
    code = "invalid_or_expired_reset_token"
    detail = "Invalid or expired reset token"


class IncorrectPasswordError(AppException):
    status_code = 400
    code = "incorrect_password"
    detail = "Incorrect password"


class ServiceUnavailableError(AppException):
    status_code = 503
    code = "service_unavailable"
    detail = "Service unavailable"


class CsrfOriginMismatchError(AppException):
    status_code = 403
    code = "csrf_origin_mismatch"
    detail = "Request origin is not allowed"


class UnsupportedMediaTypeError(AppException):
    status_code = 415
    code = "unsupported_media_type"
    detail = "Request body must be application/json"


class GameNotFoundError(AppException):
    status_code = 404
    code = "game_not_found"
    detail = "Game not found"


class VariantNotFoundError(AppException):
    status_code = 404
    code = "variant_not_found"
    detail = "Variant not found"


class PuzzleNotFoundError(AppException):
    """Raised for every puzzle that is missing or not visible (draft, not scheduled, future date,
    before launch), always with this one detail, so the bodies are identical (AC-17)."""

    status_code = 404
    code = "puzzle_not_found"
    detail = "Puzzle not found"


class PuzzleDataError(AppException):
    """A scheduled puzzle that breaks a rule of its game (for example a missing step). Not a
    client error: it is a 500 `internal_error`, and the puzzle builder must have caught it before
    the date. The response says nothing about the cause; `reason` is for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason

    def __str__(self) -> str:
        return self.reason
