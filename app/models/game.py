from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Integer, String, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, enum_check
from app.models.title import TitleKind


class Game(Base):
    __tablename__ = "games"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_format"),
        CheckConstraint(enum_check("title_kind", TitleKind), name="title_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    default_variant: Mapped[str] = mapped_column(String(32), nullable=False)
    title_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    launch_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, server_default=true(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
