"""Ledger list — students only (mentors never hold a balance), sortable by name/balance."""
import pytest

from app.models import MemberKind
from app.services import wallet as wallet_service


@pytest.mark.asyncio
async def test_ledger_excludes_mentors(client, admin_cookie, make_member):
    student = await make_member("Riley Student", kind=MemberKind.student)
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)

    resp = await client.get("/admin/ledger", cookies={"mw_sso": admin_cookie})
    assert resp.status_code == 200
    assert student.name in resp.text
    assert mentor.name not in resp.text


@pytest.mark.asyncio
async def test_ledger_has_no_kind_column(client, admin_cookie, make_member):
    await make_member("Riley Student", kind=MemberKind.student)
    resp = await client.get("/admin/ledger", cookies={"mw_sso": admin_cookie})
    assert "Kind" not in resp.text


@pytest.mark.asyncio
async def test_ledger_defaults_to_balance_descending(client, db, admin_cookie, make_member):
    low = await make_member("Low Balance", kind=MemberKind.student)
    high = await make_member("High Balance", kind=MemberKind.student)
    await wallet_service.grant(db, low, 10, actor="ada.admin")
    await wallet_service.grant(db, high, 90, actor="ada.admin")
    await db.commit()

    resp = await client.get("/admin/ledger", cookies={"mw_sso": admin_cookie})
    assert resp.text.index(high.name) < resp.text.index(low.name)


@pytest.mark.asyncio
async def test_ledger_sort_by_balance_ascending(client, db, admin_cookie, make_member):
    low = await make_member("Low Balance", kind=MemberKind.student)
    high = await make_member("High Balance", kind=MemberKind.student)
    await wallet_service.grant(db, low, 10, actor="ada.admin")
    await wallet_service.grant(db, high, 90, actor="ada.admin")
    await db.commit()

    resp = await client.get("/admin/ledger?sort=balance&dir=asc", cookies={"mw_sso": admin_cookie})
    assert resp.text.index(low.name) < resp.text.index(high.name)


@pytest.mark.asyncio
async def test_ledger_sort_by_name(client, db, admin_cookie, make_member):
    zed = await make_member("Zed Zephyr", kind=MemberKind.student)
    amy = await make_member("Amy Alpha", kind=MemberKind.student)

    asc = await client.get("/admin/ledger?sort=name&dir=asc", cookies={"mw_sso": admin_cookie})
    assert asc.text.index(amy.name) < asc.text.index(zed.name)

    desc = await client.get("/admin/ledger?sort=name&dir=desc", cookies={"mw_sso": admin_cookie})
    assert desc.text.index(zed.name) < desc.text.index(amy.name)


@pytest.mark.asyncio
async def test_ledger_member_detail_redirects_for_a_mentor(client, admin_cookie, make_member):
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)
    resp = await client.get(
        f"/admin/ledger/{mentor.id}", cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ledger"


@pytest.mark.asyncio
async def test_ledger_adjust_rejects_a_mentor(client, db, admin_cookie, make_member):
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)
    resp = await client.post(
        f"/admin/ledger/{mentor.id}/adjust", cookies={"mw_sso": admin_cookie},
        data={"delta": "50"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ledger"
    assert await wallet_service.balance_for(db, mentor.id) == 0
