import logging
import sys

from app.core.request_id import request_id_ctx

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def configure_logging() -> None:
    """Plain text to stdout for now, with the request id on every line. Never log request
    bodies, cookies, reset tokens or email addresses outside local development."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RequestIdFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
