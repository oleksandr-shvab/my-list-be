import datetime as dt

from pydantic import BaseModel


class ClockRead(BaseModel):
    """The server's clock. The client may use it only for display (AC-69)."""

    now: dt.datetime
    today: dt.date
    next_reset: dt.datetime
