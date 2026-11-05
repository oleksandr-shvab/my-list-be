"""Check that a presigned S3 link answers `Range` requests (AC-72c). Manual, not part of CI.

    MEDIA_STORAGE=s3 S3_ENDPOINT_URL=... S3_REGION=... S3_BUCKET=... \\
    S3_ACCESS_KEY_ID=... S3_SECRET_ACCESS_KEY=... uv run python scripts/check_s3_range.py

It uploads the seed clip to the bucket under its SHA-256, presigns a GET and asks for
`Range: bytes=0-1` and `Range: bytes=100-`. Each must answer 206 with the right `Content-Range`.
It then prints the link, so you can open it in a `<video>` in Chrome and Safari (AC-72c), and
deletes the object when you press Enter. The exit code is 1 when a range check fails. If it does,
production serves media through the backend's own route (`MEDIA_STORAGE=local`, AC-72b).
"""

import datetime as dt
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.media.storage import S3MediaStorage, build_media_storage

CLIP = ROOT / "scripts" / "seed_media" / "clip.mp4"
CONTENT_TYPE = "video/mp4"


def ranged_get(url: str, range_header: str) -> tuple[int, str | None]:
    """The status and `Content-Range` of a GET with `range_header`."""
    request = urllib.request.Request(url, headers={"Range": range_header})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("Content-Range")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Range")


def main() -> int:
    if settings.MEDIA_STORAGE != "s3":
        print("Refusing to run: MEDIA_STORAGE must be 's3' and the S3_* settings must be set.")
        return 1
    storage = build_media_storage()
    assert isinstance(storage, S3MediaStorage)

    data = CLIP.read_bytes()
    key = hashlib.sha256(data).hexdigest()
    size = len(data)
    storage.client.put_object(
        Bucket=storage.bucket, Key=key, Body=data, ContentType=CONTENT_TYPE, CacheControl="private"
    )
    print(f"Uploaded {CLIP.name} ({size} bytes) as {key}")
    try:
        url, _ = storage.signed_url(key, CONTENT_TYPE, dt.datetime.now(dt.UTC))
        expected = {
            "bytes=0-1": f"bytes 0-1/{size}",
            "bytes=100-": f"bytes 100-{size - 1}/{size}",
        }
        failed = False
        for range_header, content_range in expected.items():
            status, actual = ranged_get(url, range_header)
            is_ok = status == 206 and actual == content_range
            failed = failed or not is_ok
            print(
                f"{'ok  ' if is_ok else 'FAIL'} Range: {range_header} -> {status}, "
                f"Content-Range {actual!r} (want 206, {content_range!r})"
            )
        print(f"\nOpen this link in a <video> in Chrome and Safari:\n{url}\n")
        input("Press Enter to delete the object from the bucket. ")
        return 1 if failed else 0
    finally:
        storage.client.delete_object(Bucket=storage.bucket, Key=key)
        print(f"Deleted {key}")


if __name__ == "__main__":
    sys.exit(main())
