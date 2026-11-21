"""Настройки из окружения: from_env() возвращает нужный класс и требует секреты."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from medllmbot.check_config import find_problems
from medllmbot.config import AdminSettings, BotSettings, describe_settings_error

SECRETS = ("TG_TOKEN", "ADMIN_ID", "ANTHROPIC_API_KEY", "ADMIN_PASSWORD", "ADMIN_SECRET_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Пустое окружение и каталог без .env — тест не зависит от машины разработчика."""
    monkeypatch.chdir(tmp_path)
    for name in (*SECRETS, "DB_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_bot_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_ID", "777")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DB_PATH", "/data/bot.db")

    settings = BotSettings.from_env()

    assert isinstance(settings, BotSettings)
    assert settings.admin_id == 777
    assert settings.tg_token.get_secret_value() == "123:abc"
    assert settings.db_path == Path("/data/bot.db")


def test_bot_settings_require_secrets() -> None:
    with pytest.raises(ValidationError):
        BotSettings.from_env()


def test_admin_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "secret-pass")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "k" * 32)

    settings = AdminSettings.from_env()

    assert isinstance(settings, AdminSettings)
    assert settings.admin_username == "admin"
    assert settings.admin_password.get_secret_value() == "secret-pass"
    assert settings.db_path == Path("storage/bot.db")


def test_secrets_are_hidden_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "secret-pass")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "k" * 32)

    assert "secret-pass" not in repr(AdminSettings.from_env())


def test_startup_error_names_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    # Без .env бот и админка печатают этот текст вместо трейсбека pydantic
    monkeypatch.setenv("ADMIN_ID", "мой chat id")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "short")
    with pytest.raises(ValidationError) as bot_error:
        BotSettings.from_env()
    with pytest.raises(ValidationError) as admin_error:
        AdminSettings.from_env()

    text = describe_settings_error(bot_error.value)
    assert "TG_TOKEN: не задана" in text
    assert "ANTHROPIC_API_KEY: не задана" in text
    assert "ADMIN_ID: нужно целое число" in text
    assert ".env.example" in text
    assert "ADMIN_SECRET_KEY: слишком короткое значение" in describe_settings_error(admin_error.value)


def test_check_config_lists_problems_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "123:secret-token")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")

    problems = find_problems()

    assert len(problems) == 2  # и бот, и админка
    text = "\n".join(problems)
    assert "ADMIN_ID: не задана" in text
    assert "ADMIN_PASSWORD: слишком короткое значение" in text
    # Значения секретов в вывод не попадают: команда запускается в CI
    assert "secret-token" not in text
    assert "short" not in text.replace("слишком короткое", "")


def test_check_config_passes_with_all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "TG_TOKEN": "123:abc",
        "ADMIN_ID": "777",
        "ANTHROPIC_API_KEY": "key",
        "ADMIN_PASSWORD": "secret-pass",
        "ADMIN_SECRET_KEY": "k" * 32,
    }.items():
        monkeypatch.setenv(name, value)
    assert find_problems() == []
