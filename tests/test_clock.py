import datetime as dt

import pytest
from httpx import AsyncClient

from app.core.clock import get_now, next_utc_midnight, utc_today
from app.core.db import get_db
from app.main import app


def test__get_now__is_timezone_aware_utc() -> None:
    before = dt.datetime.now(dt.UTC)
    now = get_now()
    after = dt.datetime.now(dt.UTC)

    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)
    assert before <= now <= after


def test__utc_today__turns_over_one_second_after_23_59_59() -> None:
    last_second = dt.datetime(2026, 11, 3, 23, 59, 59, tzinfo=dt.UTC)

    assert utc_today(last_second) == dt.date(2026, 11, 3)
    assert utc_today(last_second + dt.timedelta(seconds=1)) == dt.date(2026, 11, 4)


def test__utc_today__converts_another_timezone_to_the_utc_date() -> None:
    other_zone = dt.datetime(2026, 11, 4, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    assert utc_today(other_zone) == dt.date(2026, 11, 3)


def test__utc_today__naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_today(dt.datetime(2026, 11, 3, 12, 0, 0))  # noqa: DTZ001 (naive on purpose)


@pytest.mark.parametrize(
    ("now", "today", "next_reset"),
    [
        (
            dt.datetime(2026, 11, 3, 23, 59, 59, tzinfo=dt.UTC),
            "2026-11-03",
            "2026-11-04T00:00:00Z",
        ),
        (
            dt.datetime(2026, 11, 4, 0, 0, 0, tzinfo=dt.UTC),
            "2026-11-04",
            "2026-11-05T00:00:00Z",
        ),
        (
            dt.datetime(2026, 12, 31, 23, 59, 59, 500000, tzinfo=dt.UTC),
            "2026-12-31",
            "2027-01-01T00:00:00Z",
        ),
    ],
)
async def test__get_clock__fixed_clock_gives_exact_values(
    client: AsyncClient, now: dt.datetime, today: str, next_reset: str
) -> None:
    app.dependency_overrides[get_now] = lambda: now

    response = await client.get("/v1/clock")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"now", "today", "next_reset"}
    assert dt.datetime.fromisoformat(body["now"]) == now
    assert body["today"] == today
    assert body["next_reset"] == next_reset
    assert response.headers["Cache-Control"] == "no-store"


async def test__get_clock__answers_200_when_the_database_dependency_raises(
    client: AsyncClient,
) -> None:
    async def broken_get_db() -> None:
        raise RuntimeError("the clock route must not open a database session")

    app.dependency_overrides[get_db] = broken_get_db

    response = await client.get("/v1/clock")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test__next_utc_midnight__is_the_following_day_at_exactly_midnight() -> None:
    other_zone = dt.datetime(2026, 11, 4, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    assert next_utc_midnight(other_zone) == dt.datetime(2026, 11, 4, tzinfo=dt.UTC)
    assert next_utc_midnight(dt.datetime(2026, 11, 4, tzinfo=dt.UTC)) == dt.datetime(
        2026, 11, 5, tzinfo=dt.UTC
    )
