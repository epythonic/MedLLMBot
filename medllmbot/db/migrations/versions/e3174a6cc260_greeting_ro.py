"""greeting_ro: приветствие по-румынски в настройках бота.

У существующих настроек поле пустое: клиенты получают прежнее приветствие, в котором бывают оба языка.

Revision ID: e3174a6cc260
Revises: 15280f8b2e3f
Create Date: 2026-11-22 01:00:27.899289

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3174a6cc260"
down_revision: str | Sequence[str] | None = "15280f8b2e3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Колонка с NULL добавляется без пересоздания таблицы
    op.add_column("bot_config", sa.Column("greeting_ro", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_column("greeting_ro")
