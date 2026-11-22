"""Process settings from environment variables and .env.

Only secrets and infrastructure live here. The bot's model, limits and texts are stored
in the DB (the bot_config table) and edited in the admin panel.
"""

from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Shared by the bot, the admin panel and migrations."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    db_path: Path = Path("storage/bot.db")

    @classmethod
    def from_env(cls) -> Self:
        """Reads settings from the environment and .env.

        Required fields are filled in by pydantic-settings, so calling without arguments
        is correct, even though the type checker sees missing parameters in `cls()`. Here, in the base
        class without required fields, it has nothing to complain about.
        """
        return cls()


class BotSettings(AppSettings):
    tg_token: SecretStr  # token from @BotFather
    admin_id: int  # chat id of the administrator who receives the leads
    anthropic_api_key: SecretStr


class AdminSettings(AppSettings):
    admin_username: str = "admin"
    admin_password: SecretStr = Field(min_length=8)
    admin_secret_key: SecretStr = Field(min_length=32)  # signs the session cookie
    admin_host: str = "127.0.0.1"
    admin_port: int = 8000
    # true when the admin panel is behind an HTTPS reverse proxy: the session cookie is not sent over plain HTTP
    admin_https_only: bool = False


# Pydantic error reasons in plain language; the rest are shown as is
_REASONS: dict[str, str] = {
    "missing": "not set",
    "int_parsing": "must be an integer",
    "bool_parsing": "must be true or false",
    "string_too_short": "value is too short",
    "too_short": "value is too short (password: at least 8 characters, secret key: at least 32)",
}


def describe_settings_error(error: ValidationError) -> str:
    """What is wrong with .env, as a list of variables, without the pydantic traceback."""
    lines = [
        f"  {'.'.join(str(part) for part in item['loc']).upper()}: {_REASONS.get(item['type'], item['msg'])}"
        for item in error.errors()
    ]
    return (
        "Environment variables are missing or invalid:\n"
        + "\n".join(lines)
        + "\nFill in .env following .env.example (see the Quick start section in README.md)."
    )
