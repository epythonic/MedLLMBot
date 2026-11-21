"""Фоновое обслуживание: удаление записей журнала и пересылок оператору старше срока хранения."""

import asyncio
import logging

from medllmbot.db.repository import Repository

log = logging.getLogger(__name__)

PURGE_EVERY = 6 * 60 * 60  # секунд


async def purge_audit_log(repo: Repository) -> int:
    """Одна очистка по сроку из настроек бота. Возвращает число удалённых записей."""
    config = await repo.get_config()
    deleted = await repo.purge_audit(config.audit_retention_days)
    if deleted:
        log.info("Журнал: удалено %s записей старше %s дней", deleted, config.audit_retention_days)
    # Связи пересылок оператору указывают на клиентов — храним их не дольше журнала
    await repo.purge_relays(config.audit_retention_days)
    return deleted


async def purge_periodically(repo: Repository, every: float = PURGE_EVERY) -> None:
    """Чистит журнал при старте и затем каждые `every` секунд, пока задачу не отменят."""
    while True:
        try:
            await purge_audit_log(repo)
        except Exception:
            # Сбой очистки не должен останавливать бота — попробуем в следующий раз
            log.exception("Не удалось очистить журнал аудита")
        await asyncio.sleep(every)
