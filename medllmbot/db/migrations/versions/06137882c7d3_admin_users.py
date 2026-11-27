"""admin_users: extra admin panel accounts with their own email and password.

The owner keeps logging in with ADMIN_USERNAME / ADMIN_PASSWORD from .env; the table starts empty.

Revision ID: 06137882c7d3
Revises: 02b4933425dd
Create Date: 2026-11-27 16:00:04.437440

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "06137882c7d3"
down_revision: str | Sequence[str] | None = "02b4933425dd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
        sa.UniqueConstraint("email", name=op.f("uq_admin_users_email")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("admin_users")
