import logging

logger = logging.getLogger(__name__)


async def send_password_reset_email(email: str, reset_link: str) -> None:
    logger.info(f"[email stub] Password reset link for {email}: {reset_link}")
