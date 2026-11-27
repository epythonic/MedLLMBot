"""staff: clinic or shop employees in Telegram who get operator calls and leads; clients.operator_chat_id.

The table starts empty, so the owner from ADMIN_ID stays the only operator until staff are added in the admin panel.
operator_chat_id is the operator who took a client's dialog with the first reply; existing dialogs are not taken.

Revision ID: c981a7c3261f
Revises: 06137882c7d3
Create Date: 2026-11-27 17:54:57.308521

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c981a7c3261f"
down_revision: str | Sequence[str] | None = "06137882c7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "staff",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff")),
        sa.UniqueConstraint("chat_id", name=op.f("uq_staff_chat_id")),
    )
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.add_column(sa.Column("operator_chat_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.drop_column("operator_chat_id")

    op.drop_table("staff")
