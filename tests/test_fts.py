"""Catalog full-text index query: word stems and escaping of FTS5 syntax."""

import pytest

from medllmbot.db.fts import FTS_TABLE, MAX_TERMS, include_name, match_query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("чистка", '"чист"*'),
        ("чистку зубов", '"чист"* OR "зуб"*'),
        ("удалить", '"удал"*'),
        # A long word is cut to six letters so that the shorter «Имплант» is found too
        ("имплантация", '"имплан"*'),
        ("Рентген", '"рент"*'),
        ("ZOOM", '"zoo"*'),
        ("albire dinți", '"albi"* OR "din"*'),
        ("коронка 5000", '"коро"* OR "5000"'),
    ],
)
def test_words_become_prefix_terms(query: str, expected: str) -> None:
    assert match_query(query) == expected


@pytest.mark.parametrize("query", ["", "  ", "?!", "я в"])
def test_query_without_words_lists_catalog(query: str) -> None:
    assert match_query(query) is None


@pytest.mark.parametrize("query", ["NEAR(a b) OR *", 'name: "x" AND -y', "x' OR '1'='1"])
def test_fts_syntax_is_not_executed(query: str) -> None:
    # Only quoted words remain in the query: FTS5 operators and the client's quotes do not get through
    match = match_query(query)
    assert match is not None
    terms = match.split(" OR ")
    assert all(term.startswith('"') and term.rstrip("*").endswith('"') for term in terms)
    assert all(term.count('"') == 2 for term in terms)


def test_repeated_words_and_term_limit() -> None:
    assert match_query("зуб зубы зубов") == '"зуб"*'
    words = " ".join(f"слово{i:02}" for i in range(MAX_TERMS + 5))
    assert match_query(words) is not None
    assert len((match_query(words) or "").split(" OR ")) <= MAX_TERMS


def test_alembic_ignores_fts_tables() -> None:
    for name in (FTS_TABLE, f"{FTS_TABLE}_data", f"{FTS_TABLE}_idx"):
        assert not include_name(name, "table", {})
    assert include_name("services", "table", {})
    assert include_name(FTS_TABLE, "column", {})
