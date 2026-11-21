"""Поиск по каталогу услуг в репозитории: полнотекстовый индекс, цены, порог каталога в промпте."""

import pytest
from sqlalchemy import delete, text, update

from medllmbot.db.models import Service
from medllmbot.db.repository import Repository
from medllmbot.db.seed import DEMO_SERVICES
from tests.conftest import Sessions


async def _names(repo: Repository, query: str, **kwargs: int) -> list[str]:
    return [service.name for service in (await repo.search_catalog(query, **kwargs)).services]


class TestSearch:
    @pytest.mark.parametrize(
        ("query", "first"),
        [
            ("чистку зубов", "Профессиональная чистка зубов (ультразвук + Air Flow)"),
            ("удалить зуб мудрости", "Удаление зуба мудрости"),
            ("имплантация", "Имплант (без коронки)"),
            ("рентген", "Прицельный рентгеновский снимок"),
            ("air flow", "Профессиональная чистка зубов (ультразвук + Air Flow)"),
        ],
    )
    async def test_finds_other_word_forms(self, seeded_repo: Repository, query: str, first: str) -> None:
        assert (await _names(seeded_repo, query))[0] == first

    async def test_best_match_first(self, seeded_repo: Repository) -> None:
        # Оба слова совпали только у «Удаление зуба …» — они выше, чем услуги, где есть лишь «зуб»
        names = await _names(seeded_repo, "удаление зуба")
        assert set(names[:2]) == {"Удаление зуба простое", "Удаление зуба мудрости"}
        assert "Отбеливание зубов ZOOM" in names[2:]

    async def test_searches_description(self, seeded_repo: Repository) -> None:
        assert await _names(seeded_repo, "бесплатна") == ["Консультация стоматолога"]

    async def test_nothing_found(self, seeded_repo: Repository) -> None:
        page = await seeded_repo.search_catalog("ноутбук")
        assert (page.services, page.total) == ([], 0)

    async def test_empty_query_lists_catalog_in_admin_order(self, seeded_repo: Repository) -> None:
        page = await seeded_repo.search_catalog("", limit=3)
        assert [s.name for s in page.services] == [name for name, _, _ in DEMO_SERVICES[:3]]
        assert page.total == len(DEMO_SERVICES)

    async def test_limit_keeps_total(self, seeded_repo: Repository) -> None:
        page = await seeded_repo.search_catalog("зуб", limit=2)
        assert len(page.services) == 2
        assert page.total == 4

    async def test_price_range(self, seeded_repo: Repository) -> None:
        cheap = (await seeded_repo.search_catalog("", max_price=200)).services
        assert sorted(s.price for s in cheap) == [100, 150, 200]
        assert await _names(seeded_repo, "коронка", min_price=3000) == ["Циркониевая коронка", "Имплант (без коронки)"]
        assert await _names(seeded_repo, "коронка", min_price=3000, max_price=6000) == ["Циркониевая коронка"]

    async def test_inactive_services_are_hidden(self, seeded_repo: Repository, sessions: Sessions) -> None:
        async with sessions.begin() as s:
            await s.execute(update(Service).where(Service.name == "Циркониевая коронка").values(is_active=False))
        assert "Циркониевая коронка" not in await _names(seeded_repo, "коронка")
        assert (await seeded_repo.search_catalog("")).total == len(DEMO_SERVICES) - 1

    async def test_admin_edits_reach_index(self, seeded_repo: Repository, sessions: Sessions) -> None:
        # Триггеры обновляют индекс при любой правке таблицы — в том числе из админки
        async with sessions.begin() as s:
            s.add(Service(name="Брекеты керамические", price=15000))
            await s.execute(
                update(Service).where(Service.name == "Отбеливание зубов ZOOM").values(name="Отбеливание Opalescence")
            )
            await s.execute(delete(Service).where(Service.name == "Имплант (без коронки)"))

        assert await _names(seeded_repo, "брекеты") == ["Брекеты керамические"]
        assert await _names(seeded_repo, "opalescence") == ["Отбеливание Opalescence"]
        assert "Отбеливание Opalescence" not in await _names(seeded_repo, "zoom")
        assert await _names(seeded_repo, "имплант") == []

    async def test_yo_and_ye_are_the_same(self, seeded_repo: Repository, sessions: Sessions) -> None:
        # Клиенты и модель пишут то «съемный», то «съёмный» — найтись должно в обоих случаях
        async with sessions.begin() as s:
            s.add(Service(name="Съёмный протез", price=7000, description="Ещё и ночная капа"))
        for query in ("съемный протез", "съёмный", "СЪЕМНЫЙ", "еще капа", "ещё"):
            assert await _names(seeded_repo, query) == ["Съёмный протез"], query

    async def test_name_match_ranks_above_description(self, seeded_repo: Repository, sessions: Sessions) -> None:
        async with sessions.begin() as s:
            s.add(Service(name="Гигиена полости рта", price=600, description="Чистка, полировка, фторирование"))
        names = await _names(seeded_repo, "чистка")
        assert names[0] == "Профессиональная чистка зубов (ультразвук + Air Flow)"
        assert "Гигиена полости рта" in names

    async def test_index_stays_consistent(self, seeded_repo: Repository, sessions: Sessions) -> None:
        # Описание NULL → текст → NULL, правка только цены, INSERT OR REPLACE: индекс совпадает с таблицей
        async with sessions.begin() as s:
            service = Service(name="Капа ночная", price=900)
            s.add(service)
        async with sessions.begin() as s:
            await s.execute(update(Service).where(Service.id == service.id).values(description="Защита от бруксизма"))
        assert await _names(seeded_repo, "бруксизм") == ["Капа ночная"]
        async with sessions.begin() as s:
            await s.execute(update(Service).where(Service.id == service.id).values(description=None, price=950))
            await s.execute(
                text(
                    "INSERT OR REPLACE INTO services (id, name, price, is_active, sort_order) VALUES (:id, 'Капа спортивная', 1200, 1, 0)"
                ),
                {"id": service.id},
            )
        assert await _names(seeded_repo, "бруксизм") == []
        assert await _names(seeded_repo, "капа") == ["Капа спортивная"]
        async with sessions.begin() as s:
            # Встроенная проверка FTS5: при расхождении индекса с таблицей — ошибка «database disk image is malformed»
            await s.execute(text("INSERT INTO services_fts(services_fts, rank) VALUES ('integrity-check', 1)"))

    async def test_fts_syntax_from_model_is_harmless(self, seeded_repo: Repository) -> None:
        for query in ("NEAR(чистка зуб) OR *", 'name: "x" AND -y', "' OR 1=1 --"):
            page = await seeded_repo.search_catalog(query)
            assert page.total >= 0


