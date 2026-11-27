"""Settings from the environment: from_env() returns the right class and requires secrets."""

import re
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from medllmbot.check_config import find_deploy_problems, find_problems
from medllmbot.config import AdminSettings, BotSettings, DeploySettings, describe_settings_error

SECRETS = ("TG_TOKEN", "ADMIN_ID", "ANTHROPIC_API_KEY", "ADMIN_PASSWORD", "ADMIN_SECRET_KEY")
DEPLOY = ("COMPOSE_PROFILES", "DOMAIN", "TUNNEL_TOKEN")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Empty environment and a directory without .env, so the test does not depend on the developer's machine."""
    monkeypatch.chdir(tmp_path)
    for name in (*SECRETS, *DEPLOY, "DB_PATH"):
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
    # Without .env the bot and the admin print this text instead of a pydantic traceback
    monkeypatch.setenv("ADMIN_ID", "мой chat id")
    monkeypatch.setenv("ADMIN_SECRET_KEY", "short")
    with pytest.raises(ValidationError) as bot_error:
        BotSettings.from_env()
    with pytest.raises(ValidationError) as admin_error:
        AdminSettings.from_env()

    text = describe_settings_error(bot_error.value)
    assert "TG_TOKEN: not set" in text
    assert "ANTHROPIC_API_KEY: not set" in text
    assert "ADMIN_ID: must be an integer" in text
    assert ".env.example" in text
    assert "ADMIN_SECRET_KEY: value is too short" in describe_settings_error(admin_error.value)


def test_check_config_lists_problems_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", "123:secret-token")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")

    problems = find_problems()

    assert len(problems) == 2  # both the bot and the admin
    text = "\n".join(problems)
    assert "ADMIN_ID: not set" in text
    assert "ADMIN_PASSWORD: value is too short" in text
    # Secret values do not get into the output: the command runs in CI
    assert "secret-token" not in text
    assert "short" not in text.replace("too short", "")


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


def set_bot_and_admin_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "TG_TOKEN": "123:abc",
        "ADMIN_ID": "777",
        "ANTHROPIC_API_KEY": "key",
        "ADMIN_PASSWORD": "secret-pass",
        "ADMIN_SECRET_KEY": "k" * 32,
    }.items():
        monkeypatch.setenv(name, value)


def test_deploy_without_profiles_is_fine() -> None:
    # A local "docker compose up" publishes nothing, so it needs neither a domain nor a tunnel
    assert find_deploy_problems(DeploySettings()) == []


@pytest.mark.parametrize(("profile", "key"), [("tunnel", "TUNNEL_TOKEN"), ("https", "DOMAIN")])
def test_deploy_profile_needs_its_key(profile: str, key: str) -> None:
    assert find_deploy_problems(DeploySettings(compose_profiles=profile)) == [
        f"{key}: not set (needed by the {profile} profile in COMPOSE_PROFILES)"
    ]


def test_deploy_profile_with_its_key_passes() -> None:
    settings = DeploySettings(compose_profiles=" tunnel ,https", domain="demo.example.com", tunnel_token=SecretStr("t"))

    assert settings.profiles == ["tunnel", "https"]
    assert find_deploy_problems(settings) == []


def test_deploy_unknown_profile_is_reported() -> None:
    # A typo in the GitHub variable would otherwise deploy a server with nothing in front of the admin panel
    assert find_deploy_problems(DeploySettings(compose_profiles="tunel")) == [
        "COMPOSE_PROFILES: unknown profile 'tunel' (known: https, tunnel)"
    ]


def test_every_compose_profile_is_checked() -> None:
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    profiles = re.findall(r'^    profiles: \["(\w+)"\]$', compose, re.MULTILINE)
    assert sorted(profiles) == ["https", "tunnel"]
    for profile in profiles:
        # Known to check_config: without its key the profile is reported as missing it, not as unknown
        assert "unknown profile" not in "".join(find_deploy_problems(DeploySettings(compose_profiles=profile)))


def test_check_config_reports_deploy_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    set_bot_and_admin_keys(monkeypatch)
    monkeypatch.setenv("COMPOSE_PROFILES", "tunnel")

    assert find_problems() == ["DeploySettings:\n  TUNNEL_TOKEN: not set (needed by the tunnel profile in COMPOSE_PROFILES)"]

    monkeypatch.setenv("TUNNEL_TOKEN", "eyJhIjoi-secret")
    assert find_problems() == []
