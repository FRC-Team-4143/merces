"""Admin order fulfillment/cancellation via the HTTP router, including audit trail and
the Slack notifications a cancellation triggers."""
import pytest
from sqlalchemy import select

from app.models import AuditLog, RedemptionStatus, StoreItem
from app.services import wallet as wallet_service


async def _ordered_redemption(db, member, *, cost=40, stock=2):
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Hoodie", cost=cost, stock=stock)
    db.add(item)
    await db.flush()
    redemption = await wallet_service.purchase(db, member, item)
    await db.commit()
    return redemption, item


@pytest.mark.asyncio
async def test_admin_orders_shows_order_number(client, db, make_member, admin_cookie):
    member = await make_member("Riley")
    redemption, _ = await _ordered_redemption(db, member)

    resp = await client.get("/admin/orders", cookies={"mw_sso": admin_cookie})
    assert f"#{redemption.id}" in resp.text


@pytest.mark.asyncio
async def test_portal_orders_shows_order_number(client, db, make_member):
    from tests.conftest import make_sso_cookie

    member = await make_member("Riley", member_code="stu00099")
    redemption, _ = await _ordered_redemption(db, member)

    cookie = make_sso_cookie(member_code="stu00099", name="Riley")
    resp = await client.get("/orders", cookies={"mw_sso": cookie})
    assert f"#{redemption.id}" in resp.text


@pytest.mark.asyncio
async def test_fulfill_order(client, db, make_member, admin_cookie):
    member = await make_member("Riley", slack_user_id="URILEY")
    redemption, _ = await _ordered_redemption(db, member)

    resp = await client.post(
        f"/admin/orders/{redemption.id}/fulfill",
        cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(redemption)
    assert redemption.status == RedemptionStatus.fulfilled


@pytest.mark.asyncio
async def test_cancel_order_refunds_restocks_and_notifies(client, db, make_member, admin_cookie, fake_slack):
    member = await make_member("Riley", slack_user_id="URILEY")
    redemption, item = await _ordered_redemption(db, member, cost=40, stock=2)
    assert item.stock == 1  # decremented by the purchase

    resp = await client.post(
        f"/admin/orders/{redemption.id}/cancel",
        cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303

    await db.refresh(redemption)
    await db.refresh(item)
    assert redemption.status == RedemptionStatus.cancelled
    assert item.stock == 2  # restocked
    assert await wallet_service.balance_for(db, member.id) == 100  # refunded

    assert any("refunded" in d["text"] for d in fake_slack.dms)

    rows = (await db.execute(select(AuditLog))).scalars().all()
    assert any(r.action == "redemption.cancel" for r in rows)


@pytest.mark.asyncio
async def test_cannot_fulfill_or_cancel_twice(client, db, make_member, admin_cookie):
    member = await make_member("Riley")
    redemption, _ = await _ordered_redemption(db, member)

    first = await client.post(
        f"/admin/orders/{redemption.id}/fulfill",
        cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert first.status_code == 303
    second = await client.post(
        f"/admin/orders/{redemption.id}/cancel",
        cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert second.status_code == 303  # redirects back with a "can't be cancelled" message
    await db.refresh(redemption)
    assert redemption.status == RedemptionStatus.fulfilled  # unchanged


@pytest.mark.asyncio
async def test_manager_can_fulfill_but_not_adjust_ledger(client, db, make_member, manager_cookie):
    member = await make_member("Riley")
    redemption, _ = await _ordered_redemption(db, member)

    resp = await client.post(
        f"/admin/orders/{redemption.id}/fulfill",
        cookies={"mw_sso": manager_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = await client.post(
        f"/admin/ledger/{member.id}/adjust",
        data={"delta": "5"},
        cookies={"mw_sso": manager_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 403
