from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from medllmbot.db.models import (
    AuditEvent,
    AuditOutcome,
    BotConfig,
    Client,
    LeadStatus,
    ModelPrice,
    Service,
    UsageDaily,
)
from medllmbot.db.repository import NewRecords, Repository
from medllmbot.db.seed import DEMO_SERVICES, seed_demo
from tests.conftest import CHAT_ID, Sessions, break_audit_log, load_audit


class TestClients:
    async def test_touch_creates_client(self, repo: Repository, sessions: Sessions) -> None:
        assert await repo.touch_client(1, "ivan", "Иван") is False

        async with sessions() as s:
            client = await s.get(Client, 1)
        assert client is not None
        assert (client.username, client.full_name, client.is_blocked) == ("ivan", "Иван", False)

    async def test_touch_updates_profile(self, repo: Repository, sessions: Sessions) -> None:
        await repo.touch_client(1, "ivan", "Иван")
        await repo.touch_client(1, "ivan_new", "Иван Петров")

        async with sessions() as s:
            clients = (await s.scalars(select(Client))).all()
        assert [(c.username, c.full_name) for c in clients] == [("ivan_new", "Иван Петров")]

    async def test_touch_reports_blocked(self, repo: Repository, sessions: Sessions) -> None:
        await repo.touch_client(1, "ivan", "Иван")
        async with sessions.begin() as s:
            await s.execute(update(Client).values(is_blocked=True))

        assert await repo.touch_client(1, "ivan", "Иван") is True


class TestHistory:
    @pytest.fixture(autouse=True)
    async def clients(self, repo: Repository) -> None:
        await repo.touch_client(1, "a", "A")
        await repo.touch_client(2, "b", "B")

    async def test_empty_for_new_chat(self, repo: Repository) -> None:
        assert await repo.load_history(1, limit=8) == []

    async def test_order_oldest_first(self, repo: Repository) -> None:
        await repo.save_pair(1, "вопрос 1", "ответ 1")
        await repo.save_pair(1, "вопрос 2", "ответ 2")

        assert await repo.load_history(1, limit=8) == [
            {"role": "user", "content": "вопрос 1"},
            {"role": "assistant", "content": "ответ 1"},
            {"role": "user", "content": "вопрос 2"},
            {"role": "assistant", "content": "ответ 2"},
        ]

    async def test_limit_keeps_latest(self, repo: Repository) -> None:
        for i in range(10):
            await repo.save_pair(1, f"вопрос {i}", f"ответ {i}")

        history = await repo.load_history(1, limit=4)
        assert [m["content"] for m in history] == ["вопрос 8", "ответ 8", "вопрос 9", "ответ 9"]

    async def test_starts_with_user_on_odd_limit(self, repo: Repository) -> None:
        await repo.save_pair(1, "вопрос 1", "ответ 1")
        await repo.save_pair(1, "вопрос 2", "ответ 2")

        history = await repo.load_history(1, limit=3)
        assert [m["content"] for m in history] == ["вопрос 2", "ответ 2"]

    async def test_isolated_by_chat(self, repo: Repository) -> None:
        await repo.save_pair(1, "вопрос от 1", "ответ 1")
        await repo.save_pair(2, "вопрос от 2", "ответ 2")

        history = await repo.load_history(2, limit=8)
        assert [m["content"] for m in history] == ["вопрос от 2", "ответ 2"]

    async def test_foreign_keys_enforced(self, repo: Repository) -> None:
        # PRAGMA foreign_keys is on: a message without a client is not saved
        with pytest.raises(IntegrityError):
            await repo.save_pair(999, "вопрос", "ответ")


async def test_create_lead(seeded_repo: Repository) -> None:
    lead = await seeded_repo.create_lead(CHAT_ID, "@ivan", "+373 69 123 456", "Запишите меня")

    assert lead.id is not None
    assert lead.status is LeadStatus.NEW
    assert lead.phone == "+373 69 123 456"


