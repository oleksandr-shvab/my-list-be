import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_password_reset_email(email: str, reset_link: str) -> None:
    # No provider yet. The link is an account-takeover token: log it only in local and test.
    if settings.ENVIRONMENT in ("local", "test"):
        logger.info("[email stub] Password reset link for %s: %s", email, reset_link)
    else:
        logger.warning("Password reset requested but no email sender is configured")
