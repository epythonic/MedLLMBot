"""Check the keys without starting the bot: python -m medllmbot.check_config

Reads environment variables and .env the same way the bot and the admin panel do, and reports what is missing.
Never prints secret values, so the command can run in CI with keys from a GitHub Environment.
"""

import sys

from pydantic import ValidationError

from medllmbot.config import AdminSettings, AppSettings, BotSettings, DeploySettings, describe_settings_error


def find_deploy_problems(settings: DeploySettings) -> list[str]:
    """Unknown profiles in COMPOSE_PROFILES and profiles without their key; no profile at all (a local run) is fine."""
    # Profiles of docker-compose.yml → the variable the profile cannot start without, and whether it is set
    required: dict[str, tuple[str, bool]] = {
        "https": ("DOMAIN", bool(settings.domain.strip())),
        "tunnel": ("TUNNEL_TOKEN", bool(settings.tunnel_token.get_secret_value().strip())),
    }
    problems: list[str] = []
    for profile in settings.profiles:
        if profile not in required:
            problems.append(f"COMPOSE_PROFILES: unknown profile {profile!r} (known: {', '.join(required)})")
            continue
        name, is_set = required[profile]
        if not is_set:
            problems.append(f"{name}: not set (needed by the {profile} profile in COMPOSE_PROFILES)")
    return problems


def find_problems() -> list[str]:
    """Descriptions of bot, admin and deploy settings errors; an empty list means everything is set correctly."""
    problems: list[str] = []
    settings_classes: tuple[type[AppSettings], ...] = (BotSettings, AdminSettings)
    for settings in settings_classes:
        try:
            settings.from_env()
        except ValidationError as error:
            problems.append(f"{settings.__name__}: {describe_settings_error(error)}")
    if deploy_problems := find_deploy_problems(DeploySettings.from_env()):
        problems.append("DeploySettings:\n" + "\n".join(f"  {problem}" for problem in deploy_problems))
    return problems


if __name__ == "__main__":
    if problems := find_problems():
        sys.exit("\n\n".join(problems))
    print("Keys are in place: bot, admin and deploy settings pass the check")
