"""Admin help is checked against the code: new sections, columns and totals do not go unexplained."""

import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from sqladmin import ModelView

from medllmbot.admin.help import FLAG_HELP, GAP_HELP, HELP, OUTCOME_HELP, TOPICS, HelpTopic, help_topic
from medllmbot.admin.views import VIEWS
from medllmbot.analytics import GapReason
from medllmbot.db.models import AuditOutcome
from medllmbot.i18n import LOCALES
from medllmbot.security import FLAG_LABELS

# Custom admin pages without a model: Costs → Monthly report and Clients → Analytics
CUSTOM_PAGES = {"costs", "analytics"}
TEMPLATES = Path(__file__).parent.parent / "medllmbot" / "admin" / "templates"


def _names(attrs: Iterable[object]) -> set[str]:
    """Field names from column_list / form_columns, which hold model attributes or strings."""
    assert not isinstance(attrs, str), "column_list = '__all__' is not supported: list the columns explicitly"
    names: set[str] = set()
    for attr in attrs:
        key = getattr(attr, "key", attr)
        assert isinstance(key, str)
        names.add(key)
    return names


def _view_id(view: type[ModelView]) -> str:
    return view.identity


def test_every_section_has_help() -> None:
    assert set(HELP) == {view.identity for view in VIEWS} | CUSTOM_PAGES


def test_identities_are_unique() -> None:
    assert len(HELP) == len(TOPICS)


@pytest.mark.parametrize("view", VIEWS, ids=_view_id)
@pytest.mark.usefixtures("russian_admin")
def test_title_matches_menu(view: type[ModelView]) -> None:
    # The help is in Russian; the menu name is a translated string, compared in Russian
    assert HELP[view.identity].title == str(view.name_plural)


@pytest.mark.parametrize("view", VIEWS, ids=_view_id)
def test_every_column_and_form_field_has_hint(view: type[ModelView]) -> None:
    # SQLAdmin declares these attributes as InstrumentedAttribute without a type parameter; only the library can fix it
    shown = _names(view.column_list) | _names(view.form_columns)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    missing = shown - set(HELP[view.identity].fields)
    assert not missing, f"No «?» hint for fields {sorted(missing)} in section {view.identity}"


@pytest.mark.parametrize("view", VIEWS, ids=_view_id)
def test_hints_name_real_model_fields(view: type[ModelView]) -> None:
    for name in HELP[view.identity].fields:
        assert hasattr(view.model, name), f"{view.identity}: the model has no field {name}"


def test_hints_are_filled() -> None:
    for topic in TOPICS:
        assert topic.summary.strip()
        for name, text in topic.fields.items():
            assert text.strip(), f"{topic.identity}.{name}: empty hint"


def test_every_outcome_is_explained() -> None:
    assert set(OUTCOME_HELP) == set(AuditOutcome)


def test_every_gap_reason_is_explained() -> None:
    assert set(GAP_HELP) == set(GapReason)
    assert all(text.strip() for text in GAP_HELP.values())


def test_every_attack_flag_has_example() -> None:
    assert [flag.code for flag in FLAG_HELP] == list(FLAG_LABELS)
    assert all(flag.example for flag in FLAG_HELP)


def test_anchors() -> None:
    topic = HELP["lead"]
    assert topic.anchor() == "help-lead"
    assert topic.anchor("statusy") == "help-lead-statusy"
    assert topic.field_anchor("phone") == "help-lead-field-phone"
    assert topic.template_for("ro") == "help/topics/ro/lead.html"
    assert topic.template == "help/topics/en/lead.html"  # outside a request the language is the default, English


def _topic_id(topic: HelpTopic) -> str:
    return topic.identity


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("topic", TOPICS, ids=_topic_id)
def test_every_topic_has_text_in_every_language(topic: HelpTopic, locale: str) -> None:
    assert (TEMPLATES / topic.template_for(locale)).is_file()


def _references(topic: HelpTopic, locale: str) -> tuple[list[str], list[str]]:
    text = (TEMPLATES / topic.template_for(locale)).read_text()
    return sorted(re.findall(r"topic\.anchor\('([^']*)'\)", text)), sorted(re.findall(r"section_link\('([^']*)'\)", text))


@pytest.mark.parametrize("locale", [locale for locale in LOCALES if locale != "ru"])
@pytest.mark.parametrize("topic", TOPICS, ids=_topic_id)
def test_translation_keeps_anchors_and_links(topic: HelpTopic, locale: str) -> None:
    # Anchors and links between sections are shared by all languages: a translation that drops or renames one
    # breaks the links to it from the «?» hints and other sections
    assert _references(topic, locale) == _references(topic, "ru")


def test_help_topic_lookup() -> None:
    assert help_topic("lead") is HELP["lead"]
    assert help_topic("unknown") is None
    assert help_topic(None) is None
