"""Admin UI languages: Russian, Romanian and English, on top of SQLAdmin's i18n.

The catalogs live in `medllmbot/locales` (`medllmbot.i18n`). SQLAdmin ships Russian for its own strings but no
Romanian: `locales/ro/LC_MESSAGES/sqladmin.po` fills that gap, and `install()` registers both languages in
SQLAdmin's i18n module.

The language is picked per request by SQLAdmin's LocaleMiddleware: `?lang=` from the switcher (remembered in
a cookie), then the cookie, then the browser's Accept-Language, otherwise English.
"""

from collections.abc import Callable
from typing import cast, final, override

from babel.support import LazyProxy, NullTranslations
from sqladmin import i18n as sqladmin_i18n
from sqladmin.i18n import I18nConfig, get_locale, gettext, ngettext

from medllmbot.i18n import DEFAULT_LOCALE, LOCALES, load_catalog

__all__ = [
    "DEFAULT_LOCALE",
    "I18N_CONFIG",
    "LOCALES",
    "get_locale",
    "gettext",
    "install",
    "lazy_gettext",
    "lazy_text",
    "ngettext",
]

I18N_CONFIG = I18nConfig(
    default_locale=DEFAULT_LOCALE,
    language_cookie_name="language",
    language_header_name="Accept-Language",
    language_switcher=list(LOCALES),
)

# SQLAdmin's own catalogs as shipped, before install() replaces them: install() can then run once per app
# (tests build many apps) without chaining our catalog onto itself
_SQLADMIN_CATALOGS: dict[str, NullTranslations] = dict(sqladmin_i18n.translations)


def _translations(locale: str) -> NullTranslations:
    """Our strings first, then SQLAdmin's: our own catalog of them for Romanian, the shipped one for Russian."""
    ours = load_catalog(locale)
    ours.add_fallback(load_catalog(locale, "sqladmin") if locale == "ro" else _SQLADMIN_CATALOGS[locale])
    return ours


def install() -> None:
    """Registers the admin languages in SQLAdmin and offers only them.

    SQLAdmin keeps its languages in module globals; LocaleMiddleware accepts a locale only from
    SUPPORTED_LOCALES, so a German browser gets English rather than a mix of German SQLAdmin and English.
    """
    for locale in LOCALES:
        if locale != DEFAULT_LOCALE:
            sqladmin_i18n.translations[locale] = _translations(locale)
    sqladmin_i18n.SUPPORTED_LOCALES[:] = list(LOCALES)


@final
class _LazyText(LazyProxy):
    """babel's lazy string made hashable: SQLAdmin turns the column_labels values into dict keys.

    The hash follows the current translation, as LazyProxy's equality does.
    """

    __slots__ = ()

    @override
    def __hash__(self) -> int:
        return hash(str(self))


def lazy_text(render: Callable[[], str]) -> str:
    """A string rendered anew each time it is shown, in the request's language. Typed as str: LazyProxy is not
    a str subclass, but SQLAdmin and WTForms only render these values."""
    return cast(str, cast(object, _LazyText(render, enable_cache=False)))


def lazy_gettext(message: str) -> str:
    """SQLAdmin's lazy_gettext that can also be a dict key: for class attributes and constants set at import."""
    return lazy_text(lambda: gettext(message))
