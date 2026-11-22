"""operator handoff: client with a live operator, relays to the admin chat, wait timeout

Revision ID: 15280f8b2e3f
Revises: dd74e56190f6
Create Date: 2026-11-21 23:13:57.217807

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "15280f8b2e3f"
down_revision: str | Sequence[str] | None = "dd74e56190f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "operator_relays",
        sa.Column("admin_chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("admin_message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["clients.chat_id"], name=op.f("fk_operator_relays_chat_id_clients"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("admin_chat_id", "admin_message_id", name=op.f("pk_operator_relays")),
    )
    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(sa.Column("operator_hours", sa.Integer(), server_default="12", nullable=False))
        # Alembic does not detect CHECK constraints automatically; added by hand
        batch_op.create_check_constraint(op.f("ck_bot_config_operator_hours_positive"), "operator_hours > 0")

    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.add_column(sa.Column("operator_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("clients", schema=None) as batch_op:
        batch_op.drop_column("operator_until")

    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_bot_config_operator_hours_positive"), type_="check")
        batch_op.drop_column("operator_hours")

    op.drop_table("operator_relays")
