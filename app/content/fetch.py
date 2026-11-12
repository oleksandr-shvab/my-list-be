"""The one HTTP client of the content scripts (AC-85).

Every request goes only to a host on the allow list, over https, and a redirect is never followed,
so IGDB data cannot send a script to another host. Bodies are read with a size cap.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx

ALLOWED_HOSTS = frozenset({"api.igdb.com", "id.twitch.tv", "images.igdb.com"})
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_BODY_BYTES = 20 * 1024 * 1024


class FetchError(Exception):
    """A request the scripts refuse to make, or a response they refuse to read."""


class ForbiddenHostError(FetchError):
    """The URL is not https or its host is not on the allow list."""


class ResponseTooLargeError(FetchError):
    pass


@dataclass(frozen=True)
class FetchedResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content)


async def refuse_foreign_hosts(request: httpx.Request) -> None:
    """Request hook: runs before anything leaves the machine."""
    url = request.url
    if url.scheme != "https" or url.host not in ALLOWED_HOSTS:
        # The URL is left out of the message: it is the part that is not trusted.
        raise ForbiddenHostError("a request to a host that is not on the allow list was refused")


def build_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """`transport` is for tests (`httpx.MockTransport`); the hook still runs in front of it."""
    return httpx.AsyncClient(
        transport=transport,
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        event_hooks={"request": [refuse_foreign_hosts]},
    )


async def send_capped(
    client: httpx.AsyncClient, request: httpx.Request, *, max_bytes: int = MAX_BODY_BYTES
) -> FetchedResponse:
    """Send and read the body, stopping at `max_bytes` instead of reading all of it."""
    response = await client.send(request, stream=True)
    try:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLargeError("a response is larger than the size cap")
            chunks.append(chunk)
    finally:
        await response.aclose()
    return FetchedResponse(response.status_code, response.headers, b"".join(chunks))
