"""Settings of the content scripts (R-3).

They live apart from `app.core.config.Settings`: the server needs no IGDB variable, and the
deployed-config checks there stay as they are. Only the scripts build this class; `app/main.py`
never imports `app/content/`.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import PROJECT_ROOT


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

    # The secret behind every random choice of the puzzle builder (AC-26 to AC-28). Kept outside
    # the repos, different from every other secret, as strong as `SECRET_KEY`: see `check_salt`.
    PUZZLE_SEED_SALT: SecretStr | None = None

    # Where the builder keeps raw IGDB screenshots and the files it makes before storing them
    # (AC-43). A relative folder is read from the repo root; the default is ignored by Git.
    CONTENT_WORK_DIR: Path = Path("var/content-work")
    # R-12, AC-2: while this is empty, no IGDB screenshot leaves `local`. The owner sets it to the
    # `SPEC.md` decision that records IGDB's written yes, or his written acceptance of the risk.
    IGDB_IMAGES_CLEARED_BY: str | None = None

    # Who may be an answer (AC-21, AC-22): the start values, for the owner to tune after measuring
    # them with `scripts/build_puzzles.py --counts` (AC-24).
    # At least 1 rating: the title import keeps games with one, and the search list must hold every
    # candidate (R-20). Fewer than 5 screenshots is raised to 5 by the funnel (a build needs 5,
    # decision 74).
    CANDIDATE_MIN_RATINGS: int = Field(50, ge=1, le=10_000_000)
    CANDIDATE_MIN_SCREENSHOTS: int = Field(5, ge=1, le=100)
    CANDIDATE_MIN_WIDTH: int = Field(1280, ge=1, le=10_000)
    CANDIDATE_MIN_HEIGHT: int = Field(720, ge=1, le=10_000)
    CANDIDATE_MIN_AGE_DAYS: int = Field(365, ge=0, le=3650)
    # What "ratings" means (Open point 3): IGDB's `total_rating_count` (users and critics), or
    # `aggregated_rating_count` (critics only).
    CANDIDATE_RATING_FIELD: Literal["total_rating_count", "aggregated_rating_count"] = (
        "total_rating_count"
    )

    @field_validator("IGDB_IMAGES_CLEARED_BY")
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @property
    def content_work_path(self) -> Path:
        return PROJECT_ROOT / self.CONTENT_WORK_DIR

    def require(self, *names: str) -> None:
        """Stop the run before any request when a setting is missing (AC-3)."""
        for name in names:
            if getattr(self, name) is None:
                raise MissingSettingError(f"{name} is not set")
