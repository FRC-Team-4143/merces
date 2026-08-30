"""Ledger service — balance derivation, grants, purchases, refunds, adjustments."""
import pytest

from app.models import MemberKind, RedemptionStatus, StoreItem
from app.services import wallet as wallet_service


@pytest.mark.asyncio
async def test_balance_starts_at_zero(db, make_member):
    member = await make_member("Riley")
    assert await wallet_service.balance_for(db, member.id) == 0


@pytest.mark.asyncio
async def test_grant_increments_balance(db, make_member):
    member = await make_member("Riley")
    balance = await wallet_service.grant(db, member, 50, actor="ada.admin", reason="Great pit demo")
    await db.commit()
    assert balance == 50
    assert await wallet_service.balance_for(db, member.id) == 50


@pytest.mark.asyncio
async def test_grant_rejects_non_positive_amount(db, make_member):
    member = await make_member("Riley")
    with pytest.raises(ValueError):
        await wallet_service.grant(db, member, 0, actor="ada.admin")


@pytest.mark.asyncio
async def test_grant_rejects_a_mentor(db, make_member):
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)
    with pytest.raises(ValueError):
        await wallet_service.grant(db, mentor, 50, actor="ada.admin")
    assert await wallet_service.balance_for(db, mentor.id) == 0


@pytest.mark.asyncio
async def test_purchase_deducts_balance_and_creates_ordered_redemption(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="T-Shirt", cost=40, stock=3)
    db.add(item)
    await db.flush()

    redemption = await wallet_service.purchase(db, member, item)
    await db.commit()

    assert redemption.status == RedemptionStatus.ordered
    assert redemption.cost == 40
    assert redemption.item_name == "T-Shirt"
    assert await wallet_service.balance_for(db, member.id) == 60
    assert item.stock == 2


@pytest.mark.asyncio
async def test_purchase_blocked_when_balance_too_low(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 10, actor="ada.admin")
    item = StoreItem(name="Hoodie", cost=50, stock=None)
    db.add(item)
    await db.flush()

    with pytest.raises(wallet_service.InsufficientBalanceError):
        await wallet_service.purchase(db, member, item)
    assert await wallet_service.balance_for(db, member.id) == 10  # unchanged


@pytest.mark.asyncio
async def test_purchase_of_sized_item_requires_a_size(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="T-Shirt", cost=40, sizes="S,M,L,XL")
    db.add(item)
    await db.flush()

    with pytest.raises(wallet_service.InvalidSizeError):
        await wallet_service.purchase(db, member, item)
    assert await wallet_service.balance_for(db, member.id) == 100  # unchanged


@pytest.mark.asyncio
async def test_purchase_of_sized_item_rejects_an_unlisted_size(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="T-Shirt", cost=40, sizes="S,M,L,XL")
    db.add(item)
    await db.flush()

    with pytest.raises(wallet_service.InvalidSizeError):
        await wallet_service.purchase(db, member, item, size="XXL")


@pytest.mark.asyncio
async def test_purchase_of_sized_item_snapshots_the_chosen_size(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="T-Shirt", cost=40, sizes="S, M, L, XL")
    db.add(item)
    await db.flush()

    redemption = await wallet_service.purchase(db, member, item, size="M")
    await db.commit()

    assert redemption.size == "M"


@pytest.mark.asyncio
async def test_purchase_of_unsized_item_ignores_a_stray_size(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Sticker", cost=5)  # no sizes configured
    db.add(item)
    await db.flush()

    redemption = await wallet_service.purchase(db, member, item, size="M")
    await db.commit()

    assert redemption.size is None


@pytest.mark.asyncio
async def test_purchase_blocked_when_out_of_stock(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Sticker", cost=5, stock=0)
    db.add(item)
    await db.flush()

    with pytest.raises(wallet_service.ItemUnavailableError):
        await wallet_service.purchase(db, member, item)


@pytest.mark.asyncio
async def test_purchase_blocked_when_item_archived(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Old Item", cost=5, is_active=False)
    db.add(item)
    await db.flush()

    with pytest.raises(wallet_service.ItemUnavailableError):
        await wallet_service.purchase(db, member, item)


@pytest.mark.asyncio
async def test_cancel_refunds_and_restocks(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Hat", cost=30, stock=2)
    db.add(item)
    await db.flush()

    redemption = await wallet_service.purchase(db, member, item)
    await db.commit()
    assert item.stock == 1
    assert await wallet_service.balance_for(db, member.id) == 70

    new_balance = await wallet_service.cancel(db, redemption, by="ada.admin")
    await db.commit()

    assert redemption.status == RedemptionStatus.cancelled
    assert new_balance == 100
    assert item.stock == 2


@pytest.mark.asyncio
async def test_cannot_cancel_a_fulfilled_order(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Poster", cost=10, stock=None)
    db.add(item)
    await db.flush()
    redemption = await wallet_service.purchase(db, member, item)
    await db.commit()
    await wallet_service.fulfill(db, redemption, by="ada.admin")
    await db.commit()

    with pytest.raises(ValueError):
        await wallet_service.cancel(db, redemption, by="ada.admin")


@pytest.mark.asyncio
async def test_fulfill_marks_order_picked_up(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Poster", cost=10, stock=None)
    db.add(item)
    await db.flush()
    redemption = await wallet_service.purchase(db, member, item)
    await db.commit()

    await wallet_service.fulfill(db, redemption, by="ada.admin")
    await db.commit()

    assert redemption.status == RedemptionStatus.fulfilled
    assert redemption.resolved_by == "ada.admin"


@pytest.mark.asyncio
async def test_cannot_fulfill_twice(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Poster", cost=10, stock=None)
    db.add(item)
    await db.flush()
    redemption = await wallet_service.purchase(db, member, item)
    await db.commit()
    await wallet_service.fulfill(db, redemption, by="ada.admin")
    await db.commit()

    with pytest.raises(ValueError):
        await wallet_service.fulfill(db, redemption, by="ada.admin")


@pytest.mark.asyncio
async def test_adjust_can_go_negative(db, make_member):
    member = await make_member("Riley")
    await wallet_service.grant(db, member, 20, actor="ada.admin")
    new_balance = await wallet_service.adjust(db, member, -30, actor="ada.admin", reason="correction")
    await db.commit()
    assert new_balance == -10
    assert await wallet_service.balance_for(db, member.id) == -10


@pytest.mark.asyncio
async def test_adjust_rejects_zero(db, make_member):
    member = await make_member("Riley")
    with pytest.raises(ValueError):
        await wallet_service.adjust(db, member, 0, actor="ada.admin")


@pytest.mark.asyncio
async def test_adjust_rejects_a_mentor(db, make_member):
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)
    with pytest.raises(ValueError):
        await wallet_service.adjust(db, mentor, 50, actor="ada.admin")
    assert await wallet_service.balance_for(db, mentor.id) == 0


@pytest.mark.asyncio
async def test_balances_for_all(db, make_member):
    a = await make_member("Alice")
    b = await make_member("Bob")
    await wallet_service.grant(db, a, 30, actor="ada.admin")
    await wallet_service.grant(db, b, 10, actor="ada.admin")
    await db.commit()

    balances = await wallet_service.balances_for_all(db)
    assert balances[a.id] == 30
    assert balances[b.id] == 10
