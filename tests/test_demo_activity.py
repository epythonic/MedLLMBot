"""Fictional demo activity: clients, chats, leads and log entries that look as if the bot wrote them."""

import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from medllmbot.analytics import CONTACTS, OTHER, GapReason, collect
from medllmbot.db.demo_activity import DEMO_CHAT_ID_BASE, DEMO_CHATS, REMEMBERED
from medllmbot.db.models import (
    AuditEvent,
    AuditOutcome,
    BusinessProfile,
    ChatMessage,
    Client,
    Lead,
    LeadStatus,
    Order,
    UsageDaily,
)
from medllmbot.db.repository import Repository
from medllmbot.db.seed import reset_demo, seed_activity
from medllmbot.guards import PriceChecker, extract_phone
from medllmbot.orders import STATUS_TEXT
from medllmbot.profiles import PROFILES
from medllmbot.prompts import render_info
from medllmbot.text import Language
from tests.conftest import Sessions

NOW = datetime(2026, 11, 27, 12, 0)  # naive UTC, as the DB stores it
PROFILE_IDS = [kind.name.lower() for kind in BusinessProfile]
SEARCH = re.compile(r"search_catalog «(?P<query>.+?)»(?:, (?P<low>\d+)–(?P<high>\d+) лей)? → найдено (?P<found>\d+)$")
ORDER = re.compile(r"order_status (?P<number>\S+) → (?P<status>.+)$")


async def demo(sessions: Sessions, kind: BusinessProfile) -> None:
    """The demo right after `seed --reset --profile …`: knowledge base, settings and the fictional activity."""
    await reset_demo(sessions, kind)
    assert await seed_activity(sessions, NOW)


async def load[M: (Lead, AuditEvent, ChatMessage)](sessions: Sessions, model: type[M]) -> list[M]:
    """Records in time order."""
    async with sessions() as session:
        return list(await session.scalars(select(model).order_by(model.created_at, model.id)))


async def load_clients(sessions: Sessions) -> list[Client]:
    async with sessions() as session:
        return list(await session.scalars(select(Client).order_by(Client.chat_id)))


@pytest.fixture(params=list(BusinessProfile), ids=PROFILE_IDS)
async def kind(request: pytest.FixtureRequest, sessions: Sessions) -> BusinessProfile:
    profile: BusinessProfile = request.param
    await demo(sessions, profile)
    return profile


class TestSeeding:
    async def test_added_once(self, kind: BusinessProfile, sessions: Sessions) -> None:
        clients = len(await load_clients(sessions))

        assert not await seed_activity(sessions, NOW)  # re-running `seed` is safe

        assert len(await load_clients(sessions)) == clients == len(DEMO_CHATS[kind])

    async def test_never_added_next_to_real_clients(self, repo: Repository, sessions: Sessions) -> None:
        await reset_demo(sessions)
        await repo.touch_client(42, "ivan", "Иван Петров")

        assert not await seed_activity(sessions, NOW)

        assert [client.chat_id for client in await load_clients(sessions)] == [42]

    async def test_follows_the_profile(self, kind: BusinessProfile, sessions: Sessions) -> None:
        names = {client.full_name for client in await load_clients(sessions)}
        assert names == {chat.full_name for chat in DEMO_CHATS[kind]}

    @pytest.mark.usefixtures("kind")
    async def test_no_made_up_spending(self, sessions: Sessions) -> None:
        # Spending stays real: a reset would otherwise add the same made-up costs again and again
        assert all(event.model is None and event.cost_micro_usd is None for event in await load(sessions, AuditEvent))
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(UsageDaily)) == 0


