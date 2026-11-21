"""Полнотекстовый поиск по каталогу услуг: индекс SQLite FTS5 и запрос к нему.

Индекс `services_fts` — внешнее содержимое: тексты хранятся только в `services`, а триггеры обновляют
индекс при каждой правке, в том числе из админки. Источник индекса — представление `services_fts_src`,
где «ё» заменена на «е»: токенизатор FTS5 их не отождествляет, а клиенты пишут «съемный» вместо «съёмный».
Триггеры передают в индекс те же значения, иначе команда 'delete' удалила бы не то, что было записано.

Индекс создаётся миграцией и, для тестов на `create_all`, событием `after_create` у таблицы `services`
(см. models.py). Ограничения:
- SQLite пересоздаёт таблицу при batch-миграции (ALTER TABLE в Alembic), и триггеры при этом пропадают:
  миграция, которая меняет `services`, должна создать их заново (`TRIGGERS`) и перестроить индекс (`REBUILD`);
- `INSERT OR REPLACE` удаляет строку без триггера удаления, если не включён `recursive_triggers`
  (он включается для каждого соединения в engine.py).
"""

import re

FTS_TABLE = "services_fts"
FTS_SOURCE = f"{FTS_TABLE}_src"


def _fold(column: str) -> str:
    """SQL-выражение: текст колонки с «ё» → «е» — так же, как в запросе (`match_query`)."""
    return f"replace(replace({column}, 'ё', 'е'), 'Ё', 'Е')"


CREATE_SOURCE = (
    f"CREATE VIEW {FTS_SOURCE} AS SELECT id, {_fold('name')} AS name, {_fold('description')} AS description FROM services"
)
# unicode61 приводит к нижнему регистру и кириллицу; remove_diacritics 2 — «preț» находится по «pret»
CREATE_TABLE = (
    f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
    f"name, description, content='{FTS_SOURCE}', content_rowid='id', tokenize='unicode61 remove_diacritics 2')"
)
_NEW = f"new.id, {_fold('new.name')}, {_fold('new.description')}"
_OLD = f"old.id, {_fold('old.name')}, {_fold('old.description')}"
TRIGGERS: tuple[str, ...] = (
    f"""CREATE TRIGGER {FTS_TABLE}_ai AFTER INSERT ON services BEGIN
    INSERT INTO {FTS_TABLE}(rowid, name, description) VALUES ({_NEW});
END""",
    f"""CREATE TRIGGER {FTS_TABLE}_ad AFTER DELETE ON services BEGIN
    INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, name, description) VALUES ('delete', {_OLD});
END""",
    f"""CREATE TRIGGER {FTS_TABLE}_au AFTER UPDATE ON services BEGIN
    INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, name, description) VALUES ('delete', {_OLD});
    INSERT INTO {FTS_TABLE}(rowid, name, description) VALUES ({_NEW});
END""",
)
CREATE: tuple[str, ...] = (CREATE_SOURCE, CREATE_TABLE, *TRIGGERS)
REBUILD = f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')"
DROP: tuple[str, ...] = (
    *(f"DROP TRIGGER IF EXISTS {FTS_TABLE}_{suffix}" for suffix in ("ai", "ad", "au")),
    f"DROP TABLE IF EXISTS {FTS_TABLE}",
    f"DROP VIEW IF EXISTS {FTS_SOURCE}",
)

# Сколько слов запроса учитывать: модель присылает ключевые слова, а не абзац
MAX_TERMS = 8
# Длиннее основа не бывает: «имплантация» → «имплан*» находит и «Имплант»
MAX_STEM = 6
_WORD = re.compile(r"\w+")


def _stem(word: str) -> str:
    """Грубая основа слова — начало, общее для разных форм.

    FTS5 не знает русских и румынских окончаний, поэтому ищем по началу слова: у коротких слов
    отрезаем две последние буквы («зубов» → «зуб*» находит «зуба», «зубы»), у длинных — три
    («удалить» → «удал*» находит «удаление», «чистку» → «чист*»). Числа — как есть.
    """
    if word.isdigit() or len(word) <= 3:
        return word
    if len(word) <= 5:
        return word[: max(3, len(word) - 2)]
    return word[: min(MAX_STEM, max(4, len(word) - 3))]


def match_query(text: str) -> str | None:
    """Строка для `MATCH` из запроса модели или None, если искать не по чему.

    Каждое слово — отдельный префиксный термин в кавычках, поэтому синтаксис FTS5 (AND, NEAR, «*», «:»)
    из запроса не исполняется. Термины через OR: позицию находит любое слово, а порядок задаёт bm25.
    """
    found: list[str] = _WORD.findall(text.lower().replace("ё", "е"))
    words = [word for word in found if len(word) >= 3 or word.isdigit()]
    stems = list(dict.fromkeys(_stem(word) for word in words))[:MAX_TERMS]
    if not stems:
        return None
    # Число ищем целиком: «500» не должно находить «5000»
    return " OR ".join(f'"{stem}"' if stem.isdigit() else f'"{stem}"*' for stem in stems)


def include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """Фильтр для сравнения схемы в Alembic: индекс FTS, его служебные таблицы и представление-источник
    не описаны в моделях."""
    return not (type_ == "table" and name is not None and name.startswith(FTS_TABLE))
