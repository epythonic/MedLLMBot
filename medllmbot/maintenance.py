"""Background maintenance: deletes audit log entries and operator relays older than the retention period."""

import asyncio
import logging

from medllmbot.db.repository import Repository

log = logging.getLogger(__name__)

PURGE_EVERY = 6 * 60 * 60  # seconds


async def purge_audit_log(repo: Repository) -> int:
    """A single purge by the retention period from the bot settings. Returns the number of deleted entries."""
    config = await repo.get_config()
    deleted = await repo.purge_audit(config.audit_retention_days)
    if deleted:
        log.info("Audit log: deleted %s entries older than %s days", deleted, config.audit_retention_days)
    # Operator relay links point to clients, so keep them no longer than the audit log
    await repo.purge_relays(config.audit_retention_days)
    return deleted


async def purge_periodically(repo: Repository, every: float = PURGE_EVERY) -> None:
    """Purges the audit log at startup and then every `every` seconds until the task is cancelled."""
    while True:
        try:
            await purge_audit_log(repo)
        except Exception:
            # A failed purge must not stop the bot; we will try again next time
            log.exception("Failed to purge the audit log")
        await asyncio.sleep(every)
