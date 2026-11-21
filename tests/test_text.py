"""Обработка текста: очистка ответа модели от Markdown и язык клиента для готовых ответов."""

import pytest

from evals.shop.grading import MARKDOWN
from medllmbot.text import Language, language_of, plain_text, reply_language


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


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Сколько стоит чистка?", Language.RU),
        ("Сколько стоит Brisko Clean S8 Pro Max?", Language.RU),  # латинская марка в русском вопросе
        ("Cât costă o curățare?", Language.RO),
        ("cat costa o curatare?", Language.RO),  # без диакритики — по частым словам
        ("Buna ziua! Unde este comanda mea DE-10022?", Language.RO),
        ("Aveți livrare în Bălți?", Language.RO),
        ("Cât costă Робот-пылесос Brisko?", Language.RO),  # русское название из каталога в румынском вопросе
        ("S8?", None),
        ("0102", None),
        ("/operator", None),
        ("skolko stoit chistka", None),  # русский латиницей — не румынский
        ("moi telefon 069123456", None),
        ("How much is it?", None),
        ("", None),
    ],
)
def test_language_of(text: str, language: Language | None) -> None:
    assert language_of(text) is language


@pytest.mark.parametrize(
    ("text", "telegram_language", "language"),
    [
        ("Cât costă?", "ru", Language.RO),  # язык сообщения важнее языка Telegram
        ("Сколько стоит?", "ro", Language.RU),
        ("S8?", "ro", Language.RO),
        ("S8?", "ro-MD", Language.RO),
        ("S8?", "en", Language.RU),
        ("S8?", None, Language.RU),
    ],
)
def test_reply_language(text: str, telegram_language: str | None, language: Language) -> None:
    assert reply_language(text, telegram_language) is language
