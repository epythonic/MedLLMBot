"""Migrations: apply from scratch, roll back and match the models."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from medllmbot.db.fts import include_name
from medllmbot.db.migrate import alembic_config, upgrade_db
from medllmbot.db.models import Base


def _diff(db_path: Path) -> list[object]:
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        # Same filter as in env.py: the FTS index's internal tables are not described in the models
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
    # If the models changed but no migration was created, this lists the differences
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
    # Services created before the search migration get into the index, and new ones via triggers
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
    # Tests run on create_all, the bot on migrations: the index, view and triggers must match
    # down to the SQL. Changed fts.py? A new migration is needed
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
    # A bot installed before profiles stays a dental clinic
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


def test_operator_handoff_keeps_client_data(tmp_path: Path) -> None:
    # The clients column is added by a batch migration: recreating the clients table must not
    # cascade-delete their messages, leads and audit log
    db = tmp_path / "bot.db"
    command.upgrade(alembic_config(db), "dd74e56190f6")
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        conn.execute(sa.text("INSERT INTO clients (chat_id, full_name) VALUES (42, 'Иван')"))
        conn.execute(sa.text("INSERT INTO messages (chat_id, role, content) VALUES (42, 'user', 'Сколько стоит?')"))
        conn.execute(
            sa.text("INSERT INTO leads (chat_id, client_name, phone, message) VALUES (42, 'Иван', '069123456', 'запись')")
        )
        conn.execute(sa.text("INSERT INTO audit_log (chat_id, user_text, outcome) VALUES (42, 'вопрос', 'ANSWERED')"))
        conn.execute(
            sa.text(
                "INSERT INTO bot_config (id, model, max_tokens, max_len, history_limit, rate_limit, "
                "rate_window, greeting) VALUES (1, 'claude-haiku-5-5', 1024, 1024, 8, 10, 60, 'Привет')"
            )
        )
    upgrade_db(db)
    with engine.connect() as conn:
        counts = [conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar_one() for t in ("messages", "leads", "audit_log")]
        until = conn.execute(sa.text("SELECT operator_until FROM clients")).scalar_one()
        hours = conn.execute(sa.text("SELECT operator_hours FROM bot_config")).scalar_one()
    engine.dispose()
    assert counts == [1, 1, 1]
    assert until is None  # existing clients stay with the bot
    assert hours == 12


def test_existing_settings_keep_russian_for_the_admin(tmp_path: Path) -> None:
    # Before the setting, messages to the administrator were Russian only
    db = tmp_path / "bot.db"
    command.upgrade(alembic_config(db), "e3174a6cc260")
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
        language = conn.execute(sa.text("SELECT admin_language FROM bot_config")).scalar_one()
        with pytest.raises(IntegrityError):  # only the admin languages are accepted
            conn.execute(sa.text("UPDATE bot_config SET admin_language = 'de'"))
    engine.dispose()
    assert language == "ru"


def test_existing_greeting_stays_for_everyone(tmp_path: Path) -> None:
    # Before greeting_ro there was one greeting, often in both languages at once: all clients get it
    db = tmp_path / "bot.db"
    command.upgrade(alembic_config(db), "15280f8b2e3f")
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO bot_config (id, model, max_tokens, max_len, history_limit, rate_limit, "
                "rate_window, greeting) VALUES (1, 'claude-haiku-5-5', 1024, 1024, 8, 10, 60, 'Привет!\n\nBună!')"
            )
        )
    upgrade_db(db)
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT greeting, greeting_ro FROM bot_config")).one()
    engine.dispose()
    assert tuple(row) == ("Привет!\n\nBună!", None)
