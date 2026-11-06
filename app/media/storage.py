import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.core.config import settings
from app.media.signing import sign_link

VIDEO_CONTENT_TYPE_PREFIX = "video/"


class MediaStorage(Protocol):
    def signed_url(
        self, key: str, content_type: str, now: dt.datetime
    ) -> tuple[str, dt.datetime]: ...


def _lifetime_seconds(content_type: str, *, image_ttl: int, video_ttl: int) -> int:
    return video_ttl if content_type.startswith(VIDEO_CONTENT_TYPE_PREFIX) else image_ttl


def _expiry(now: dt.datetime, lifetime_seconds: int) -> dt.datetime:
    # Whole seconds, because the local link carries the expiry as an integer.
    return dt.datetime.fromtimestamp(int(now.timestamp()) + lifetime_seconds, tz=dt.UTC)


class LocalMediaStorage:
    def __init__(
        self,
        *,
        directory: Path,
        base_url: str,
        signing_key: bytes,
        image_ttl_seconds: int,
        video_ttl_seconds: int,
    ) -> None:
        self.directory = directory
        self.base_url = base_url.rstrip("/")
        self.signing_key = signing_key
        self.image_ttl_seconds = image_ttl_seconds
        self.video_ttl_seconds = video_ttl_seconds

    def path_for(self, key: str) -> Path:
        """The caller has checked the key's shape (`is_media_key`)."""
        return self.directory / key

    def signed_url(self, key: str, content_type: str, now: dt.datetime) -> tuple[str, dt.datetime]:
        lifetime = _lifetime_seconds(
            content_type, image_ttl=self.image_ttl_seconds, video_ttl=self.video_ttl_seconds
        )
        expires_at = _expiry(now, lifetime)
        expires = int(expires_at.timestamp())
        signature = sign_link(key, expires, self.signing_key)
        return f"{self.base_url}/v1/media/{key}?exp={expires}&sig={signature}", expires_at


class S3MediaStorage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        image_ttl_seconds: int,
        video_ttl_seconds: int,
    ) -> None:
        self.bucket = bucket
        self.image_ttl_seconds = image_ttl_seconds
        self.video_ttl_seconds = video_ttl_seconds
        # Imported here, so an app with local storage never loads boto3 (large and slow to import).
        import boto3.session
        from botocore.config import Config

        # Not boto3's shared default session: it is created lazily and is not thread-safe to
        # create.
        self.client: Any = boto3.session.Session().client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            # Virtual-hosted addressing, which Hetzner Object Storage supports.
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )

    def signed_url(self, key: str, content_type: str, now: dt.datetime) -> tuple[str, dt.datetime]:
        lifetime = _lifetime_seconds(
            content_type, image_ttl=self.image_ttl_seconds, video_ttl=self.video_ttl_seconds
        )
        # Presigning makes no network call, so it is safe in async code.
        expires_at = _expiry(now, lifetime)
        # `private` keeps shared caches out. The response's `Expires` is the link's own expiry: a
        # `max-age` counts from the moment the bucket answers, which can be just before the link
        # dies, so it could let a browser keep the file well past it.
        url: str = self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseCacheControl": "private",
                "ResponseExpires": expires_at,
            },
            ExpiresIn=lifetime,
        )
        return url, expires_at


@lru_cache
def build_media_storage() -> MediaStorage:
    if settings.MEDIA_STORAGE == "s3":
        # The settings validator has checked that these are set.
        assert settings.S3_ENDPOINT_URL and settings.S3_REGION and settings.S3_BUCKET
        assert settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY
        return S3MediaStorage(
            endpoint_url=settings.S3_ENDPOINT_URL,
            region=settings.S3_REGION,
            bucket=settings.S3_BUCKET,
            access_key_id=settings.S3_ACCESS_KEY_ID,
            secret_access_key=settings.S3_SECRET_ACCESS_KEY.get_secret_value(),
            image_ttl_seconds=settings.MEDIA_IMAGE_LINK_TTL_SECONDS,
            video_ttl_seconds=settings.MEDIA_VIDEO_LINK_TTL_SECONDS,
        )
    return LocalMediaStorage(
        directory=settings.media_local_path,
        base_url=settings.MEDIA_LOCAL_BASE_URL,
        signing_key=settings.MEDIA_SIGNING_KEY.get_secret_value().encode(),
        image_ttl_seconds=settings.MEDIA_IMAGE_LINK_TTL_SECONDS,
        video_ttl_seconds=settings.MEDIA_VIDEO_LINK_TTL_SECONDS,
    )


async def get_media_storage() -> MediaStorage:
    """`async`, so it runs on the event loop instead of the threadpool on every request."""
    return build_media_storage()
