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
