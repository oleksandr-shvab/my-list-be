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
        CheckConstraint("build_slot IS NULL OR build_slot > 0", name="build_slot_positive"),
        CheckConstraint(
            f"reason <> '{ExclusionReason.REBUILD}' OR build_slot IS NOT NULL",
            name="rebuild_has_slot",
        ),
        CheckConstraint(
            f"reason <> '{ExclusionReason.NEVER}' OR build_slot IS NULL", name="never_has_no_slot"
        ),
        Index("ix_answer_exclusions_game_id_variant_title_id", "game_id", "variant", "title_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.id", ondelete="RESTRICT"), nullable=False
    )
    # A build slot is unique per game and variant, so a `rebuild` row names the variant too.
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
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
