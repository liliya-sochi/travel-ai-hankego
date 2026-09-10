"""add trip preferences data

Revision ID: 8ca44bb76e20
Revises: 13629a2f4518
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ca44bb76e20"
down_revision: str | Sequence[str] | None = "13629a2f4518"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет nullable-предпочтения без изменения старых записей."""

    op.add_column(
        "trips",
        sa.Column(
            "preferences_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Удаляет сохранённые предпочтения маршрута."""

    op.drop_column("trips", "preferences_data")
