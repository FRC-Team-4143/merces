"""Student self-serve redemption flow via the HTTP portal — instant deduction plus the
staff Slack ping (SLACK_ANNOUNCE_CHANNEL) that lets someone know to hand over the merch."""
import pytest

from app.config import settings
from app.models import StoreItem
from app.services import wallet as wallet_service
from tests.conftest import make_sso_cookie


@pytest.mark.asyncio
async def test_redeem_deducts_balance_and_notifies(client, db, make_member, fake_slack, monkeypatch):
    monkeypatch.setattr(settings, "slack_announce_channel", "C0PICKUP")

    member = await make_member("Riley", slack_user_id="URILEY", member_code="stu00001")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Hoodie", cost=40, stock=2)
    db.add(item)
    await db.commit()
    await db.refresh(item)

    cookie = make_sso_cookie(member_code="stu00001", name="Riley", slack_user_id="URILEY")
    resp = await client.post(
        f"/store/{item.id}/redeem", cookies={"mw_sso": cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "redeemed" in resp.headers["location"]

    assert await wallet_service.balance_for(db, member.id) == 60
    await db.refresh(item)
    assert item.stock == 1

    # Staff pickup ping in the announce channel, plus a confirmation DM to the student.
    assert any("Hoodie" in p["text"] for p in fake_slack.channel_posts)
    assert any("Hoodie" in d["text"] for d in fake_slack.dms)


@pytest.mark.asyncio
async def test_redeem_blocked_when_balance_too_low(client, db, make_member):
    member = await make_member("Riley", member_code="stu00002")
    item = StoreItem(name="Hoodie", cost=40, stock=2)
    db.add(item)
    await db.commit()
    await db.refresh(item)

    cookie = make_sso_cookie(member_code="stu00002", name="Riley")
    resp = await client.post(
        f"/store/{item.id}/redeem", cookies={"mw_sso": cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "enough" in resp.headers["location"]
    assert await wallet_service.balance_for(db, member.id) == 0
    await db.refresh(item)
    assert item.stock == 2  # untouched


@pytest.mark.asyncio
async def test_redeem_sized_item_with_a_size_succeeds(client, db, make_member):
    member = await make_member("Riley", member_code="stu00004")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="T-Shirt", cost=40, sizes="S,M,L,XL")
    db.add(item)
    await db.commit()
    await db.refresh(item)

    cookie = make_sso_cookie(member_code="stu00004", name="Riley")
    resp = await client.post(
        f"/store/{item.id}/redeem", cookies={"mw_sso": cookie},
        data={"size": "L"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "redeemed" in resp.headers["location"]
    assert await wallet_service.balance_for(db, member.id) == 60


@pytest.mark.asyncio
async def test_redeem_sized_item_without_a_size_is_blocked(client, db, make_member):
    member = await make_member("Riley", member_code="stu00005")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="T-Shirt", cost=40, sizes="S,M,L,XL")
    db.add(item)
    await db.commit()
    await db.refresh(item)

    cookie = make_sso_cookie(member_code="stu00005", name="Riley")
    resp = await client.post(
        f"/store/{item.id}/redeem", cookies={"mw_sso": cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "size" in resp.headers["location"]
    assert await wallet_service.balance_for(db, member.id) == 100  # unchanged


@pytest.mark.asyncio
async def test_redeem_blocked_when_out_of_stock(client, db, make_member):
    member = await make_member("Riley", member_code="stu00003")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Hoodie", cost=40, stock=0)
    db.add(item)
    await db.commit()
    await db.refresh(item)

    cookie = make_sso_cookie(member_code="stu00003", name="Riley")
    resp = await client.post(
        f"/store/{item.id}/redeem", cookies={"mw_sso": cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "stock" in resp.headers["location"]
    assert await wallet_service.balance_for(db, member.id) == 100  # untouched
