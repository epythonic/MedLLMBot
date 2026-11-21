"""Формы админки: сообщения об ошибках на русском и дробные числа через запятую."""

from typing import final

import pytest
from starlette.datastructures import FormData
from wtforms import IntegerField, StringField
from wtforms.validators import InputRequired, Length, NumberRange

from medllmbot.admin.forms import AdminForm, CommaFloatField


@final
class PriceForm(AdminForm):
    name = StringField(validators=[InputRequired(), Length(max=10)])
    price = CommaFloatField(validators=[InputRequired(), NumberRange(min=0)])
    count = IntegerField(validators=[NumberRange(min=1, max=5)])


def submit(**fields: str) -> PriceForm:
    form = PriceForm(FormData({"name": "Чистка", "price": "1", "count": "1"} | fields))
    form.validate()
    return form


class TestCommaFloatField:
    @pytest.mark.parametrize("raw", ["0.15", "0,15", " 0,15 "])
    def test_dot_and_comma(self, raw: str) -> None:
        form = submit(price=raw)
        assert form.price.errors == []
        assert form.price.data == 0.15

    def test_garbage_gives_one_clear_error(self) -> None:
        # Без остановки проверок к ошибке разбора добавилось бы «Число должно быть не меньше 0»
        assert submit(price="1,2,3").price.errors == ["Неверное десятичное число."]

    def test_empty_is_required_not_invalid(self) -> None:
        assert submit(price="").price.errors == ["Обязательное поле."]

    def test_range_still_checked(self) -> None:
        assert submit(price="-0,5").price.errors == ["Число должно быть не меньше 0."]


class TestRussianMessages:
    def test_required(self) -> None:
        assert submit(name="").name.errors == ["Обязательное поле."]

    def test_length_uses_plural_forms(self) -> None:
        assert submit(name="Профессиональная чистка").name.errors == ["Значение не должно содержать более 10 символов."]

    def test_between_is_fixed(self) -> None:
        # В каталоге WTForms — «Значение должно быть между 1 и 5»; выравниваем со «не меньше / не больше»
        assert submit(count="9").count.errors == ["Число должно быть от 1 до 5."]

    def test_integer(self) -> None:
        assert "Неверное целое число." in submit(count="два").count.errors
