import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, enum_check
from app.games.enums import ProgressStatus
from app.models.title import Title


class ProgressOrigin(StrEnum):
    PLAY = "play"
    MERGE = "merge"


class Progress(Base):
    """One account's game on one puzzle (AC-35). Created by the first guess, never by a view."""

    __tablename__ = "progress"
    __table_args__ = (
        UniqueConstraint("user_id", "puzzle_id"),
        CheckConstraint(enum_check("status", ProgressStatus), name="status"),
        CheckConstraint(enum_check("origin", ProgressOrigin), name="origin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The player's progress is personal data and goes with the account (AC-35).
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Indexed for the foreign key's RESTRICT check and for reads by puzzle (stats, archive).
    puzzle_id: Mapped[int] = mapped_column(
        ForeignKey("puzzles.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once, when the first guess is stored, and never changed (AC-36).
    counts_for_streak: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # A hook for a later admin role (Q12). Nothing sets or returns it in Phase 1 (AC-43).
    is_test: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)

    # `lazy="raise"`: AsyncSession cannot lazy-load, so the queries in `app/crud/progress.py`
    # load these explicitly and any other access fails loudly.
    guesses: Mapped[list["ProgressGuess"]] = relationship(
        order_by="ProgressGuess.position", lazy="raise", viewonly=True
    )


class ProgressGuess(Base):
    """One stored guess of a progress row. `title_id` is null for a skip."""

    __tablename__ = "progress_guesses"
    __table_args__ = (
        UniqueConstraint("progress_id", "position"),
        CheckConstraint("position >= 1", name="position_positive"),
        # A title cannot be guessed twice in one game (AC-31): the database backs the service.
        Index(
            "uq_progress_guesses_progress_id_title_id",
            "progress_id",
            "title_id",
            unique=True,
            postgresql_where="title_id IS NOT NULL",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    progress_id: Mapped[int] = mapped_column(
        ForeignKey("progress.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # RESTRICT: a title that a stored guess refers to cannot be deleted (AC-11).
    title_id: Mapped[int | None] = mapped_column(
        ForeignKey("titles.id", ondelete="RESTRICT"), index=True, nullable=True
    )

    title: Mapped[Title | None] = relationship(lazy="raise", viewonly=True)
