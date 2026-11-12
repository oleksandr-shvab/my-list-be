import datetime as dt

from sqlalchemy import ARRAY, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, enum_check
from app.games.enums import ExclusionReason


class AnswerExclusion(Base):
    """A title the reviewer held back as an answer: `rebuild` for the slot it was drawn for (with
    the screenshots to leave out), `never` for good."""

    __tablename__ = "answer_exclusions"
    __table_args__ = (
        CheckConstraint(enum_check("reason", ExclusionReason), name="reason"),
        Index("ix_answer_exclusions_game_id_title_id", "game_id", "title_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="RESTRICT"), nullable=False
    )
    title_id: Mapped[int] = mapped_column(
        ForeignKey("titles.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    # The slot to build again; null for `never`.
    build_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_ids: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), server_default="{}", nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
