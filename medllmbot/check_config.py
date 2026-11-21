"""Проверка ключей без запуска бота: python -m medllmbot.check_config

Читает переменные окружения и .env так же, как бот и админка, и сообщает, чего не хватает.
Значения секретов не выводит — команду можно запускать в CI с ключами из GitHub Environment.
"""

import sys

from pydantic import ValidationError

from medllmbot.config import AdminSettings, AppSettings, BotSettings, describe_settings_error


def find_problems() -> list[str]:
    """Описания ошибок настроек бота и админки; пустой список — всё задано верно."""
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
    print("Ключи на месте: настройки бота и админки проходят проверку")