class TestLooksReal:
    @pytest.mark.usefixtures("kind")
    async def test_dates_are_recent_and_in_order(self, sessions: Sessions) -> None:
        events = await load(sessions, AuditEvent)
        # Within a week: the shortest «Analytics» period shows the whole demo
        assert all(NOW - timedelta(days=7) < event.created_at < NOW for event in events)
        # IDs follow time, as when the bot writes the log: «Event #12» came after «Event #11»
        assert [event.id for event in events] == sorted(event.id for event in events)
        leads = await load(sessions, Lead)
        assert [lead.id for lead in leads] == sorted(lead.id for lead in leads)
        for client in await load_clients(sessions):
            times = [event.created_at for event in events if event.chat_id == client.chat_id]
            assert (client.first_seen_at, client.last_seen_at) == (min(times), max(times))

    @pytest.mark.usefixtures("kind")
    async def test_nobody_real_can_be_reached(self, sessions: Sessions) -> None:
        # Chat IDs are beyond real Telegram users, phones follow the demo pattern +373 69 000 xxx
        assert all(client.chat_id > DEMO_CHAT_ID_BASE for client in await load_clients(sessions))
        for lead in await load(sessions, Lead):
            digits = re.sub(r"\D", "", lead.phone)
            assert digits.startswith(("37369000", "069000")), lead.phone

    async def test_every_lead_comes_from_the_message_with_its_phone(self, kind: BusinessProfile, sessions: Sessions) -> None:
        events = await load(sessions, AuditEvent)
        leads = await load(sessions, Lead)
        assert len(leads) == sum(1 for chat in DEMO_CHATS[kind] if chat.lead is not None)
        for lead in leads:
            source = [e for e in events if e.chat_id == lead.chat_id and e.created_at == lead.created_at]
            assert [extract_phone(e.user_text) for e in source] == [lead.phone]
            assert source[0].user_text == lead.message

    async def test_lead_statuses_show_the_whole_flow(self, sessions: Sessions) -> None:
        await demo(sessions, BusinessProfile.CLINIC)
        assert {lead.status for lead in await load(sessions, Lead)} == set(LeadStatus)

    @pytest.mark.usefixtures("kind")
    async def test_history_keeps_only_what_the_bot_remembers(self, sessions: Sessions) -> None:
        events = await load(sessions, AuditEvent)
        remembered = [e for e in events if e.outcome in REMEMBERED and not e.is_suspicious]
        history = await load(sessions, ChatMessage)
        assert [(m.role, m.content) for m in history] == [
            pair for e in remembered for pair in (("user", e.user_text), ("assistant", e.final_answer))
        ]
        # Attack attempts stay out of the model context (CLAUDE.md: «подозрительные сообщения … в неё не попадают»)
        suspicious = {e.user_text for e in events if e.is_suspicious}
        assert suspicious
        assert not suspicious & {m.content for m in history}

    async def test_attack_signs_come_from_the_detector(self, sessions: Sessions) -> None:
        await demo(sessions, BusinessProfile.CLINIC)
        events = await load(sessions, AuditEvent)
        leak = next(e for e in events if e.outcome is AuditOutcome.LEAK_BLOCKED)
        assert "prompt_leak" in leak.flags.split(",")
        injection = next(e for e in events if e.user_text.startswith("Игнорируй"))
        assert "ignore_instructions" in injection.flags.split(",")
        spammer = next(c for c in await load_clients(sessions) if c.is_blocked)
        blocked = [e for e in events if e.outcome is AuditOutcome.BLOCKED]
        assert [e.chat_id for e in blocked] == [spammer.chat_id]
        assert blocked[0].final_answer is None  # the bot does not answer a blocked client

    async def test_canned_replies_are_the_bots_own(self, kind: BusinessProfile, sessions: Sessions) -> None:
        events = await load(sessions, AuditEvent)
        language = {DEMO_CHAT_ID_BASE + i: chat.language for i, chat in enumerate(DEMO_CHATS[kind], start=1)}
        for event in events:
            replies = PROFILES[kind].replies_in(language[event.chat_id])
            expected = {
                AuditOutcome.PRICE_REPLACED: replies.price_fallback,
                AuditOutcome.HANDOFF: replies.operator_started,
                AuditOutcome.LEAK_BLOCKED: replies.fallback,
                AuditOutcome.FROM_OPERATOR: event.user_text,
                AuditOutcome.TO_OPERATOR: None,
            }
            if event.outcome in expected:
                assert event.final_answer == expected[event.outcome], event.user_text

    async def test_greeting_is_the_one_in_the_settings(self, repo: Repository, sessions: Sessions) -> None:
        await demo(sessions, BusinessProfile.CLINIC)
        greetings = [e.final_answer for e in await load(sessions, AuditEvent) if e.outcome is AuditOutcome.GREETING]
        assert greetings == [(await repo.get_config()).greeting]

    async def test_answers_pass_the_bots_price_check(
        self, kind: BusinessProfile, repo: Repository, sessions: Sessions
    ) -> None:
        # A price the bot's own check would reject would have reached the client as «Уточню стоимость…»
        config = await repo.get_config()
        knowledge = await repo.load_knowledge(catalog_inline_limit=config.catalog_inline_limit)
        prices = PriceChecker(render_info(knowledge, kind))
        if knowledge.catalog_search:
            prices.add_known(await repo.catalog_prices())
        events = await load(sessions, AuditEvent)
        for event in events:
            if event.outcome is AuditOutcome.ANSWERED:
                said = [e.user_text for e in events if e.chat_id == event.chat_id and e.created_at <= event.created_at]
                assert not prices.has_unknown_price(event.final_answer or "", said), event.final_answer

    async def test_logged_searches_match_the_catalog(self, repo: Repository, sessions: Sessions) -> None:
        await demo(sessions, BusinessProfile.SHOP)
        lines = [line for e in await load(sessions, AuditEvent) for line in (e.tool_calls or "").splitlines()]
        searches = [match for line in lines if (match := SEARCH.match(line))]
        assert searches
        for search in searches:
            low, high = search["low"], search["high"]
            page = await repo.search_catalog(
                search["query"], min_price=int(low) if low else None, max_price=int(high) if high else None
            )
            assert page.total == int(search["found"]), search.string

    async def test_logged_order_checks_match_the_orders(self, sessions: Sessions) -> None:
        await demo(sessions, BusinessProfile.SHOP)
        async with sessions() as session:
            status = {order.number: STATUS_TEXT[order.status] for order in await session.scalars(select(Order))}
        lines = [line for e in await load(sessions, AuditEvent) for line in (e.tool_calls or "").splitlines()]
        checks = [match for line in lines if (match := ORDER.match(line))]
        assert checks
        assert all(status[check["number"]] == check["status"] for check in checks)

    def test_chats_in_romanian_are_marked(self) -> None:
        # Canned replies follow the chat language: a Romanian chat with a Russian «Уточню…» would look broken
        for chats in DEMO_CHATS.values():
            for chat in chats:
                romanian = any(re.search(r"[ăâîșț]", turn.text) for turn in chat.turns)
                assert (chat.language is Language.RO) == romanian, chat.full_name


class TestAnalytics:
    """The demo fills every block of «Analytics» from the first minute."""

    @pytest.mark.parametrize(
        ("profile", "gaps"),
        [
            (BusinessProfile.CLINIC, {GapReason.NO_INFO, GapReason.UNKNOWN_PRICE}),
            (BusinessProfile.SHOP, {GapReason.NOT_FOUND, GapReason.UNKNOWN_PRICE}),
        ],
        ids=PROFILE_IDS,
    )
    async def test_topics_leads_and_gaps(
        self, profile: BusinessProfile, gaps: set[GapReason], repo: Repository, sessions: Sessions
    ) -> None:
        await demo(sessions, profile)

        report = await collect(repo, profile, 7, NOW)

        topics = [topic.name for topic in report.topics if topic.name not in (OTHER, CONTACTS)]
        assert len(topics) >= 5
        assert report.dialogs_with_lead > 0
        assert report.handoffs == 1
        assert {gap.reason for gap in report.gaps} == gaps
