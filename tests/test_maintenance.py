import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from medllmbot.db.models import AuditEvent, AuditOutcome, BotConfig
from medllmbot.db.repository import Repository
from medllmbot.maintenance import purge_audit_log, purge_periodically
from tests.conftest import CHAT_ID, Sessions, load_audit


def _days_ago(days: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)


async def _add_events(sessions: Sessions, *ages_in_days: int) -> None:
    async with sessions.begin() as s:
        for age in ages_in_days:
            event = AuditEvent.incoming(CHAT_ID, f"{age} дней назад", AuditOutcome.ANSWERED)
            event.created_at = _days_ago(age)
            s.add(event)


async def test_purge_uses_retention_from_config(seeded_repo: Repository, sessions: Sessions) -> None:
    await seeded_repo.get_config()
    async with sessions.begin() as s:
        await s.execute(update(BotConfig).values(audit_retention_days=30))
    await _add_events(sessions, 0, 29, 31, 400)

    assert await purge_audit_log(seeded_repo) == 2

    assert [e.user_text for e in await load_audit(sessions)] == ["0 дней назад", "29 дней назад"]


async def test_purge_with_nothing_to_delete(seeded_repo: Repository, sessions: Sessions) -> None:
    await _add_events(sessions, 1)
    assert await purge_audit_log(seeded_repo) == 0
    assert len(await load_audit(sessions)) == 1


async def test_periodic_purge_runs_until_cancelled(seeded_repo: Repository, sessions: Sessions) -> None:
    await _add_events(sessions, 100)  # срок по умолчанию — 90 дней

    task = asyncio.create_task(purge_periodically(seeded_repo, every=3600))
    await asyncio.sleep(0.1)  # первая очистка — сразу при старте
    task.cancel()

    assert await load_audit(sessions) == []
