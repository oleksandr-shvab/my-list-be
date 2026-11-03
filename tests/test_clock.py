import datetime as dt

import pytest

from app.core.clock import get_now, utc_today


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
    # 01:30 on 4 November at UTC+2 is 23:30 on 3 November in UTC.
    other_zone = dt.datetime(2026, 11, 4, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    assert utc_today(other_zone) == dt.date(2026, 11, 3)


def test__utc_today__naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_today(dt.datetime(2026, 11, 3, 12, 0, 0))  # noqa: DTZ001 (naive on purpose)
