"""Check the keys without starting the bot: python -m medllmbot.check_config

Reads environment variables and .env the same way the bot and the admin panel do, and reports what is missing.
Never prints secret values, so the command can run in CI with keys from a GitHub Environment.
"""

import sys

from pydantic import ValidationError

from medllmbot.config import AdminSettings, AppSettings, BotSettings, describe_settings_error


def find_problems() -> list[str]:
    """Descriptions of bot and admin settings errors; an empty list means everything is set correctly."""
    problems: list[str] = []
    settings_classes: tuple[type[AppSettings], ...] = (BotSettings, AdminSettings)
    for settings in settings_classes:
        try:
            settings.from_env()
        except ValidationError as error:
            problems.append(f"{settings.__name__}: {describe_settings_error(error)}")
    return problems


if __name__ == "__main__":
    if problems := find_problems():
        sys.exit("\n\n".join(problems))
    print("Keys are in place: bot and admin settings pass the check")
