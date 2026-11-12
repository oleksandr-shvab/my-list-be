from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, enum_check


class TitleKind(StrEnum):
    VIDEO_GAME = "video_game"


class Title(Base):
    """The normalized columns are written only by `app/crud/title.py`, through
    `app/games/normalize.py`."""

    __tablename__ = "titles"
    __table_args__ = (
        CheckConstraint(enum_check("kind", TitleKind), name="kind"),
        CheckConstraint("normalized_name <> ''", name="normalized_name_not_empty"),
        # `alembic check` does not compare the operator class, so check the migration by hand.
        Index(
            "ix_titles_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_titles_normalized_franchise_trgm",
            "normalized_franchise",
            postgresql_using="gin",
            postgresql_ops={"normalized_franchise": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Wider than the raw columns: NFKD can make a string longer.
    normalized_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    franchise: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # A stored copy: the trigram index needs a real column, and SQL cannot repeat the Python
    # normalizer without `unaccent`.
    normalized_franchise: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The IGDB id of an imported title. NULLs are distinct, so titles made by hand stay valid.
    igdb_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
