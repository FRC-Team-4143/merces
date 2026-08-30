"""Give Cash admin flow — students only. Mentors are staff in this app, not reward
recipients, so they must never appear as a grantable recipient anywhere in this path."""
import pytest

from app.models import MemberKind
from app.services import wallet as wallet_service


@pytest.mark.asyncio
async def test_give_form_only_lists_students(client, admin_cookie, make_member):
    student = await make_member("Riley Student", kind=MemberKind.student)
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)

    resp = await client.get("/admin/give", cookies={"mw_sso": admin_cookie})
    assert resp.status_code == 200
    assert student.name in resp.text
    assert mentor.name not in resp.text


@pytest.mark.asyncio
async def test_give_submit_ignores_a_mentor_id(client, db, admin_cookie, make_member):
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)

    resp = await client.post(
        "/admin/give", cookies={"mw_sso": admin_cookie},
        data={"member_ids": str(mentor.id), "amount": "50"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "0+member" in resp.headers["location"]
    assert await wallet_service.balance_for(db, mentor.id) == 0


@pytest.mark.asyncio
async def test_give_submit_grants_students_and_skips_mentors_in_the_same_batch(
    client, db, admin_cookie, make_member,
):
    student = await make_member("Riley Student", kind=MemberKind.student)
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)

    resp = await client.post(
        "/admin/give", cookies={"mw_sso": admin_cookie},
        data={"member_ids": [str(student.id), str(mentor.id)], "amount": "50"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "50+to+1+member" in resp.headers["location"]
    assert await wallet_service.balance_for(db, student.id) == 50
    assert await wallet_service.balance_for(db, mentor.id) == 0


@pytest.mark.asyncio
async def test_roster_splits_students_and_mentors_and_omits_mentor_balance(
    client, db, admin_cookie, make_member,
):
    student = await make_member("Riley Student", kind=MemberKind.student)
    mentor = await make_member("Coach Casey", kind=MemberKind.mentor)

    resp = await client.get("/admin/roster", cookies={"mw_sso": admin_cookie})
    assert resp.status_code == 200
    assert student.name in resp.text
    assert mentor.name in resp.text

    students_tab = resp.text.split('id="students-tab"')[1].split('id="mentors-tab"')[0]
    mentors_tab = resp.text.split('id="mentors-tab"')[1]
    assert "Balance" in students_tab
    assert student.name in students_tab
    assert mentor.name not in students_tab
    assert "Balance" not in mentors_tab
    assert mentor.name in mentors_tab
