"""admin_language: the language of the bot's Telegram messages to the administrator.

Existing settings get Russian, the only language those messages had before.

Revision ID: 02b4933425dd
Revises: e3174a6cc260
Create Date: 2026-11-22 10:47:35.362710

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "02b4933425dd"
down_revision: str | Sequence[str] | None = "e3174a6cc260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(sa.Column("admin_language", sa.String(length=2), server_default="ru", nullable=False))
        # Alembic does not detect CHECK constraints automatically; added by hand
        batch_op.create_check_constraint(op.f("ck_bot_config_admin_language_known"), "admin_language IN ('ru', 'ro', 'en')")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_bot_config_admin_language_known"), type_="check")
        batch_op.drop_column("admin_language")
