from typing import Any


class AppException(Exception):
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
    """One detail for every missing or hidden puzzle, so the bodies are identical (AC-17)."""

    status_code = 404
    code = "puzzle_not_found"
    detail = "Puzzle not found"


class TitleNotFoundError(AppException):
    status_code = 422
    code = "title_not_found"
    detail = "Title not found"


class DuplicateGuessError(AppException):
    status_code = 422
    code = "duplicate_guess"
    detail = "Title already guessed"


class InvalidGuessHistoryError(AppException):
    status_code = 422
    code = "invalid_guess_history"
    detail = "The earlier guesses are not a valid game"


class PuzzleAlreadyFinishedError(AppException):
    status_code = 409
    code = "puzzle_already_finished"
    detail = "The puzzle is already finished"


class InvalidMediaLinkError(AppException):
    """One answer for an expired link, a wrong signature and a bad key, so the response reveals
    nothing about the file (AC-73)."""

    status_code = 403
    code = "invalid_media_link"
    detail = "Invalid or expired media link"


class RangeNotSatisfiableError(AppException):
    """Only a `<video>` element sees it, so the frontend has no text for it (AC-72b)."""

    status_code = 416
    code = "range_not_satisfiable"
    detail = "Requested range not satisfiable"

    def __init__(self, size: int) -> None:
        super().__init__()
        self.size = size


class PuzzleDataError(AppException):
    """A 500, not a client error: the puzzle builder should have caught it before the date. The
    response hides the cause; `reason` is for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason

    def __str__(self) -> str:
        return self.reason
