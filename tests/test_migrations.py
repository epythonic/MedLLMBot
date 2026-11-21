"""Миграции: применяются с нуля, откатываются и совпадают с моделями."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from medllmbot.db.migrate import alembic_config, upgrade_db
from medllmbot.db.models import Base


def _diff(db_path: Path) -> list[object]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        diff: list[object] = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    return diff


def _tables(db_path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    tables = set(sa.inspect(engine).get_table_names())
    engine.dispose()
    return tables


def test_upgrade_matches_models(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    upgrade_db(db)
    # Если модели поменяли, а миграцию не создали, здесь будет список отличий
    assert _diff(db) == []


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    upgrade_db(db)
    upgrade_db(db)
    assert "leads" in _tables(db)


def test_downgrade_to_base_and_back(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    upgrade_db(db)

    command.downgrade(alembic_config(db), "base")
    assert _tables(db) == {"alembic_version"}

    upgrade_db(db)
    assert _diff(db) == []


def test_price_list_is_seeded(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    upgrade_db(db)

    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT model, from_prompt_tokens, input_per_mtok, output_per_mtok FROM model_prices")
        ).all()
    engine.dispose()

    prices = {(model, start): (inp, out) for model, start, inp, out in rows}
    assert prices[("claude-haiku-4-5", 0)] == (1.0, 5.0)
    assert prices[("claude-haiku-5-5", 0)] == (0.1, 0.5)
    assert prices[("claude-haiku-5-5", 100_001)] == (0.5, 2.5)
