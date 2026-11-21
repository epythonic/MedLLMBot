"""Обработка текста: нормализация для поиска по словам (аналитика тем, просьба позвать оператора),
язык клиента для готовых ответов и очистка ответа модели от Markdown.
"""

import re
from enum import StrEnum

# Сравнение без регистра, «ё» и румынских диакритик: «Приём» = «прием», «preț» = «pret».
# Типографский апостроф Claude — обычный: «don’t» = «don't»
_FOLD = str.maketrans({"ё": "е", "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t", "’": "'"})

# Разметка, которую Telegram без parse_mode показывает как есть. Звёздочка вплотную к тексту с обеих сторон —
# выделение, а «5 * 3» и «2*2» не трогаем
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_STAR_BULLET = re.compile(r"^([ \t]*)\*[ \t]+", re.MULTILINE)


def normalize(text: str) -> str:
    """Текст для сравнения: строчные буквы, «ё» → «е», румынские буквы без диакритики и обычный апостроф,
    одиночные пробелы.
    """
    return " ".join(text.lower().translate(_FOLD).split())


class Language(StrEnum):
    """Языки клиентов. Русский — язык базы знаний и ответ по умолчанию."""

    RU = "ru"
    RO = "ro"


_LETTER = re.compile(r"[^\W\d_]")
_WORD = re.compile(r"[^\W\d_]+")
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_ROMANIAN_LETTERS = re.compile(r"[ăâîșțşţ]", re.IGNORECASE)
# Частые румынские слова — их пишут и без диакритики: «buna ziua», «cat costa». Слов, которые бывают
# в английском («care», «am») или в русском латиницей («da», «nu», «telefon»), здесь нет
_ROMANIAN_WORDS: frozenset[str] = frozenset(
    {
        "buna", "ziua", "seara", "salut", "multumesc", "va", "rog", "cat", "costa", "pret", "pretul", "preturile",
        "este", "sunt", "aveti", "avem", "puteti", "pot", "vreau", "doresc", "pentru", "unde", "cand", "cum",
        "si", "sau", "cu", "din", "mai", "imi", "mea", "meu", "comanda", "comenzii", "livrare", "livrarea",
        "programare", "ultimele", "cifre", "dinti", "operatorul", "numarul",
    }
)  # fmt: skip


def language_of(text: str) -> Language | None:
    """Язык сообщения клиента: румынские буквы или слова — румынский, кириллица — русский.

    Русские названия в румынском сообщении и латинские марки в русском язык не переворачивают, пока их
    меньше половины букв. None — по тексту не понять: номер заказа, «S8?», команда, английский.
    """
    letters: list[str] = _LETTER.findall(text)
    if not letters:
        return None
    cyrillic = sum(1 for letter in letters if _CYRILLIC.fullmatch(letter))
    if cyrillic * 2 < len(letters) and (
        _ROMANIAN_LETTERS.search(text) or not _ROMANIAN_WORDS.isdisjoint(_WORD.findall(normalize(text)))
    ):
        return Language.RO
    return Language.RU if cyrillic else None


def telegram_language(code: str | None) -> Language | None:
    """Язык интерфейса Telegram клиента (`language_code`: «ro», «ru-RU»); None — другой язык или неизвестен."""
    base = (code or "").split("-")[0].lower()
    return next((language for language in Language if language == base), None)


def reply_language(text: str, telegram_code: str | None = None) -> Language:
    """Язык готового ответа: язык сообщения, а если по тексту не понять — язык Telegram клиента.
    По умолчанию — русский.
    """
    return language_of(text) or telegram_language(telegram_code) or Language.RU


def plain_text(text: str) -> str:
    """Ответ модели без Markdown: жирный и курсив — обычным текстом, заголовки без «#», списки «*» — через «-».

    Промпт уже просит писать без разметки, но модель иногда выделяет жирным — клиент увидел бы звёздочки.
    Таблицы не переделываются: их не перевести в обычный текст без потери смысла.
    """
    text = _STAR_BULLET.sub(r"\1- ", text)
    text = _HEADING.sub("", text)
    text = _BOLD.sub(lambda match: match.group(1) or match.group(2), text)
    text = text.replace("**", "")  # непарные: модель оборвала выделение
    return _ITALIC.sub(r"\1", text)
