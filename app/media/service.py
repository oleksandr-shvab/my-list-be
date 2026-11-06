import datetime as dt
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_today
from app.core.exceptions import InvalidMediaLinkError, PuzzleDataError, PuzzleNotFoundError
from app.crud.clue_media import get_visible_media_file
from app.media.signing import is_media_key, is_valid_link
from app.media.storage import LocalMediaStorage

# A Unix time in seconds has 10 digits for the next few centuries; the cap keeps `int()` away from
# absurd input.
MAX_EXPIRY_DIGITS = 12


@dataclass(frozen=True)
class MediaFile:
    """A file the route may serve: the path, the stored content type, the file's stat result (for
    the size and the headers) and the seconds the link still lives."""

    path: Path
    content_type: str
    stat_result: os.stat_result
    remaining_seconds: int


def _parse_expiry(value: str) -> int | None:
    if not value.isascii() or not value.isdigit() or len(value) > MAX_EXPIRY_DIGITS:
        return None
    return int(value)


async def open_media(
    db: AsyncSession,
    *,
    storage: LocalMediaStorage,
    key: str,
    expires: str,
    signature: str,
    now: dt.datetime,
) -> MediaFile:
    """Check a signed link and find its file. The checks run in this order: the key's shape, the
    signature and the expiry (one `invalid_media_link` for all three, AC-73), then the visibility of
    the puzzle the file belongs to (`puzzle_not_found`, R-19). The signature is checked on every
    request, ranged or not (AC-72b)."""
    expires_at = _parse_expiry(expires)
    now_seconds = int(now.timestamp())
    if (
        not is_media_key(key)
        or expires_at is None
        or not is_valid_link(
            key, expires_at, signature, secret=storage.signing_key, now=now_seconds
        )
    ):
        raise InvalidMediaLinkError()

    media_file = await get_visible_media_file(db, key=key, today=utc_today(now))
    if media_file is None:
        raise PuzzleNotFoundError()

    path = storage.path_for(key)
    try:
        stat_result = await anyio.Path(path).stat()
    except FileNotFoundError as error:
        # A row with no file: a builder or deploy fault, not the client's. The log has the key.
        raise PuzzleDataError(f"Media file {key} is missing from the local storage") from error
    if not stat.S_ISREG(stat_result.st_mode):
        raise PuzzleDataError(f"Media file {key} is not a regular file")
    return MediaFile(
        path=path,
        content_type=media_file.content_type,
        stat_result=stat_result,
        remaining_seconds=expires_at - now_seconds,
    )
