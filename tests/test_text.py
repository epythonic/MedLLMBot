"""Очистка ответа модели от Markdown: Telegram без parse_mode показывает разметку клиенту как есть."""

import pytest

from evals.shop.grading import MARKDOWN
from medllmbot.text import plain_text


@pytest.mark.parametrize(
    ("answer", "plain"),
    [
        # Ответы из оценки магазина: клиент видел звёздочки
        ("**Tavrix Strike 17**: 27 999 лей", "Tavrix Strike 17: 27 999 лей"),
        (
            'Монитор **Lumetra 27" 2K 165 Гц** за **4 799 лей**.',
            'Монитор Lumetra 27" 2K 165 Гц за 4 799 лей.',
        ),
        ("1. **Kivora X5 128 ГБ, 4299 лей.** Батарея 5000 мА·ч.", "1. Kivora X5 128 ГБ, 4299 лей. Батарея 5000 мА·ч."),
        ("__Важно__: гарантия 24 месяца", "Важно: гарантия 24 месяца"),
        ("*Внимание*: наличие подтвердит менеджер", "Внимание: наличие подтвердит менеджер"),
        ("***Скидок нет***", "Скидок нет"),
        ("## Ноутбуки\nVoltix Air 14", "Ноутбуки\nVoltix Air 14"),
        ("Варианты:\n* Aerelo Buds\n  * Aerelo Buds Pro", "Варианты:\n- Aerelo Buds\n  - Aerelo Buds Pro"),
        ("Оборванное **выделение", "Оборванное выделение"),
    ],
)
def test_markdown_removed(answer: str, plain: str) -> None:
    assert plain_text(answer) == plain
    assert MARKDOWN.search(plain_text(answer)) is None  # та же проверка формата, что в оценке магазина


@pytest.mark.parametrize(
    "answer",
    [
        "Чистка зубов стоит 800 лей.",
        "Варианты:\n- Aerelo Buds\n- Aerelo Buds Pro",  # список через тире — обычный текст
        "Площадь 5 * 3 м, коэффициент 2*2.",  # звёздочка без выделения
        "Заказ DE-10022, раздел #3, имя файла report_2026_final.pdf",
        'Экран 15,6", 8 ГБ ОЗУ — подойдёт для учёбы.',
    ],
)
def test_plain_text_unchanged(answer: str) -> None:
    assert plain_text(answer) == answer
