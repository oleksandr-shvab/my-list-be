from fastapi import APIRouter

from app.core import health
from app.core.exceptions import ServiceUnavailableError

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process answers. Checks no dependency."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    """Readiness: Postgres and Redis answer. Not meant to be public; the proxy hides it."""
    failed = await health.failed_dependencies()
    if failed:
        raise ServiceUnavailableError("Dependencies not ready", params={"failed": failed})
    return {"status": "ready"}
