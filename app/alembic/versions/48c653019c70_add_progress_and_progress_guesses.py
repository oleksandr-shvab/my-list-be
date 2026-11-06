"""add progress and progress guesses

Revision ID: 48c653019c70
Revises: bd04bca0d271
Create Date: 2026-11-05 15:07:47.908250

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "48c653019c70"
down_revision: str | Sequence[str] | None = "bd04bca0d271"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "progress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("puzzle_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counts_for_streak", sa.Boolean(), nullable=False),
        sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.CheckConstraint("origin IN ('play', 'merge')", name=op.f("ck_progress_origin")),
        sa.CheckConstraint(
            "status IN ('in_progress', 'won', 'lost')", name=op.f("ck_progress_status")
        ),
        sa.ForeignKeyConstraint(
            ["puzzle_id"],
            ["puzzles.id"],
            name=op.f("fk_progress_puzzle_id_puzzles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_progress_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_progress")),
        sa.UniqueConstraint("user_id", "puzzle_id", name=op.f("uq_progress_user_id_puzzle_id")),
    )
    op.create_index(op.f("ix_progress_puzzle_id"), "progress", ["puzzle_id"], unique=False)
    op.create_table(
        "progress_guesses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("progress_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("position >= 1", name=op.f("ck_progress_guesses_position_positive")),
        sa.ForeignKeyConstraint(
            ["progress_id"],
            ["progress.id"],
            name=op.f("fk_progress_guesses_progress_id_progress"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_progress_guesses_title_id_titles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_progress_guesses")),
        sa.UniqueConstraint(
            "progress_id", "position", name=op.f("uq_progress_guesses_progress_id_position")
        ),
    )
    op.create_index(
        op.f("ix_progress_guesses_title_id"), "progress_guesses", ["title_id"], unique=False
    )
    op.create_index(
        "uq_progress_guesses_progress_id_title_id",
        "progress_guesses",
        ["progress_id", "title_id"],
        unique=True,
        postgresql_where="title_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_progress_guesses_progress_id_title_id",
        table_name="progress_guesses",
        postgresql_where="title_id IS NOT NULL",
    )
    op.drop_index(op.f("ix_progress_guesses_title_id"), table_name="progress_guesses")
    op.drop_table("progress_guesses")
    op.drop_index(op.f("ix_progress_puzzle_id"), table_name="progress")
    op.drop_table("progress")
