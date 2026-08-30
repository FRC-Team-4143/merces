"""Roster mirror upsert from Legion's API shape."""
import pytest
from sqlalchemy import select

from app.models import Member, MemberKind
from app.services.legion_sync import _upsert_members


def _payload(**over):
    base = {
        "member_code": "abc12345",
        "name": "Pat Person",
        "role": "student",
        "team_number": 4143,
        "slack_user_id": "UPAT",
        "is_active": True,
        "groups": [{"slug": "merces-manager", "label": "Merces Manager"}],
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_upsert_creates_member_with_groups(db):
    await _upsert_members(db, [_payload()])
    m = (await db.execute(select(Member).where(Member.member_code == "abc12345"))).scalars().first()
    assert m is not None
    assert m.kind == MemberKind.student
    assert m.team_number == 4143
    assert m.group_slugs == "merces-manager"
    assert m.has_group("merces-manager")


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_updates(db):
    await _upsert_members(db, [_payload()])
    await _upsert_members(db, [_payload(name="Pat Renamed", role="mentor", groups=[])])
    members = (await db.execute(select(Member).where(Member.member_code == "abc12345"))).scalars().all()
    assert len(members) == 1
    assert members[0].name == "Pat Renamed"
    assert members[0].kind == MemberKind.mentor
    assert members[0].group_slugs is None


@pytest.mark.asyncio
async def test_upsert_backlinks_by_slack_id(db):
    # A pre-existing row that has no member_code yet but shares the Slack id.
    db.add(Member(member_code=None, name="Old Name", slack_user_id="UPAT"))
    await db.commit()
    await _upsert_members(db, [_payload()])
    members = (await db.execute(select(Member).where(Member.slack_user_id == "UPAT"))).scalars().all()
    assert len(members) == 1  # back-linked, not duplicated
    assert members[0].member_code == "abc12345"
