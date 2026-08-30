"""
Merces ledger — balances are always derived (SUM of Transaction.delta), never stored, so
they can't drift out of sync with the transaction history (the ledger is the source of
truth, mirroring Munus's approved-hours model).

None of these functions commit — callers commit alongside their own audit.record() call,
matching the transaction-boundary convention used throughout the sibling apps.
"""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Member, MemberKind, Redemption, RedemptionStatus, StoreItem, Transaction, TransactionKind,
)
from app.services.store import parse_sizes
from app.utils import now_utc


class InsufficientBalanceError(RuntimeError):
    """Raised when a purchase would take a member's balance below zero."""


class ItemUnavailableError(RuntimeError):
    """Raised when a purchase targets an archived or out-of-stock item."""


class InvalidSizeError(RuntimeError):
    """Raised when a purchase for an item with configured sizes is missing one, or
    names a size that isn't one of the item's options."""


async def balance_for(db: AsyncSession, member_id: int) -> int:
    total = (
        await db.execute(
            select(func.coalesce(func.sum(Transaction.delta), 0))
            .where(Transaction.member_id == member_id)
        )
    ).scalar_one()
    return int(total)


async def balances_for_all(db: AsyncSession) -> dict[int, int]:
    """Every member's balance in one query, keyed by member_id. A member with no
    transactions yet is simply absent — callers should default missing keys to 0."""
    rows = (
        await db.execute(
            select(Transaction.member_id, func.sum(Transaction.delta))
            .group_by(Transaction.member_id)
        )
    ).all()
    return {member_id: int(total) for member_id, total in rows}


async def grant(
    db: AsyncSession, member: Member, amount: int, *, actor: str, reason: Optional[str] = None
) -> int:
    """Award `amount` (must be positive) points to `member`. Returns their new balance.
    Students only — mentors have no reason to hold a balance in this app (they're not
    the ones redeeming store items), so this is a hard rule enforced here, not just a
    UI-level filter on the Give Cash form."""
    if amount <= 0:
        raise ValueError("Grant amount must be positive.")
    if member.kind != MemberKind.student:
        raise ValueError(f"{member.name} is a mentor — only students can be granted {settings.currency_name}.")
    db.add(Transaction(
        member_id=member.id, delta=amount, kind=TransactionKind.grant,
        actor=actor, reason=reason,
    ))
    await db.flush()
    return await balance_for(db, member.id)


async def adjust(
    db: AsyncSession, member: Member, delta: int, *, actor: str, reason: Optional[str] = None
) -> int:
    """Manual correction of any sign — the only ledger entry allowed to go negative
    unprompted, since it's admin-gated at the router level. Returns the new balance.
    Students only, same rule as grant() — a mentor has no legitimate balance to
    correct."""
    if delta == 0:
        raise ValueError("Adjustment must be non-zero.")
    if member.kind != MemberKind.student:
        raise ValueError(f"{member.name} is a mentor — only students can hold {settings.currency_name}.")
    db.add(Transaction(
        member_id=member.id, delta=delta, kind=TransactionKind.adjustment,
        actor=actor, reason=reason,
    ))
    await db.flush()
    return await balance_for(db, member.id)


async def purchase(
    db: AsyncSession, member: Member, item: StoreItem, *, actor: str = "self",
    size: Optional[str] = None,
) -> Redemption:
    """Instantly redeem `item` for `member`: guard active/stock/balance/size, decrement
    stock, and create the Redemption + a negative Transaction linking to it. Self-serve —
    no approval step; the order starts `ordered` and a staff member fulfills it in
    person."""
    if not item.is_active:
        raise ItemUnavailableError("That item is no longer available.")
    if item.stock is not None and item.stock <= 0:
        raise ItemUnavailableError("That item is out of stock.")
    balance = await balance_for(db, member.id)
    if balance < item.cost:
        raise InsufficientBalanceError(f"Not enough balance ({balance} < {item.cost}).")

    # Only items with configured sizes require one — an unsized item ignores whatever
    # was passed in rather than snapshotting stray form data onto the order.
    available_sizes = parse_sizes(item.sizes)
    if available_sizes and size not in available_sizes:
        raise InvalidSizeError("Please choose a size.")

    if item.stock is not None:
        item.stock -= 1

    redemption = Redemption(
        member_id=member.id, store_item_id=item.id,
        item_name=item.name, cost=item.cost,
        size=size if available_sizes else None,
    )
    db.add(redemption)
    await db.flush()  # need redemption.id for the linked Transaction below

    db.add(Transaction(
        member_id=member.id, delta=-item.cost, kind=TransactionKind.purchase,
        actor=actor, reason=f"Redeemed {item.name}", redemption_id=redemption.id,
    ))
    await db.flush()
    return redemption


async def fulfill(db: AsyncSession, redemption: Redemption, *, by: str) -> None:
    """Mark an order as physically handed over."""
    if redemption.status != RedemptionStatus.ordered:
        raise ValueError("Only an order awaiting pickup can be marked fulfilled.")
    redemption.status = RedemptionStatus.fulfilled
    redemption.resolved_at = now_utc()
    redemption.resolved_by = by


async def cancel(db: AsyncSession, redemption: Redemption, *, by: str, restock: bool = True) -> int:
    """Cancel an order and refund the member's cost as a new Transaction (the original
    purchase entry is never edited — the ledger is append-only). Restocks the item unless
    it's since been deleted. Returns the member's new balance."""
    if redemption.status != RedemptionStatus.ordered:
        raise ValueError("Only an order awaiting pickup can be cancelled.")
    redemption.status = RedemptionStatus.cancelled
    redemption.resolved_at = now_utc()
    redemption.resolved_by = by
    db.add(Transaction(
        member_id=redemption.member_id, delta=redemption.cost, kind=TransactionKind.refund,
        actor=by, reason=f"Refund: {redemption.item_name}", redemption_id=redemption.id,
    ))
    await db.flush()
    if restock:
        item = await db.get(StoreItem, redemption.store_item_id)
        if item is not None and item.stock is not None:
            item.stock += 1
    return await balance_for(db, redemption.member_id)
