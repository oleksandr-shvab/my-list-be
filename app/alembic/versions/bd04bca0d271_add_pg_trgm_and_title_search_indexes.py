"""add pg_trgm and title search indexes

Revision ID: bd04bca0d271
Revises: dece8484277f
Create Date: 2026-11-04 16:20:49.605853

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd04bca0d271"
down_revision: str | Sequence[str] | None = "dece8484277f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # A trusted extension: the database owner can create it, no superuser needed.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        op.f("ix_titles_normalized_franchise_trgm"),
        "titles",
        ["normalized_franchise"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_franchise": "gin_trgm_ops"},
    )
    op.create_index(
        op.f("ix_titles_normalized_name_trgm"),
        "titles",
        ["normalized_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_titles_normalized_name_trgm"),
        table_name="titles",
        postgresql_using="gin",
        postgresql_ops={"normalized_name": "gin_trgm_ops"},
    )
    op.drop_index(
        op.f("ix_titles_normalized_franchise_trgm"),
        table_name="titles",
        postgresql_using="gin",
        postgresql_ops={"normalized_franchise": "gin_trgm_ops"},
    )
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
