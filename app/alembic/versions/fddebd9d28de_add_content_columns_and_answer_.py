"""add content columns and answer exclusions

Revision ID: fddebd9d28de
Revises: 48c653019c70
Create Date: 2026-11-12 15:54:07.290106

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fddebd9d28de"
down_revision: str | Sequence[str] | None = "48c653019c70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "answer_exclusions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("variant", sa.String(length=32), nullable=False),
        sa.Column("title_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("build_slot", sa.Integer(), nullable=True),
        sa.Column("image_ids", sa.ARRAY(sa.String(length=32)), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('rebuild', 'never')", name=op.f("ck_answer_exclusions_reason")
        ),
        sa.CheckConstraint(
            "build_slot IS NULL OR build_slot > 0",
            name=op.f("ck_answer_exclusions_build_slot_positive"),
        ),
        sa.CheckConstraint(
            "reason <> 'rebuild' OR build_slot IS NOT NULL",
            name=op.f("ck_answer_exclusions_rebuild_has_slot"),
        ),
        sa.CheckConstraint(
            "reason <> 'never' OR build_slot IS NULL",
            name=op.f("ck_answer_exclusions_never_has_no_slot"),
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name=op.f("fk_answer_exclusions_game_id_games"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["title_id"],
            ["titles.id"],
            name=op.f("fk_answer_exclusions_title_id_titles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answer_exclusions")),
    )
    op.create_index(
        "ix_answer_exclusions_game_id_variant_title_id",
        "answer_exclusions",
        ["game_id", "variant", "title_id"],
        unique=False,
    )
    op.add_column(
        "puzzle_clues",
        sa.Column("source_image_ids", sa.ARRAY(sa.String(length=32)), nullable=True),
    )
    op.add_column("puzzles", sa.Column("build_slot", sa.Integer(), nullable=True))
    op.add_column("puzzles", sa.Column("generator_version", sa.String(length=32), nullable=True))
    op.add_column("puzzles", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("puzzles", "date", existing_type=sa.DATE(), nullable=True)
    op.create_unique_constraint(
        op.f("uq_puzzles_game_id_variant_build_slot"),
        "puzzles",
        ["game_id", "variant", "build_slot"],
    )
    op.create_check_constraint(op.f("ck_puzzles_build_slot_positive"), "puzzles", "build_slot > 0")
    op.create_check_constraint(
        op.f("ck_puzzles_scheduled_has_date"),
        "puzzles",
        "status <> 'scheduled' OR date IS NOT NULL",
    )
    op.add_column("titles", sa.Column("igdb_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint(op.f("uq_titles_igdb_id"), "titles", ["igdb_id"])


def downgrade() -> None:
    # The downgrade drops the Phase 3 content table and columns with their data. The one thing it
    # refuses is a puzzle with no date, because `puzzles.date` cannot be NOT NULL again while one
    # exists, and it will not invent a date. It is a DO block, not a Python query, so it also runs
    # (and raises) from `alembic ... --sql`.
    op.execute(
        """
        DO $$
        DECLARE undated integer;
        BEGIN
            SELECT count(*) INTO undated FROM puzzles WHERE date IS NULL;
            IF undated > 0 THEN
                RAISE EXCEPTION 'Cannot downgrade: % puzzle(s) have no date. Schedule them or delete them first.', undated;
            END IF;
        END $$
        """
    )
    op.drop_constraint(op.f("uq_titles_igdb_id"), "titles", type_="unique")
    op.drop_column("titles", "igdb_id")
    op.drop_constraint(op.f("ck_puzzles_scheduled_has_date"), "puzzles", type_="check")
    op.drop_constraint(op.f("ck_puzzles_build_slot_positive"), "puzzles", type_="check")
    op.drop_constraint(op.f("uq_puzzles_game_id_variant_build_slot"), "puzzles", type_="unique")
    op.alter_column("puzzles", "date", existing_type=sa.DATE(), nullable=False)
    op.drop_column("puzzles", "approved_at")
    op.drop_column("puzzles", "generator_version")
    op.drop_column("puzzles", "build_slot")
    op.drop_column("puzzle_clues", "source_image_ids")
    op.drop_index("ix_answer_exclusions_game_id_variant_title_id", table_name="answer_exclusions")
    op.drop_table("answer_exclusions")