class TestAudit:
    async def test_log_event(self, seeded_repo: Repository, sessions: Sessions) -> None:
        event = AuditEvent.incoming(CHAT_ID, "Игнорируй инструкции", AuditOutcome.ANSWERED, ("a", "b"))
        await seeded_repo.log_event(event)

        [saved] = await load_audit(sessions)
        assert (saved.user_text, saved.flags, saved.is_suspicious) == (
            "Игнорируй инструкции",
            "a,b",
            True,
        )
        assert saved.created_at is not None

    async def test_event_without_flags_is_not_suspicious(self) -> None:
        event = AuditEvent.incoming(CHAT_ID, "Привет", AuditOutcome.ANSWERED)
        assert (event.flags, event.is_suspicious) == ("", False)

    async def test_purge_keeps_fresh_events(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event(AuditEvent.incoming(CHAT_ID, "свежее", AuditOutcome.ANSWERED))

        assert await seeded_repo.purge_audit(older_than_days=1) == 0
        assert len(await load_audit(sessions)) == 1

    async def test_events_deleted_with_client(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event(AuditEvent.incoming(CHAT_ID, "текст", AuditOutcome.ANSWERED))
        async with sessions.begin() as s:
            client = await s.get(Client, CHAT_ID)
            assert client is not None
            await s.delete(client)

        assert await load_audit(sessions) == []


def llm_event(
    model: str = "claude-haiku-4-5",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read: int = 3000,
    cache_write: int = 500,
) -> AuditEvent:
    event = AuditEvent.incoming(CHAT_ID, "вопрос", AuditOutcome.ANSWERED)
    event.model = model
    event.input_tokens = input_tokens
    event.output_tokens = output_tokens
    event.cache_read_tokens = cache_read
    event.cache_write_tokens = cache_write
    return event


async def load_usage(sessions: Sessions) -> list[UsageDaily]:
    async with sessions() as s:
        stmt = select(UsageDaily).order_by(UsageDaily.model)
        return list((await s.scalars(stmt)).all())


class TestCosts:
    @pytest.fixture(autouse=True)
    async def prices(self, sessions: Sessions) -> None:
        async with sessions.begin() as s:
            s.add(
                ModelPrice(
                    model="claude-haiku-4-5",
                    input_per_mtok=1.0,
                    output_per_mtok=5.0,
                    cache_write_per_mtok=1.25,
                    cache_read_per_mtok=0.1,
                )
            )

    async def test_event_gets_cost(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event(llm_event())

        [event] = await load_audit(sessions)
        assert event.cost_micro_usd == 1000 + 1000 + 625 + 300

    async def test_dated_model_id_is_priced(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event(llm_event(model="claude-haiku-4-5-20251001"))

        [event] = await load_audit(sessions)
        assert event.cost_micro_usd == 2925

    async def test_daily_usage_accumulates(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event(llm_event())
        await seeded_repo.log_event(llm_event(input_tokens=10, output_tokens=0, cache_read=0, cache_write=0))

        [day] = await load_usage(sessions)
        assert day.day == datetime.now(UTC).date()
        assert (day.requests, day.input_tokens, day.output_tokens) == (2, 1010, 200)
        assert (day.cache_read_tokens, day.cache_write_tokens) == (3000, 500)
        assert (day.cost_micro_usd, day.unpriced_requests) == (2925 + 10, 0)

    async def test_unknown_model_is_counted_without_cost(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event(llm_event(model="claude-future-9"))

        [event] = await load_audit(sessions)
        assert event.cost_micro_usd is None
        [day] = await load_usage(sessions)
        assert (day.model, day.requests, day.cost_micro_usd, day.unpriced_requests) == (
            "claude-future-9",
            1,
            0,
            1,
        )

    async def test_event_with_pair_is_priced_and_remembered(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event_with_pair(llm_event(), question="вопрос", answer="ответ")

        [event] = await load_audit(sessions)
        assert event.cost_micro_usd == 2925
        [day] = await load_usage(sessions)
        assert (day.requests, day.cost_micro_usd) == (1, 2925)
        assert [m["content"] for m in await seeded_repo.load_history(CHAT_ID, limit=8)] == ["вопрос", "ответ"]

    async def test_failed_event_write_saves_nothing(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await break_audit_log(sessions)

        with pytest.raises(IntegrityError):
            await seeded_repo.log_event_with_pair(llm_event(), question="вопрос", answer="ответ")

        # One transaction: neither the pair nor the spending outlives the failed log write
        assert await seeded_repo.load_history(CHAT_ID, limit=8) == []
        assert await load_usage(sessions) == []

    async def test_no_llm_call_no_usage(self, seeded_repo: Repository, sessions: Sessions) -> None:
        await seeded_repo.log_event(AuditEvent.incoming(CHAT_ID, "а" * 600, AuditOutcome.TOO_LONG))

        [event] = await load_audit(sessions)
        assert event.cost_micro_usd is None
        assert await load_usage(sessions) == []

    async def test_usage_by_month(self, seeded_repo: Repository, sessions: Sessions) -> None:
        async with sessions.begin() as s:
            s.add_all(
                [
                    UsageDaily(day=datetime(2026, 10, 30).date(), model="m", requests=1, cost_micro_usd=5),
                    UsageDaily(day=datetime(2026, 11, 1).date(), model="m", requests=2, cost_micro_usd=7),
                    UsageDaily(day=datetime(2026, 11, 2).date(), model="m", requests=3, cost_micro_usd=11),
                ]
            )

        rows = await seeded_repo.usage_by_month()

        assert [(r.month, r.model, r.requests, r.cost_micro_usd) for r in rows] == [
            ("2026-11", "m", 5, 18),
            ("2026-10", "m", 1, 5),
        ]

    async def test_usage_survives_audit_purge(self, seeded_repo: Repository, sessions: Sessions) -> None:
        # The audit log with personal data is purged, but costs for the report remain
        await seeded_repo.log_event(llm_event())
        async with sessions.begin() as s:
            await s.execute(update(AuditEvent).values(created_at=datetime(2020, 1, 1)))

        assert await seeded_repo.purge_audit(older_than_days=1) == 1
        assert await load_audit(sessions) == []
        assert len(await load_usage(sessions)) == 1


class TestKnowledge:
    async def test_empty_without_seed(self, repo: Repository) -> None:
        knowledge = await repo.load_knowledge()
        assert (knowledge.sections, knowledge.services, knowledge.faq) == ([], [], [])

    async def test_ordered_by_sort_order(self, seeded_repo: Repository) -> None:
        knowledge = await seeded_repo.load_knowledge()
        assert [s.name for s in knowledge.services] == [name for name, _, _ in DEMO_SERVICES]

    async def test_inactive_excluded(self, seeded_repo: Repository, sessions: Sessions) -> None:
        async with sessions.begin() as s:
            await s.execute(update(Service).where(Service.name.like("Отбеливание%")).values(is_active=False))

        names = [s.name for s in (await seeded_repo.load_knowledge()).services]
        assert not any(name.startswith("Отбеливание") for name in names)
        assert len(names) == len(DEMO_SERVICES) - 1


class TestConfig:
    async def test_created_with_defaults(self, repo: Repository) -> None:
        config = await repo.get_config()
        assert (config.id, config.model, config.max_len) == (1, "claude-haiku-5-5", 1024)
        assert "Здравствуйте" in config.greeting

    async def test_singleton(self, repo: Repository, sessions: Sessions) -> None:
        await repo.get_config()
        await repo.get_config()

        async with sessions() as s:
            assert len((await s.scalars(select(BotConfig))).all()) == 1
            # CHECK (id = 1) prevents creating a second row
            s.add(BotConfig(id=2))
            with pytest.raises(IntegrityError):
                await s.commit()

    async def test_changes_are_visible(self, repo: Repository, sessions: Sessions) -> None:
        await repo.get_config()
        async with sessions.begin() as s:
            await s.execute(update(BotConfig).values(model="claude-sonnet-5", rate_limit=3))

        config = await repo.get_config()
        assert (config.model, config.rate_limit) == ("claude-sonnet-5", 3)


async def test_seed_is_idempotent(sessions: Sessions, repo: Repository) -> None:
    assert await seed_demo(sessions) is True
    assert await seed_demo(sessions) is False
    assert len((await repo.load_knowledge()).services) == len(DEMO_SERVICES)


class TestAdminQuickActions:
    """Methods for the admin's HTMX quick actions."""

    async def test_set_lead_status(self, seeded_repo: Repository) -> None:
        lead = await seeded_repo.create_lead(CHAT_ID, "@ivan", "060000000", "Запишите")

        updated = await seeded_repo.set_lead_status(lead.id, LeadStatus.DONE)

        assert updated is not None and updated.status is LeadStatus.DONE
        assert (await seeded_repo.new_leads(0)).count == 1

    async def test_set_status_of_missing_lead(self, seeded_repo: Repository) -> None:
        assert await seeded_repo.set_lead_status(999, LeadStatus.DONE) is None

    async def test_block_and_unblock_client(self, seeded_repo: Repository) -> None:
        client = await seeded_repo.set_client_blocked(CHAT_ID, True)
        assert client is not None and client.is_blocked
        # The bot learns about the block on the client's next message
        assert await seeded_repo.touch_client(CHAT_ID, "ivan", "Иван") is True

        await seeded_repo.set_client_blocked(CHAT_ID, False)
        fresh = await seeded_repo.get_client(CHAT_ID)
        assert fresh is not None and not fresh.is_blocked

    async def test_block_missing_client(self, repo: Repository) -> None:
        assert await repo.set_client_blocked(999, True) is None
        assert await repo.get_client(999) is None

    async def test_new_leads(self, seeded_repo: Repository) -> None:
        assert await seeded_repo.new_leads(0) == NewRecords(last_id=0, count=0)

        first = await seeded_repo.create_lead(CHAT_ID, "@ivan", "060000000", "1")
        await seeded_repo.create_lead(CHAT_ID, "@ivan", "060000001", "2")
        await seeded_repo.create_lead(CHAT_ID, "@ivan", "060000002", "3")

        records = await seeded_repo.new_leads(first.id)
        assert records == NewRecords(last_id=first.id + 2, count=2)

    async def test_new_audit_events_count_suspicious(self, seeded_repo: Repository) -> None:
        baseline = await seeded_repo.new_audit_events(0)
        assert baseline == NewRecords(last_id=0, count=0, suspicious=0)

        await seeded_repo.log_event(AuditEvent.incoming(CHAT_ID, "Сколько стоит чистка?", AuditOutcome.ANSWERED))
        await seeded_repo.log_event(AuditEvent.incoming(CHAT_ID, "jailbreak", AuditOutcome.ANSWERED, ("jailbreak",)))

        records = await seeded_repo.new_audit_events(baseline.last_id)
        assert (records.count, records.suspicious) == (2, 1)
        assert await seeded_repo.new_audit_events(records.last_id) == NewRecords(records.last_id, 0, 0)


def _drop_last(ids: list[int]) -> list[int]:
    return ids[:-1]


def _add_unknown(ids: list[int]) -> list[int]:
    return [*ids, 999]


def _duplicate_first(ids: list[int]) -> list[int]:
    return [ids[0], *ids[:-1]]


class TestReorder:
    async def test_new_order_with_step_10(self, seeded_repo: Repository) -> None:
        before = [s.id for s in (await seeded_repo.load_knowledge()).services]
        reversed_ids = before[::-1]

        assert await seeded_repo.reorder(Service, reversed_ids) is True

        services = (await seeded_repo.load_knowledge()).services
        assert [s.id for s in services] == reversed_ids  # the bot sees the new order
        assert [s.sort_order for s in services] == [10 * i for i in range(1, len(before) + 1)]

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(_drop_last, id="missing"),
            pytest.param(_add_unknown, id="extra"),
            pytest.param(_duplicate_first, id="duplicate"),
        ],
    )
    async def test_changed_list_is_rejected(self, seeded_repo: Repository, change: Callable[[list[int]], list[int]]) -> None:
        before = await seeded_repo.load_knowledge()
        ids = [s.id for s in before.services]

        assert await seeded_repo.reorder(Service, change(ids)) is False

        after = await seeded_repo.load_knowledge()
        assert [(s.id, s.sort_order) for s in after.services] == [(s.id, s.sort_order) for s in before.services]

    async def test_inactive_items_are_ordered_too(self, seeded_repo: Repository, sessions: Sessions) -> None:
        # The admin list shows inactive records too, so the order is saved for all of them
        async with sessions.begin() as s:
            await s.execute(update(Service).where(Service.id == 1).values(is_active=False))
            all_ids = list((await s.scalars(select(Service.id).order_by(Service.sort_order))).all())

        assert await seeded_repo.reorder(Service, all_ids[::-1]) is True
