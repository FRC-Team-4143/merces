"""Store item photo upload through the admin HTTP router: create/replace/remove, and
that a rejected upload doesn't leave a half-created item behind.

Note: the /uploads StaticFiles mount binds its serving directory once at import time
(app/main.py), so monkeypatching settings.upload_dir mid-test doesn't redirect it — the
"served correctly" test below uses the real default directory (and cleans up after
itself); the create/replace/remove tests use a monkeypatched tmp_path since they only
need to assert database + on-disk state, not the HTTP-served bytes.
"""
import os

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import StoreItem


@pytest.mark.asyncio
async def test_uploaded_photo_is_served_at_its_url(client, db, admin_cookie):
    resp = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Served Hat", "cost": "20"},
        files={"image": ("hat.png", b"\x89PNGfakebytes", "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    item = (await db.execute(select(StoreItem).where(StoreItem.name == "Served Hat"))).scalars().first()
    assert item is not None and item.image_filename

    try:
        served = await client.get(f"/uploads/{item.image_filename}")
        assert served.status_code == 200
        assert served.content == b"\x89PNGfakebytes"
    finally:
        os.remove(os.path.join(settings.upload_dir, item.image_filename))


@pytest.mark.asyncio
async def test_replacing_photo_deletes_the_old_file(client, db, admin_cookie, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    create = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Replaceable", "cost": "20"},
        files={"image": ("v1.png", b"version-one", "image/png")},
        follow_redirects=False,
    )
    assert create.status_code == 303
    item = (await db.execute(select(StoreItem).where(StoreItem.name == "Replaceable"))).scalars().first()
    old_path = tmp_path / item.image_filename
    assert old_path.exists()

    edit = await client.post(
        f"/admin/store/{item.id}/edit",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Replaceable", "cost": "20"},
        files={"image": ("v2.png", b"version-two", "image/png")},
        follow_redirects=False,
    )
    assert edit.status_code == 303
    await db.refresh(item)

    assert not old_path.exists()  # old file cleaned up
    new_path = tmp_path / item.image_filename
    assert new_path.exists()
    assert new_path.read_bytes() == b"version-two"


@pytest.mark.asyncio
async def test_remove_photo_checkbox_clears_field_and_deletes_file(client, db, admin_cookie, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    create = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Removable", "cost": "20"},
        files={"image": ("v1.png", b"version-one", "image/png")},
        follow_redirects=False,
    )
    assert create.status_code == 303
    item = (await db.execute(select(StoreItem).where(StoreItem.name == "Removable"))).scalars().first()
    saved_path = tmp_path / item.image_filename

    edit = await client.post(
        f"/admin/store/{item.id}/edit",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Removable", "cost": "20", "remove_image": "on"},
        follow_redirects=False,
    )
    assert edit.status_code == 303
    await db.refresh(item)

    assert item.image_filename is None
    assert not saved_path.exists()


@pytest.mark.asyncio
async def test_rejected_upload_does_not_create_the_item(client, db, admin_cookie, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    resp = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Should Not Exist", "cost": "20"},
        files={"image": ("virus.exe", b"whatever", "application/octet-stream")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    item = (
        await db.execute(select(StoreItem).where(StoreItem.name == "Should Not Exist"))
    ).scalars().first()
    assert item is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_creating_with_sizes_normalizes_the_list(client, db, admin_cookie):
    resp = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "T-Shirt", "cost": "20", "sizes": "S, M ,,L"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    item = (await db.execute(select(StoreItem).where(StoreItem.name == "T-Shirt"))).scalars().first()
    assert item.sizes == "S, M, L"


@pytest.mark.asyncio
async def test_creating_without_sizes_leaves_it_blank(client, db, admin_cookie):
    resp = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Sticker", "cost": "5"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    item = (await db.execute(select(StoreItem).where(StoreItem.name == "Sticker"))).scalars().first()
    assert item.sizes is None


@pytest.mark.asyncio
async def test_editing_can_clear_sizes(client, db, admin_cookie):
    create = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Hoodie", "cost": "40", "sizes": "S,M,L"},
        follow_redirects=False,
    )
    assert create.status_code == 303
    item = (await db.execute(select(StoreItem).where(StoreItem.name == "Hoodie"))).scalars().first()
    assert item.sizes == "S, M, L"

    edit = await client.post(
        f"/admin/store/{item.id}/edit",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Hoodie", "cost": "40"},  # sizes omitted
        follow_redirects=False,
    )
    assert edit.status_code == 303
    await db.refresh(item)
    assert item.sizes is None


@pytest.mark.asyncio
async def test_admin_store_hides_archived_by_default(client, db, admin_cookie):
    db.add(StoreItem(name="Visible Item", cost=10))
    db.add(StoreItem(name="Archived Item", cost=10, is_active=False))
    await db.commit()

    resp = await client.get("/admin/store", cookies={"mw_sso": admin_cookie})
    assert "Visible Item" in resp.text
    assert "Archived Item" not in resp.text


@pytest.mark.asyncio
async def test_admin_store_show_archived_reveals_archived_items(client, db, admin_cookie):
    db.add(StoreItem(name="Visible Item", cost=10))
    db.add(StoreItem(name="Archived Item", cost=10, is_active=False))
    await db.commit()

    resp = await client.get("/admin/store?show_archived=1", cookies={"mw_sso": admin_cookie})
    assert "Visible Item" in resp.text
    assert "Archived Item" in resp.text


@pytest.mark.asyncio
async def test_archive_toggle_hides_then_restore_brings_it_back(client, db, admin_cookie):
    item = StoreItem(name="Togglable", cost=10)
    db.add(item)
    await db.commit()
    await db.refresh(item)

    archive = await client.post(
        f"/admin/store/{item.id}/archive", cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert archive.status_code == 303
    await db.refresh(item)
    assert item.is_active is False

    default_view = await client.get("/admin/store", cookies={"mw_sso": admin_cookie})
    assert "Togglable" not in default_view.text

    restore = await client.post(
        f"/admin/store/{item.id}/archive", cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert restore.status_code == 303
    await db.refresh(item)
    assert item.is_active is True

    default_view_again = await client.get("/admin/store", cookies={"mw_sso": admin_cookie})
    assert "Togglable" in default_view_again.text


@pytest.mark.asyncio
async def test_deleting_a_never_ordered_item_removes_it(client, db, admin_cookie):
    create = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Mistake Item", "cost": "5"},
        follow_redirects=False,
    )
    assert create.status_code == 303
    item = (
        await db.execute(select(StoreItem).where(StoreItem.name == "Mistake Item"))
    ).scalars().first()

    resp = await client.post(
        f"/admin/store/{item.id}/delete", cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "deleted" in resp.headers["location"]
    remaining = (await db.execute(select(StoreItem).where(StoreItem.id == item.id))).scalars().first()
    assert remaining is None


@pytest.mark.asyncio
async def test_deleting_an_ordered_item_is_blocked(client, db, admin_cookie, make_member):
    from app.services import wallet as wallet_service

    member = await make_member("Riley", member_code="stu00006")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Already Ordered", cost=10)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    await wallet_service.purchase(db, member, item)
    await db.commit()

    resp = await client.post(
        f"/admin/store/{item.id}/delete", cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "archive" in resp.headers["location"]
    assert await db.get(StoreItem, item.id) is not None  # not deleted


@pytest.mark.asyncio
async def test_deleting_removes_its_photo_file(client, db, admin_cookie, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    create = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "Photo Item", "cost": "5"},
        files={"image": ("v1.png", b"version-one", "image/png")},
        follow_redirects=False,
    )
    assert create.status_code == 303
    item = (
        await db.execute(select(StoreItem).where(StoreItem.name == "Photo Item"))
    ).scalars().first()
    photo_path = tmp_path / item.image_filename
    assert photo_path.exists()

    resp = await client.post(
        f"/admin/store/{item.id}/delete", cookies={"mw_sso": admin_cookie}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not photo_path.exists()


@pytest.mark.asyncio
async def test_creating_without_a_photo_still_works(client, db, admin_cookie):
    resp = await client.post(
        "/admin/store",
        cookies={"mw_sso": admin_cookie},
        data={"name": "No Photo Item", "cost": "20"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    item = (
        await db.execute(select(StoreItem).where(StoreItem.name == "No Photo Item"))
    ).scalars().first()
    assert item is not None
    assert item.image_filename is None
