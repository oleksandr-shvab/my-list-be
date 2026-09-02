class AppException(Exception):
    status_code: int = 500
    detail: str = "Internal server error"

    def __init__(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class EmailAlreadyRegisteredError(AppException):
    status_code = 409
    detail = "Email already registered"


class InvalidCredentialsError(AppException):
    status_code = 401
    detail = "Invalid email or password"


class NotAuthenticatedError(AppException):
    status_code = 401
    detail = "Not authenticated"


class InvalidOrExpiredResetTokenError(AppException):
    status_code = 400
    detail = "Invalid or expired reset token"
