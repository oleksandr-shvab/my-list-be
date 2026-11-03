from fastapi import APIRouter, Response

from app.clock.schemas import ClockRead
from app.core.clock import NowDep, next_utc_midnight, utc_today

router = APIRouter()


@router.get("/clock", response_model=ClockRead)
async def get_clock(now: NowDep, response: Response) -> ClockRead:
    """No database and no session: only the clock dependency (AC-67)."""
    response.headers["Cache-Control"] = "no-store"
    return ClockRead(now=now, today=utc_today(now), next_reset=next_utc_midnight(now))
