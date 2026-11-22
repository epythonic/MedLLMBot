"""catalog search: catalog full-text index, catalog-in-prompt threshold, tool calls in the audit log

Revision ID: f79015512d1f
Revises: 5b9f7d89371e
Create Date: 2026-11-21 21:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f79015512d1f"
down_revision: str | Sequence[str] | None = "5b9f7d89371e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQL is a copy of medllmbot/db/fts.py at migration time: the migration must not change along with the code.
# The index source is a view mapping «ё» → «е»: the FTS5 tokenizer does not treat them as equal
FTS_UPGRADE = (
    """CREATE VIEW services_fts_src AS SELECT id, replace(replace(name, 'ё', 'е'), 'Ё', 'Е') AS name, replace(replace(description, 'ё', 'е'), 'Ё', 'Е') AS description FROM services""",
    """CREATE VIRTUAL TABLE services_fts USING fts5(name, description, content='services_fts_src', content_rowid='id', tokenize='unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER services_fts_ai AFTER INSERT ON services BEGIN
    INSERT INTO services_fts(rowid, name, description) VALUES (new.id, replace(replace(new.name, 'ё', 'е'), 'Ё', 'Е'), replace(replace(new.description, 'ё', 'е'), 'Ё', 'Е'));
END""",
    """CREATE TRIGGER services_fts_ad AFTER DELETE ON services BEGIN
    INSERT INTO services_fts(services_fts, rowid, name, description) VALUES ('delete', old.id, replace(replace(old.name, 'ё', 'е'), 'Ё', 'Е'), replace(replace(old.description, 'ё', 'е'), 'Ё', 'Е'));
END""",
    """CREATE TRIGGER services_fts_au AFTER UPDATE ON services BEGIN
    INSERT INTO services_fts(services_fts, rowid, name, description) VALUES ('delete', old.id, replace(replace(old.name, 'ё', 'е'), 'Ё', 'Е'), replace(replace(old.description, 'ё', 'е'), 'Ё', 'Е'));
    INSERT INTO services_fts(rowid, name, description) VALUES (new.id, replace(replace(new.name, 'ё', 'е'), 'Ё', 'Е'), replace(replace(new.description, 'ё', 'е'), 'Ё', 'Е'));
END""",
    # Index the services that already exist
    "INSERT INTO services_fts(services_fts) VALUES ('rebuild')",
)
FTS_DOWNGRADE = (
    "DROP TRIGGER IF EXISTS services_fts_ai",
    "DROP TRIGGER IF EXISTS services_fts_ad",
    "DROP TRIGGER IF EXISTS services_fts_au",
    "DROP TABLE IF EXISTS services_fts",
    "DROP VIEW IF EXISTS services_fts_src",
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tool_calls", sa.Text(), nullable=True))

    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.add_column(sa.Column("catalog_inline_limit", sa.Integer(), server_default="50", nullable=False))
        # Alembic does not detect CHECK constraints automatically; added by hand
        batch_op.create_check_constraint(op.f("ck_bot_config_catalog_limit_non_negative"), "catalog_inline_limit >= 0")

    for statement in FTS_UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema."""
    for statement in FTS_DOWNGRADE:
        op.execute(statement)

    with op.batch_alter_table("bot_config", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_bot_config_catalog_limit_non_negative"), type_="check")
        batch_op.drop_column("catalog_inline_limit")

    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_column("tool_calls")
