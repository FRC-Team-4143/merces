"""
APScheduler jobs:
  1. Legion roster sync — hourly incremental pull.
  2. Weekly SQLite backup.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import AsyncSessionLocal

log = logging.getLogger(__name__)


async def job_nightly_backup() -> None:
    from app.services.backup import is_sqlite, nightly_backup
    if not is_sqlite():
        return
    try:
        nightly_backup()
    except Exception:  # never let a backup failure crash the scheduler
        log.exception("Backup failed")


async def job_legion_sync() -> None:
    """Pull the roster from Legion. No-op (with a log line) when Legion isn't configured."""
    if not settings.updates_enabled:
        log.info("Legion sync skipped (updates_enabled=false)")
        return
    if not settings.legion_base_url or not settings.legion_api_key:
        log.info("Legion sync skipped (LEGION_BASE_URL/LEGION_API_KEY not set)")
        return
    from app.services.legion_sync import sync_roster
    try:
        async with AsyncSessionLocal() as db:
            summary = await sync_roster(db)
        log.info("Scheduled Legion sync: %s", summary)
    except Exception:  # never let a sync failure crash the scheduler
        log.exception("Scheduled Legion sync failed")


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    """(Re)register all scheduled jobs from the current settings. Uses
    ``replace_existing=True`` so it is safe to call on a running scheduler."""
    bh, bm = settings.backup_time.split(":")
    scheduler.add_job(
        job_nightly_backup,
        CronTrigger(day_of_week=settings.backup_day, hour=int(bh), minute=int(bm), timezone=settings.timezone),
        id="nightly_backup",
        replace_existing=True,
    )

    scheduler.add_job(
        job_legion_sync,
        CronTrigger(minute=0, timezone=settings.timezone),
        id="legion_sync",
        replace_existing=True,
    )


def reschedule_all(scheduler) -> None:
    if scheduler is None:
        return
    register_jobs(scheduler)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    register_jobs(scheduler)
    return scheduler
