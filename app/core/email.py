import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_password_reset_email(email: str, reset_link: str) -> None:
    # No email provider is wired up yet. In local development the link is logged so it can be
    # copied; anywhere else it must never reach the logs (it is an account-takeover token).
    if settings.ENVIRONMENT in ("local", "test"):
        logger.info("[email stub] Password reset link for %s: %s", email, reset_link)
    else:
        logger.warning("Password reset requested but no email sender is configured")
