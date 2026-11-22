"""Full-text search over the service catalog: SQLite FTS5 index and the query against it.

The `services_fts` index is external-content: texts live only in `services`, and triggers update
the index on every edit, including edits from the admin. The index source is the `services_fts_src` view,
where «ё» is replaced with «е»: the FTS5 tokenizer does not treat them as equal, and clients write «съемный»
instead of «съёмный». Triggers pass the same values to the index, otherwise 'delete' would remove something
other than what was written.

The index is created by a migration and, for `create_all` tests, by the `after_create` event on the `services`
table (see models.py). Caveats:
- SQLite recreates the table during a batch migration (ALTER TABLE in Alembic) and the triggers are lost:
  a migration that changes `services` must recreate them (`TRIGGERS`) and rebuild the index (`REBUILD`);
- `INSERT OR REPLACE` deletes a row without the delete trigger unless `recursive_triggers` is on
  (engine.py turns it on for every connection).
"""

import re

FTS_TABLE = "services_fts"
FTS_SOURCE = f"{FTS_TABLE}_src"


def _fold(column: str) -> str:
    """SQL expression: column text with «ё» → «е», same as in the query (`match_query`)."""
    return f"replace(replace({column}, 'ё', 'е'), 'Ё', 'Е')"


CREATE_SOURCE = (
    f"CREATE VIEW {FTS_SOURCE} AS SELECT id, {_fold('name')} AS name, {_fold('description')} AS description FROM services"
)
# unicode61 lowercases Cyrillic too; remove_diacritics 2 makes «pret» find «preț»
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

# How many query words to use: the model sends keywords, not a paragraph
MAX_TERMS = 8
# Maximum stem length: «имплантация» → «имплан*» also finds «Имплант»
MAX_STEM = 6
_WORD = re.compile(r"\w+")


def _stem(word: str) -> str:
    """Rough word stem: the beginning shared by different forms.

    FTS5 knows no Russian or Romanian endings, so we search by word prefix: short words lose
    their last two letters («зубов» → «зуб*» finds «зуба», «зубы»), long words lose three
    («удалить» → «удал*» finds «удаление», «чистку» → «чист*»). Numbers are kept as is.
    """
    if word.isdigit() or len(word) <= 3:
        return word
    if len(word) <= 5:
        return word[: max(3, len(word) - 2)]
    return word[: min(MAX_STEM, max(4, len(word) - 3))]


def match_query(text: str) -> str | None:
    """`MATCH` string built from the model's query, or None if there is nothing to search for.

    Each word is a separate quoted prefix term, so FTS5 syntax (AND, NEAR, «*», «:») in the query
    is not executed. Terms are joined with OR: any word finds an item, and bm25 sets the order.
    """
    found: list[str] = _WORD.findall(text.lower().replace("ё", "е"))
    words = [word for word in found if len(word) >= 3 or word.isdigit()]
    stems = list(dict.fromkeys(_stem(word) for word in words))[:MAX_TERMS]
    if not stems:
        return None
    # Numbers are matched whole: «500» must not find «5000»
    return " OR ".join(f'"{stem}"' if stem.isdigit() else f'"{stem}"*' for stem in stems)


def include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """Filter for Alembic schema comparison: the FTS index, its shadow tables and the source view
    are not described in the models."""
    return not (type_ == "table" and name is not None and name.startswith(FTS_TABLE))
