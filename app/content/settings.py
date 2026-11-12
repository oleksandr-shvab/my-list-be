"""Settings of the content scripts (R-3).

They live apart from `app.core.config.Settings`: the server needs no IGDB variable, and the
deployed-config checks there stay as they are. Only the scripts build this class; `app/main.py`
never imports `app/content/`.
"""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class MissingSettingError(Exception):
    """A script needs a setting that is not set. The message names the setting, never a value."""


class ContentSettings(BaseSettings):
    # The scripts run from any folder, so read the repo's `.env`. An empty value counts as unset,
    # which is how `.env.example` ships the IGDB keys.
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_ignore_empty=True, extra="ignore"
    )

    IGDB_CLIENT_ID: str | None = None
    # A SecretStr prints as `**********` in a repr, a log line or a traceback.
    IGDB_CLIENT_SECRET: SecretStr | None = None

    def require(self, *names: str) -> None:
        """Stop the run before any request when a setting is missing (AC-3)."""
        for name in names:
            if getattr(self, name) is None:
                raise MissingSettingError(f"{name} is not set")
