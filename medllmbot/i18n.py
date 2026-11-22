"""Translation catalogs shared by the admin and the bot's messages to the administrator.

Source strings are English gettext msgids; Russian and Romanian live in `locales/<lang>/LC_MESSAGES/messages.po`
and are compiled in memory, so git holds only the readable .po files. The admin plugs the catalogs into SQLAdmin
(`medllmbot.admin.i18n`); the bot uses `translation()` for texts sent to the administrator in Telegram.

Enum labels, analytics topic names and attack-sign labels are msgids marked with `N_`: the domain code keeps them
as plain strings, the admin translates them when it renders a page.
"""

from collections.abc import Callable
from functools import cache
from io import BytesIO
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import NullTranslations, Translations

type Gettext = Callable[[str], str]  # translates a msgid into one language

LOCALES: tuple[str, ...] = ("ru", "ro", "en")  # in the order of the admin's language switcher
DEFAULT_LOCALE = "en"  # msgids are English, so English needs no catalog
LOCALES_DIR = Path(__file__).parent / "locales"
DOMAIN = "messages"  # our strings; "sqladmin" is the Romanian catalog of SQLAdmin's own strings


def N_(message: str) -> str:  # the conventional name of the gettext_noop marker
    """Returns the message unchanged; only the catalog extractor (`pybabel extract -k N_`) looks at it."""
    return message


def catalog_path(locale: str, domain: str = DOMAIN) -> Path:
    return LOCALES_DIR / locale / "LC_MESSAGES" / f"{domain}.po"


@cache
def _compiled(locale: str, domain: str) -> bytes:
    """A .po compiled to .mo in memory. Untranslated and fuzzy entries are left out: they show the English msgid."""
    with catalog_path(locale, domain).open("rb") as po:
        catalog = read_po(po, locale=locale, domain=domain)
    compiled = BytesIO()
    write_mo(compiled, catalog)
    return compiled.getvalue()


def load_catalog(locale: str, domain: str = DOMAIN) -> Translations:
    """A new catalog object each time: add_fallback() mutates the catalog, so a shared one would chain onto itself."""
    return Translations(BytesIO(_compiled(locale, domain)), domain=domain)


@cache
def translation(locale: str) -> NullTranslations:
    """Our catalog for one language, for code outside an admin request. English returns msgids as they are."""
    return NullTranslations() if locale == DEFAULT_LOCALE else load_catalog(locale)


def gettext_for(locale: str) -> Gettext:
    """gettext for one language, e.g. the administrator's (`bot_config.admin_language`)."""
    return translation(locale).gettext
