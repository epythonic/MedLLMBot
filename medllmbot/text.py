"""Text processing: normalization for word matching (topic analytics, requests for an operator),
the client's language for canned replies and stripping Markdown from model replies.
"""

import re
from enum import StrEnum

# Comparison ignoring case, «ё» and Romanian diacritics: «Приём» = «прием», «preț» = «pret».
# Claude's typographic apostrophe becomes a plain one: «don’t» = «don't»
_FOLD = str.maketrans({"ё": "е", "ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t", "’": "'"})

# Markup that Telegram without parse_mode shows as is. An asterisk touching the text on both sides is
# emphasis, while «5 * 3» and «2*2» are left alone
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_STAR_BULLET = re.compile(r"^([ \t]*)\*[ \t]+", re.MULTILINE)


def normalize(text: str) -> str:
    """Text for comparison: lowercase, «ё» → «е», Romanian letters without diacritics, a plain apostrophe,
    single spaces.
    """
    return " ".join(text.lower().translate(_FOLD).split())


class Language(StrEnum):
    """Client languages. Russian is the knowledge base language and the default reply language."""

    RU = "ru"
    RO = "ro"


_LETTER = re.compile(r"[^\W\d_]")
_WORD = re.compile(r"[^\W\d_]+")
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_ROMANIAN_LETTERS = re.compile(r"[ăâîșțşţ]", re.IGNORECASE)
# Common Romanian words, often written without diacritics: «buna ziua», «cat costa». Words that also occur
# in English («care», «am») or in Russian written in Latin letters («da», «nu», «telefon») are left out
_ROMANIAN_WORDS: frozenset[str] = frozenset(
    {
        "buna", "ziua", "seara", "salut", "multumesc", "va", "rog", "cat", "costa", "pret", "pretul", "preturile",
        "este", "sunt", "aveti", "avem", "puteti", "pot", "vreau", "doresc", "pentru", "unde", "cand", "cum",
        "si", "sau", "cu", "din", "mai", "imi", "mea", "meu", "comanda", "comenzii", "livrare", "livrarea",
        "programare", "ultimele", "cifre", "dinti", "operatorul", "numarul",
    }
)  # fmt: skip


def language_of(text: str) -> Language | None:
    """Language of a client message: Romanian letters or words mean Romanian, Cyrillic means Russian.

    Russian names in a Romanian message and Latin brand names in a Russian one do not flip the language while
    they make up less than half of the letters. None means the text does not tell: an order number, «S8?»,
    a command, English.
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
    """Language of the client's Telegram interface (`language_code`: «ro», «ru-RU»); None if other or unknown."""
    base = (code or "").split("-")[0].lower()
    return next((language for language in Language if language == base), None)


def reply_language(text: str, telegram_code: str | None = None) -> Language:
    """Language of a canned reply: the message language or, if the text does not tell, the client's Telegram
    language. Russian by default.
    """
    return language_of(text) or telegram_language(telegram_code) or Language.RU


def plain_text(text: str) -> str:
    """Model reply without Markdown: bold and italics as plain text, headings without «#», «*» lists with «-».

    The prompt already asks for no markup, but the model sometimes uses bold, and the client would see asterisks.
    Tables are not converted: they cannot be turned into plain text without losing meaning.
    """
    text = _STAR_BULLET.sub(r"\1- ", text)
    text = _HEADING.sub("", text)
    text = _BOLD.sub(lambda match: match.group(1) or match.group(2), text)
    text = text.replace("**", "")  # unpaired: the model cut the emphasis short
    return _ITALIC.sub(r"\1", text)
