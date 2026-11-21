"""Настройки процессов из переменных окружения и .env.

Здесь только секреты и инфраструктура. Модель, лимиты и тексты бота хранятся
в БД (таблица bot_config) и редактируются в админке.
"""

from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Общее для бота, админки и миграций."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    db_path: Path = Path("storage/bot.db")

    @classmethod
    def from_env(cls) -> Self:
        """Читает настройки из окружения и .env.

        Обязательные поля заполняет pydantic-settings, поэтому вызов без аргументов
        корректен, хотя тайпчекер видит в `cls()` пропущенные параметры. Здесь, в базовом
        классе без обязательных полей, ему не на что жаловаться.
        """
        return cls()


class BotSettings(AppSettings):
    tg_token: SecretStr  # токен от @BotFather
    admin_id: int  # chat id администратора, которому уходят заявки
    anthropic_api_key: SecretStr


class AdminSettings(AppSettings):
    admin_username: str = "admin"
    admin_password: SecretStr = Field(min_length=8)
    admin_secret_key: SecretStr = Field(min_length=32)  # подпись cookie сессии
    admin_host: str = "127.0.0.1"
    admin_port: int = 8000
    # true, когда админка открыта по HTTPS (за reverse proxy): cookie сессии не уйдёт по голому HTTP
    admin_https_only: bool = False


# Причины ошибок pydantic человеческим языком; остальные выводятся как есть
_REASONS: dict[str, str] = {
    "missing": "не задана",
    "int_parsing": "нужно целое число",
    "bool_parsing": "нужно true или false",
    "string_too_short": "слишком короткое значение",
    "too_short": "слишком короткое значение (пароль — от 8 символов, секретный ключ — от 32)",
}


def describe_settings_error(error: ValidationError) -> str:
    """Что не так с .env — списком переменных, без трейсбека pydantic."""
    lines = [
        f"  {'.'.join(str(part) for part in item['loc']).upper()}: {_REASONS.get(item['type'], item['msg'])}"
        for item in error.errors()
    ]
    return (
        "Не заданы или заданы неверно переменные окружения:\n"
        + "\n".join(lines)
        + "\nЗаполните .env по образцу .env.example (см. README, раздел «Быстрый старт»)."
    )
