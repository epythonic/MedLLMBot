"""daily budget: дневной лимит расходов на Claude

Revision ID: 5b9f7d89371e
Revises: a6cc85d9a7c3
Create Date: 2026-11-21 18:14:51.701007

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b9f7d89371e"
down_revision: str | Sequence[str] | None = "a6cc85d9a7c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(sa.Column("daily_budget_usd", sa.Integer(), server_default="0", nullable=False))
        # Alembic не находит CHECK-ограничения автоматически — добавлено вручную
        batch_op.create_check_constraint(op.f("ck_bot_config_budget_non_negative"), "daily_budget_usd >= 0")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_bot_config_budget_non_negative"), type_="check")
        batch_op.drop_column("daily_budget_usd")
