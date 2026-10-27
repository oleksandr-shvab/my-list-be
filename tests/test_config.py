import pytest
from pydantic import ValidationError

from app.core.config import Settings

DEPLOYED = {
    "ENVIRONMENT": "production",
    "DATABASE_URL": "postgresql+asyncpg://u:p@db/app",
    "SECRET_KEY": "x" * 40,
    "FRONTEND_URL": "https://example.com",
    "BACKEND_CORS_ORIGINS": [],
}


def test__production_settings__safe_values_boot() -> None:
    assert Settings(**DEPLOYED).is_deployed


@pytest.mark.parametrize(
    "override",
    [
        {"SECRET_KEY": "change-me"},
        {"SECRET_KEY": "too-short"},
        {"FRONTEND_URL": "http://example.com"},
        {"BACKEND_CORS_ORIGINS": ["http://localhost:5173"]},
    ],
)
def test__production_settings__unsafe_values_refuse_to_boot(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**DEPLOYED, **override})
