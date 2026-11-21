from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlockParam
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from medllmbot.db.engine import create_engine, create_sessionmaker
from medllmbot.db.models import AuditEvent, Base, FaqItem, InfoSection, Service
from medllmbot.db.repository import Knowledge, Repository
from medllmbot.db.seed import DEMO_FAQ, DEMO_SECTIONS, DEMO_SERVICES, seed_demo
from medllmbot.prompts import render_info

CHAT_ID = 42

type Sessions = async_sessionmaker[AsyncSession]


def demo_knowledge() -> Knowledge:
    """Демо-база знаний без БД — для тестов чистых функций."""
    return Knowledge(
        sections=[InfoSection(title=t, content=c) for t, c in DEMO_SECTIONS],
        services=[Service(name=n, price=p, description=d) for n, p, d in DEMO_SERVICES],
        faq=[FaqItem(question=q, answer=a) for q, a in DEMO_FAQ],
    )


DEMO_INFO = render_info(demo_knowledge())


def make_response(
    text: str,
    stop_reason: str = "end_turn",
    *,
    model: str = "claude-haiku-5-5",
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> SimpleNamespace:
    """Минимальный ответ в форме anthropic.types.Message."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model=model,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_tokens,
            cache_creation_input_tokens=cache_write_tokens,
        ),
    )


@dataclass(frozen=True)
class LLMCall:
    """Аргументы одного вызова messages.create."""

    model: str
    max_tokens: int
    system: list[TextBlockParam]
    messages: list[MessageParam]


class FakeMessages:
    """Подменяет client.messages: запоминает запросы, отдаёт заданный ответ или ошибку."""

    calls: list[LLMCall]
    response: SimpleNamespace
    error: Exception | None

    def __init__(self) -> None:
        self.calls = []
        self.response = make_response("Чистка зубов стоит 800 лей.")
        self.error = None

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[TextBlockParam],
        messages: list[MessageParam],
    ) -> SimpleNamespace:
        self.calls.append(LLMCall(model, max_tokens, system, messages))
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    messages: FakeMessages

    def __init__(self) -> None:
        self.messages = FakeMessages()

    def as_anthropic(self) -> AsyncAnthropic:
        """Фейк повторяет только нужную часть интерфейса, поэтому приводим тип явно."""
        return cast(AsyncAnthropic, cast(object, self))


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(tmp_path / "test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> Sessions:
    return create_sessionmaker(engine)


@pytest.fixture
def repo(sessions: Sessions) -> Repository:
    return Repository(sessions)


@pytest.fixture
async def seeded_repo(repo: Repository, sessions: Sessions) -> Repository:
    """Репозиторий с демо-базой знаний и зарегистрированным клиентом CHAT_ID."""
    await seed_demo(sessions)
    await repo.touch_client(CHAT_ID, "ivan", "Иван Петров")
    return repo


async def load_audit(sessions: Sessions) -> list[AuditEvent]:
    """Все записи журнала аудита по порядку."""
    async with sessions() as s:
        return list((await s.scalars(select(AuditEvent).order_by(AuditEvent.id))).all())
