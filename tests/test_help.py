"""Admin help is checked against the code: new sections, columns and totals do not go unexplained."""

from collections.abc import Iterable

import pytest
from sqladmin import ModelView

from medllmbot.admin.help import FLAG_HELP, GAP_HELP, HELP, OUTCOME_HELP, TOPICS, help_topic
from medllmbot.admin.views import VIEWS
from medllmbot.analytics import GapReason
from medllmbot.db.models import AuditOutcome
from medllmbot.security import FLAG_LABELS

# Custom admin pages without a model: «Расходы → Отчёт по месяцам» and «Клиенты → Аналитика»
CUSTOM_PAGES = {"costs", "analytics"}


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
    assert topic.template == "help/topics/lead.html"


def test_help_topic_lookup() -> None:
    assert help_topic("lead") is HELP["lead"]
    assert help_topic("unknown") is None
    assert help_topic(None) is None
