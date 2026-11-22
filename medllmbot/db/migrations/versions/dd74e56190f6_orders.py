"""orders: shop orders for the order_status tool

Revision ID: dd74e56190f6
Revises: 5a84015c5062
Create Date: 2026-11-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dd74e56190f6"
down_revision: str | Sequence[str] | None = "5a84015c5062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(length=32), nullable=False),
        sa.Column("customer_name", sa.String(length=256), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "NEW",
                "CONFIRMED",
                "SHIPPED",
                "READY",
                "DELIVERED",
                "CANCELLED",
                name="orderstatus",
                native_enum=False,
                length=16,
            ),
            server_default="NEW",
            nullable=False,
        ),
        sa.Column("items", sa.Text(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("total >= 0", name=op.f("ck_orders_total_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("number", name=op.f("uq_orders_number")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("orders")
