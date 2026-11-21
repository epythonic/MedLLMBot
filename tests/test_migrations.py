"""Миграции: применяются с нуля, откатываются и совпадают с моделями."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from medllmbot.db.fts import include_name
from medllmbot.db.migrate import alembic_config, upgrade_db
from medllmbot.db.models import Base


def _diff(db_path: Path) -> list[object]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        # Тот же фильтр, что в env.py: служебные таблицы индекса FTS в моделях не описаны
        context = MigrationContext.configure(conn, opts={"include_name": include_name})
        diff: list[object] = compare_metadata(context, Base.metadata)
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


def test_catalog_index_is_built_for_existing_services(tmp_path: Path) -> None:
    # Услуги, заведённые до миграции поиска, попадают в индекс, а новые — через триггеры
    db = tmp_path / "bot.db"
    config = alembic_config(db)
    command.upgrade(config, "5b9f7d89371e")
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO services (name, price) VALUES ('Чистка зубов', 800)"))
    upgrade_db(db)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO services (name, price) VALUES ('Отбеливание зубов', 3500)"))
        found = conn.execute(
            sa.text("SELECT s.name FROM services s JOIN services_fts f ON f.rowid = s.id WHERE services_fts MATCH 'зуб*'")
        ).scalars()
        names = sorted(found)
    engine.dispose()
    assert names == ["Отбеливание зубов", "Чистка зубов"]


def test_migration_and_models_create_same_catalog_index(tmp_path: Path) -> None:
    # Тесты работают на create_all, бот — на миграциях: индекс, представление и триггеры должны совпадать
    # вплоть до SQL. Поменяли fts.py — нужна новая миграция
    def schema(db_path: Path) -> list[tuple[str, str, str]]:
        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'services_fts%' ORDER BY type, name")
            ).all()
        engine.dispose()
        return [(kind, name, sql or "") for kind, name, sql in rows]

    migrated = tmp_path / "migrated.db"
    upgrade_db(migrated)
    created = tmp_path / "created.db"
    engine = sa.create_engine(f"sqlite:///{created}")
    Base.metadata.create_all(engine)
    engine.dispose()

    assert schema(migrated) == schema(created)
    assert {(kind, name) for kind, name, _ in schema(migrated)} >= {
        ("table", "services_fts"),
        ("view", "services_fts_src"),
        ("trigger", "services_fts_ai"),
        ("trigger", "services_fts_ad"),
        ("trigger", "services_fts_au"),
    }


def test_existing_settings_get_clinic_profile(tmp_path: Path) -> None:
    # Бот, установленный до профилей, остаётся стоматологией
    db = tmp_path / "bot.db"
    command.upgrade(alembic_config(db), "f79015512d1f")
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO bot_config (id, model, max_tokens, max_len, history_limit, rate_limit, "
                "rate_window, greeting) VALUES (1, 'claude-haiku-5-5', 1024, 1024, 8, 10, 60, 'Привет')"
            )
        )
    upgrade_db(db)
    with engine.connect() as conn:
        profile = conn.execute(sa.text("SELECT profile FROM bot_config")).scalar_one()
    engine.dispose()
    assert profile == "CLINIC"