async def test_catalog_prices(seeded_repo: Repository, sessions: Sessions) -> None:
    assert await seeded_repo.catalog_prices() == {price for _, price, _ in DEMO_SERVICES}
    async with sessions.begin() as s:
        await s.execute(update(Service).where(Service.price == 9500).values(is_active=False))
    assert 9500 not in await seeded_repo.catalog_prices()


class TestCatalogInlineLimit:
    async def test_small_catalog_goes_to_prompt(self, seeded_repo: Repository) -> None:
        knowledge = await seeded_repo.load_knowledge(catalog_inline_limit=len(DEMO_SERVICES))
        assert not knowledge.catalog_search
        assert len(knowledge.services) == len(DEMO_SERVICES)

    async def test_large_catalog_is_searched(self, seeded_repo: Repository) -> None:
        knowledge = await seeded_repo.load_knowledge(catalog_inline_limit=len(DEMO_SERVICES) - 1)
        assert knowledge.catalog_search
        assert knowledge.services == []
        assert knowledge.sections and knowledge.faq  # остальная база знаний — по-прежнему в промпте

    async def test_zero_means_always_search(self, seeded_repo: Repository) -> None:
        assert (await seeded_repo.load_knowledge(catalog_inline_limit=0)).catalog_search

    async def test_inactive_services_do_not_count(self, seeded_repo: Repository, sessions: Sessions) -> None:
        async with sessions.begin() as s:
            await s.execute(update(Service).where(Service.price > 1000).values(is_active=False))
        knowledge = await seeded_repo.load_knowledge(catalog_inline_limit=8)
        assert not knowledge.catalog_search

    async def test_no_limit_by_default(self, seeded_repo: Repository) -> None:
        knowledge = await seeded_repo.load_knowledge()
        assert not knowledge.catalog_search
        assert len(knowledge.services) == len(DEMO_SERVICES)
