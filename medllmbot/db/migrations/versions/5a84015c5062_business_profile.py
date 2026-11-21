"""business profile: стоматология или магазин электроники

Revision ID: 5a84015c5062
Revises: f79015512d1f
Create Date: 2026-11-22 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a84015c5062"
down_revision: str | Sequence[str] | None = "f79015512d1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "profile",
                sa.Enum("CLINIC", "SHOP", name="businessprofile", native_enum=False, length=16),
                server_default="CLINIC",
                nullable=False,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_column("profile")
