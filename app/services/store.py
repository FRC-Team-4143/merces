"""
Rewards store — catalog CRUD helpers. Purchases go through services/wallet.py, which
owns balance/stock guards; this module only reads/writes StoreItem rows.
"""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Redemption, StoreItem


async def active_items(db: AsyncSession) -> list[StoreItem]:
    """Items visible in the student-facing store."""
    return (
        await db.execute(
            select(StoreItem)
            .where(StoreItem.is_active.is_(True))
            .order_by(StoreItem.sort_order, StoreItem.name)
        )
    ).scalars().all()


async def all_items(db: AsyncSession, *, show_archived: bool = True) -> list[StoreItem]:
    """Items for the admin catalog view. `show_archived=False` (the admin Store page's
    default) hides archived items, mirroring Munus's opportunities list — otherwise
    every item ever archived stays visible forever."""
    q = select(StoreItem).order_by(StoreItem.sort_order, StoreItem.name)
    if not show_archived:
        q = q.where(StoreItem.is_active.is_(True))
    return (await db.execute(q)).scalars().all()


async def get_item(db: AsyncSession, item_id: int) -> Optional[StoreItem]:
    return await db.get(StoreItem, item_id)


async def has_redemptions(db: AsyncSession, item_id: int) -> bool:
    """Whether any order (of any status) has ever been placed for this item — governs
    whether it can be hard-deleted (see routers/admin.py:store_delete) versus only
    archived, since Redemption.item_name/cost/size are snapshots that outlive the row."""
    count = (
        await db.execute(
            select(func.count()).select_from(Redemption).where(Redemption.store_item_id == item_id)
        )
    ).scalar_one()
    return count > 0


def parse_sizes(sizes: Optional[str]) -> list[str]:
    """A StoreItem's raw comma-separated `sizes` field into a clean list, e.g.
    "S, M, L" -> ["S", "M", "L"]. Empty/None -> [] (no size selection needed)."""
    if not sizes:
        return []
    return [s.strip() for s in sizes.split(",") if s.strip()]
