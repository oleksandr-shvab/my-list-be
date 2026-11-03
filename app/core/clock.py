import datetime as dt
from typing import Annotated

from fastapi import Depends


def get_now() -> dt.datetime:
    """The current time, timezone aware, in UTC. A dependency, so tests override it and
    services take `now` as a parameter (R-7)."""
    return dt.datetime.now(dt.UTC)


def utc_today(now: dt.datetime) -> dt.date:
    """The UTC calendar date of `now`: the server's "today" for every game rule.

    A naive datetime is refused: `astimezone` would read it as local time."""
    if now.tzinfo is None:
        raise ValueError("utc_today needs a timezone-aware datetime")
    return now.astimezone(dt.UTC).date()


NowDep = Annotated[dt.datetime, Depends(get_now)]
