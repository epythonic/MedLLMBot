"""Применение миграций без alembic.ini: python -m medllmbot.db.migrate"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from medllmbot.config import AppSettings
from medllmbot.db.engine import database_url

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url(db_path))
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_db(db_path: Path) -> None:
    """Доводит схему БД до последней версии. Вызывать до запуска event loop."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), "head")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    upgrade_db(AppSettings().db_path)
