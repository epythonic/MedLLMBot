"""Формы разделов админки: сообщения об ошибках на русском и дробные числа через запятую."""

import math
from typing import final, override

from wtforms import FloatField, Form
from wtforms.form import BaseForm
from wtforms.i18n import get_translations
from wtforms.meta import DefaultMeta
from wtforms.validators import StopValidation

# Язык интерфейса админки: SQLAdmin переводит по нему свои шаблоны, WTForms — ошибки в формах
LOCALE = "ru"

# В русском каталоге WTForms «at least» и «at most» переведены как «больше» и «меньше»:
# у цены с минимумом 0 ошибка звучала бы так, будто ноль вводить нельзя
_FIXES: dict[str, str] = {
    "Number must be at least %(min)s.": "Число должно быть не меньше %(min)s.",
    "Number must be at most %(max)s.": "Число должно быть не больше %(max)s.",
    "Number must be between %(min)s and %(max)s.": "Число должно быть от %(min)s до %(max)s.",
}


@final
class Translations:
    """Каталог сообщений WTForms на языке админки, с поправками неточных переводов."""

    def __init__(self, locale: str) -> None:
        self._catalog = get_translations([locale])

    def gettext(self, string: str) -> str:
        return _FIXES.get(string) or self._catalog.gettext(string)

    def ngettext(self, singular: str, plural: str, n: int) -> str:
        return self._catalog.ngettext(singular, plural, n)


TRANSLATIONS = Translations(LOCALE)


class AdminForm(Form):
    """Базовая форма разделов админки: SQLAdmin строит от неё формы создания и редактирования."""

    class Meta(DefaultMeta):
        @override
        def get_translations(self, form: BaseForm) -> Translations:
            return TRANSLATIONS


@final
class CommaFloatField(FloatField):
    """Дробное число через точку или запятую: в русской раскладке привычнее «0,15», чем «0.15»."""

    @override
    def process_formdata(self, valuelist: list[object]) -> None:
        if valuelist and valuelist[0] == "":
            self.data = None  # пустое поле оценят InputRequired или Optional: «Обязательное поле», а не «неверное число»
            return
        super().process_formdata([value.replace(",", ".") if isinstance(value, str) else value for value in valuelist])
        # float() принимает и «inf», и «nan» — для цены это не числа
        if isinstance(self.data, float) and not math.isfinite(self.data):
            self.data = None
            raise ValueError(self.gettext("Not a valid float value."))

    @override
    def pre_validate(self, form: BaseForm) -> None:
        # Число не разобрано — проверять его диапазон незачем: к «Неверное десятичное число»
        # добавилось бы сбивающее с толку «Число должно быть не меньше 0»
        if self.process_errors:
            raise StopValidation
