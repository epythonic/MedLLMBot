"""Admin section forms: error messages in the admin language and decimal numbers with a comma."""

import math
from typing import final, override

from wtforms import FloatField, Form
from wtforms.form import BaseForm
from wtforms.i18n import get_translations
from wtforms.meta import DefaultMeta
from wtforms.validators import StopValidation

from medllmbot.admin.i18n import LOCALES, get_locale

# The WTForms Russian catalog translates «at least» and «at most» as «больше» and «меньше»:
# for a price with a minimum of 0 the error would read as if zero were not allowed
_FIXES: dict[str, dict[str, str]] = {
    "ru": {
        "Number must be at least %(min)s.": "Число должно быть не меньше %(min)s.",
        "Number must be at most %(max)s.": "Число должно быть не больше %(max)s.",
        "Number must be between %(min)s and %(max)s.": "Число должно быть от %(min)s до %(max)s.",
    },
}


@final
class Translations:
    """WTForms message catalog for one admin language, with fixes for inaccurate translations."""

    def __init__(self, locale: str) -> None:
        self._catalog = get_translations([locale])
        self._fixes = _FIXES.get(locale, {})

    def gettext(self, string: str) -> str:
        return self._fixes.get(string) or self._catalog.gettext(string)

    def ngettext(self, singular: str, plural: str, n: int) -> str:
        return self._catalog.ngettext(singular, plural, n)


TRANSLATIONS: dict[str, Translations] = {locale: Translations(locale) for locale in LOCALES}


class AdminForm(Form):
    """Base form for admin sections: SQLAdmin builds create and edit forms from it."""

    class Meta(DefaultMeta):
        @override
        def get_translations(self, form: BaseForm) -> Translations:
            # The request language, set by SQLAdmin's LocaleMiddleware
            return TRANSLATIONS[get_locale()]


@final
class CommaFloatField(FloatField):
    """Decimal number with a dot or a comma: on a Russian keyboard «0,15» is more natural than «0.15»."""

    @override
    def process_formdata(self, valuelist: list[object]) -> None:
        if valuelist and valuelist[0] == "":
            self.data = None  # InputRequired or Optional judge an empty field: «Обязательное поле», not «неверное число»
            return
        super().process_formdata([value.replace(",", ".") if isinstance(value, str) else value for value in valuelist])
        # float() accepts «inf» and «nan», which are not numbers for a price
        if isinstance(self.data, float) and not math.isfinite(self.data):
            self.data = None
            message = "Not a valid float value."  # WTForms' own catalog translates it, not ours
            raise ValueError(self.gettext(message))

    @override
    def pre_validate(self, form: BaseForm) -> None:
        # The number was not parsed, so there is no point checking its range: «Неверное десятичное число»
        # would get a confusing «Число должно быть не меньше 0» added to it
        if self.process_errors:
            raise StopValidation
