"""Admin languages: complete catalogs, the language choice, labels that the domain code marks with N_."""

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqladmin
from babel.messages.extract import DEFAULT_KEYWORDS, extract_from_dir
from babel.messages.pofile import read_po
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqladmin import i18n as sqladmin_i18n

from medllmbot.admin import i18n
from medllmbot.admin.app import create_app
from medllmbot.config import AdminSettings
from medllmbot.db.engine import create_engine
from medllmbot.db.migrate import upgrade_db
from medllmbot.db.models import AuditOutcome, BusinessProfile, LeadStatus, OrderStatus
from medllmbot.i18n import catalog_path

ROOT = Path(__file__).parent.parent
TRANSLATED = ("ru", "ro")  # English is the source language
PLACEHOLDER = re.compile(r"%\((\w+)\)[sd]")


def code_msgids() -> set[str]:
    """msgids in the code and templates, extracted the same way as `pybabel extract -F babel.cfg`."""
    method_map = [("medllmbot/**.py", "python"), ("medllmbot/admin/templates/**.html", "jinja2")]
    keywords = {**DEFAULT_KEYWORDS, "lazy_gettext": None, "N_": None}
    found: set[str] = set()
    for _file, _line, message, _comments, _context in extract_from_dir(
        str(ROOT), method_map, keywords=keywords, options_map={"medllmbot/admin/templates/**.html": {}}
    ):
        found.add(message[0] if isinstance(message, tuple) else message)
    return found


def catalog(locale: str, domain: str = "messages") -> dict[str, tuple[str, ...]]:
    """msgid → translated forms: one for a plain message, one per plural form; empty strings if untranslated."""
    with catalog_path(locale, domain).open("rb") as po:
        messages = [m for m in read_po(po) if m.id and not m.fuzzy]
    out: dict[str, tuple[str, ...]] = {}
    for message in messages:
        msgid = message.id[0] if isinstance(message.id, (tuple, list)) else message.id
        forms = message.string if isinstance(message.string, (tuple, list)) else (message.string,)
        out[str(msgid)] = tuple(str(form or "") for form in forms)
    return out


@pytest.fixture(scope="module")
def msgids() -> set[str]:
    return code_msgids()


class TestCatalogs:
    @pytest.mark.parametrize("locale", TRANSLATED)
    def test_every_string_is_translated(self, msgids: set[str], locale: str) -> None:
        # A new UI string without a translation would show up in English in the Russian or Romanian admin
        translations = catalog(locale)
        untranslated = sorted(m for m in msgids if not all(translations.get(m, ("",))))
        assert untranslated == [], f"{locale}: run pybabel extract/update and translate these (see CLAUDE.md)"

    @pytest.mark.parametrize("locale", TRANSLATED)
    def test_no_stale_entries(self, msgids: set[str], locale: str) -> None:
        assert sorted(set(catalog(locale)) - msgids) == []

    @pytest.mark.parametrize("locale", TRANSLATED)
    def test_placeholders_match(self, locale: str) -> None:
        # «%(count)s» missing from a translation raises KeyError at runtime or silently drops the number
        for msgid, forms in catalog(locale).items():
            for form in forms:
                assert set(PLACEHOLDER.findall(form)) <= set(PLACEHOLDER.findall(msgid)), (locale, msgid, form)

    def test_domain_labels_are_marked(self, msgids: set[str]) -> None:
        # Enum values are English msgids: every one must reach the catalogs, or the admin shows it untranslated
        labels = {e.value for enum in (LeadStatus, OrderStatus, AuditOutcome, BusinessProfile) for e in enum}
        assert labels <= msgids

    def test_romanian_covers_sqladmin(self) -> None:
        # SQLAdmin ships no Romanian; our catalog of its strings must keep up when SQLAdmin adds new ones
        pot = Path(os.path.dirname(sqladmin.__file__)) / "translations" / "admin.pot"
        with pot.open("rb") as file:
            theirs = {str(m.id) for m in read_po(file) if m.id}
        ours = catalog("ro", "sqladmin")
        assert sorted(m for m in theirs if not all(ours.get(m, ("",)))) == []


class TestInstall:
    def test_offers_only_admin_languages(self) -> None:
        i18n.install()
        assert sqladmin_i18n.SUPPORTED_LOCALES == ["ru", "ro", "en"]

    def test_repeated_install_does_not_chain_catalogs(self) -> None:
        # install() runs once per app; an untranslated string must still resolve (a looped fallback chain hangs)
        for _ in range(3):
            i18n.install()
        sqladmin_i18n.set_locale("ro")
        try:
            assert sqladmin_i18n.gettext("Logout") == "Ieșire"  # SQLAdmin's string, from our Romanian catalog
            assert sqladmin_i18n.gettext("no such message") == "no such message"
        finally:
            sqladmin_i18n.set_locale("en")


@pytest.fixture
def anonymous(tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "admin.db"
    upgrade_db(db)
    settings = AdminSettings.model_construct(
        admin_username="admin",
        admin_password=SecretStr("secret-pass"),
        admin_secret_key=SecretStr("k" * 32),
        admin_https_only=False,
        db_path=db,
    )
    with TestClient(create_app(settings, engine=create_engine(db))) as client:
        yield client


class TestLanguageChoice:
    """The login page is enough: SQLAdmin's LocaleMiddleware picks the language for every admin route."""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("ro-RO,ro;q=0.9,en;q=0.8", "Parolă"),
            ("ru", "Пароль"),
            ("en-US", "Password"),
            ("de-DE,de;q=0.9", "Password"),  # a language the admin does not offer — English, not German
            (None, "Password"),
        ],
    )
    def test_browser_language(self, anonymous: TestClient, header: str | None, expected: str) -> None:
        headers = {"Accept-Language": header} if header else {}
        assert expected in anonymous.get("/admin/login", headers=headers).text

    def test_switcher_choice_is_remembered(self, anonymous: TestClient) -> None:
        response = anonymous.get("/admin/login?lang=ro", headers={"Accept-Language": "ru"})
        assert "Parolă" in response.text
        assert "language=ro" in response.headers["set-cookie"]
        # The cookie wins over the browser language on the next request
        assert "Parolă" in anonymous.get("/admin/login", headers={"Accept-Language": "ru"}).text
