from medllmbot.db.models import FaqItem, InfoSection, Service
from medllmbot.db.repository import Knowledge
from medllmbot.prompts import CATALOG_SEARCH_NOTE, build_system_prompt, render_info, service_line
from tests.conftest import CLINIC_RULES, DEMO_INFO, demo_knowledge


def test_render_info_contains_all_parts() -> None:
    assert "== Контакты ==" in DEMO_INFO
    assert "Профессиональная чистка зубов (ультразвук + Air Flow) — 800 лей (Около 60 минут)" in (DEMO_INFO)
    assert "Вопрос: Есть ли парковка?\nОтвет: Да, бесплатная парковка у входа." in DEMO_INFO


def test_render_info_is_deterministic() -> None:
    # Same data gives a byte-identical prompt, otherwise prompt caching won't work
    assert render_info(demo_knowledge()) == render_info(demo_knowledge())


def test_empty_parts_are_skipped() -> None:
    knowledge = Knowledge(
        sections=[InfoSection(title="Контакты", content="ул. Примерная, 1")],
        services=[],
        faq=[],
    )
    assert render_info(knowledge) == "== Контакты ==\nул. Примерная, 1"


def test_service_without_description() -> None:
    knowledge = Knowledge(sections=[], services=[Service(name="Снимок", price=100)], faq=[])
    assert render_info(knowledge) == "== Услуги и цены ==\nСнимок — 100 лей"


def test_faq_formatting() -> None:
    knowledge = Knowledge(sections=[], services=[], faq=[FaqItem(question="Q? ", answer=" A.")])
    assert render_info(knowledge) == "== Частые вопросы ==\nВопрос: Q?\nОтвет: A."


def test_system_prompt_wraps_info() -> None:
    prompt = build_system_prompt("ИНФО")
    assert prompt.startswith(CLINIC_RULES)
    assert prompt.endswith("<info>\nИНФО\n</info>")


def test_catalog_search_replaces_price_list() -> None:
    knowledge = Knowledge(
        sections=[InfoSection(title="Контакты", content="ул. Примерная, 1")], services=[], faq=[], catalog_search=True
    )
    assert render_info(knowledge) == f"== Контакты ==\nул. Примерная, 1\n\n== Услуги и цены ==\n{CATALOG_SEARCH_NOTE}"


def test_service_line_is_shared_with_search() -> None:
    assert service_line(Service(name="Снимок", price=100)) == "Снимок — 100 лей"
    assert service_line(Service(name="Чистка", price=800, description=" 60 минут ")) == "Чистка — 800 лей (60 минут)"


def test_rules_allow_prices_from_search() -> None:
    assert "search_catalog" in CLINIC_RULES
