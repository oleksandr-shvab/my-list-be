"""The seed's placeholder files (AC-74b, AC-82): the manifest matches the files and stays inside
the builder's limits, and the seed stores them under their SHA-256."""

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.media.signing import is_media_key
from app.models.puzzle import ClueMediaFile

SEED_MEDIA = Path(__file__).resolve().parent.parent / "scripts" / "seed_media"
MANIFEST = json.loads((SEED_MEDIA / "manifest.json").read_text())

# AC-74b: video at most 1280x720, 4 MB, 8 to 15 seconds; image and poster at most 1920x1080, 1 MB.
# Each dimension is checked alone: a 720x1280 clip passes a pair comparison but is not allowed.
MB = 1024 * 1024
VIDEO_MAX = (1280, 720)
IMAGE_MAX = (1920, 1080)


def broken_limits(
    content_type: str, width: int, height: int, byte_size: int, duration_ms: int | None
) -> list[str]:
    broken: list[str] = []
    if content_type == "video/mp4":
        max_width, max_height, max_bytes = (*VIDEO_MAX, 4 * MB)
        if duration_ms is None or not 8_000 <= duration_ms <= 15_000:
            broken.append("duration")
    elif content_type == "image/webp":
        max_width, max_height, max_bytes = (*IMAGE_MAX, 1 * MB)
        if duration_ms is not None:
            broken.append("duration")
    else:
        return ["content_type"]
    if width > max_width:
        broken.append("width")
    if height > max_height:
        broken.append("height")
    if byte_size > max_bytes:
        broken.append("byte_size")
    return broken


@pytest.mark.parametrize(
    ("content_type", "width", "height", "byte_size", "duration_ms", "expected"),
    [
        ("video/mp4", 1280, 720, 4 * MB, 8_000, []),
        ("video/mp4", 640, 360, 1000, 15_000, []),
        ("video/mp4", 720, 1280, 1000, 12_000, ["height"]),  # Portrait: both fit as a pair.
        ("video/mp4", 1281, 720, 1000, 12_000, ["width"]),
        ("video/mp4", 1280, 721, 1000, 12_000, ["height"]),
        ("video/mp4", 1920, 1080, 1000, 12_000, ["width", "height"]),
        ("video/mp4", 640, 360, 4 * MB + 1, 12_000, ["byte_size"]),
        ("video/mp4", 640, 360, 1000, 7_999, ["duration"]),
        ("video/mp4", 640, 360, 1000, 15_001, ["duration"]),
        ("video/mp4", 640, 360, 1000, None, ["duration"]),
        ("video/webm", 640, 360, 1000, 12_000, ["content_type"]),
        ("image/webp", 1920, 1080, 1 * MB, None, []),
        ("image/webp", 1080, 1920, 1000, None, ["height"]),
        ("image/webp", 1921, 100, 1000, None, ["width"]),
        ("image/webp", 100, 1081, 1000, None, ["height"]),
        ("image/webp", 640, 360, 1 * MB + 1, None, ["byte_size"]),
        ("image/webp", 640, 360, 1000, 12_000, ["duration"]),
        ("image/png", 640, 360, 1000, None, ["content_type"]),
    ],
)
def test__broken_limits__each_dimension_is_checked_on_its_own(
    content_type: str,
    width: int,
    height: int,
    byte_size: int,
    duration_ms: int | None,
    expected: list[str],
) -> None:
    assert broken_limits(content_type, width, height, byte_size, duration_ms) == expected


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test__seed_media__file_is_what_the_manifest_says_and_within_the_ac74b_limits(
    name: str,
) -> None:
    spec = MANIFEST[name]
    data = (SEED_MEDIA / name).read_bytes()

    if spec["content_type"] == "video/mp4":
        assert data[4:8] == b"ftyp"
    else:
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert (
        broken_limits(
            spec["content_type"], spec["width"], spec["height"], len(data), spec.get("duration_ms")
        )
        == []
    )


async def test__seed_media__are_stored_under_their_sha256_and_every_clue_has_files(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    spec = importlib.util.spec_from_file_location("seed_dev", SEED_MEDIA.parent / "seed_dev.py")
    assert spec is not None and spec.loader is not None
    seed_dev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_dev)

    await seed_dev.seed(db_session, dt.date(2026, 11, 3), media_dir=tmp_path)

    rows = (await db_session.execute(select(ClueMediaFile))).scalars().all()
    assert len(rows) == 6 * 7
    for row in rows:
        assert is_media_key(row.key)
        stored = (tmp_path / row.key).read_bytes()
        assert hashlib.sha256(stored).hexdigest() == row.key
        assert row.byte_size == len(stored)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted({row.key for row in rows})
