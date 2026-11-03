import datetime as dt
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, enum_check


class PuzzleStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    SCHEDULED = "scheduled"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class ClueRole(StrEnum):
    SOURCE = "source"
    POSTER = "poster"


class ClueContentType(StrEnum):
    IMAGE_WEBP = "image/webp"
    VIDEO_MP4 = "video/mp4"
    VIDEO_WEBM = "video/webm"


class Puzzle(Base):
    __tablename__ = "puzzles"
    __table_args__ = (
        UniqueConstraint("game_id", "variant", "date"),
        CheckConstraint(enum_check("status", PuzzleStatus), name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="RESTRICT"), nullable=False
    )
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # RESTRICT: a title that is the answer of a puzzle cannot be deleted (AC-11).
    answer_title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PuzzleClue(Base):
    __tablename__ = "puzzle_clues"
    __table_args__ = (
        UniqueConstraint("puzzle_id", "step"),
        CheckConstraint("step BETWEEN 1 AND 6", name="step"),
        CheckConstraint(enum_check("media_type", MediaType), name="media_type"),
        CheckConstraint("(hint_kind IS NULL) = (hint_value IS NULL)", name="hint_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    puzzle_id: Mapped[int] = mapped_column(
        ForeignKey("puzzles.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Free text, because other games will have other hints. The game guesser uses
    # metacritic_score, platform, genre, year and developer (see SPEC-02, game guesser steps).
    hint_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hint_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClueMediaFile(Base):
    """One stored file of a clue. The cross-row rules of AC-13b (a video clue has one MP4 and
    one poster, an image clue has one image and no poster) are not database constraints: the
    view checks them on read and the puzzle builder on write."""

    __tablename__ = "clue_media_files"
    __table_args__ = (
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("width > 0", name="width_positive"),
        CheckConstraint("height > 0", name="height_positive"),
        CheckConstraint("duration_ms > 0", name="duration_ms_positive"),
        CheckConstraint(enum_check("role", ClueRole), name="role"),
        CheckConstraint(enum_check("content_type", ClueContentType), name="content_type"),
        CheckConstraint(
            "(content_type LIKE 'video/%') = (duration_ms IS NOT NULL)", name="duration_video_only"
        ),
        CheckConstraint(
            f"role <> '{ClueRole.POSTER}' OR content_type = '{ClueContentType.IMAGE_WEBP}'",
            name="poster_is_webp",
        ),
        Index(
            "uq_clue_media_files_clue_id_content_type_source",
            "clue_id",
            "content_type",
            unique=True,
            postgresql_where=f"role = '{ClueRole.SOURCE}'",
        ),
        Index(
            "uq_clue_media_files_clue_id_poster",
            "clue_id",
            unique=True,
            postgresql_where=f"role = '{ClueRole.POSTER}'",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clue_id: Mapped[int] = mapped_column(
        ForeignKey("puzzle_clues.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # Not unique: content-hash keys can repeat across clues.
    key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
