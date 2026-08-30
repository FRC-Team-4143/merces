"""Store catalog CRUD helpers."""
import pytest

from app.models import StoreItem
from app.services import store as store_service


@pytest.mark.asyncio
async def test_active_items_excludes_archived(db):
    db.add(StoreItem(name="Visible", cost=10, sort_order=0))
    db.add(StoreItem(name="Hidden", cost=10, is_active=False, sort_order=1))
    await db.commit()

    items = await store_service.active_items(db)
    names = [i.name for i in items]
    assert "Visible" in names
    assert "Hidden" not in names


@pytest.mark.asyncio
async def test_all_items_includes_archived(db):
    db.add(StoreItem(name="Visible", cost=10, sort_order=0))
    db.add(StoreItem(name="Hidden", cost=10, is_active=False, sort_order=1))
    await db.commit()

    items = await store_service.all_items(db)
    assert {i.name for i in items} == {"Visible", "Hidden"}


@pytest.mark.asyncio
async def test_all_items_show_archived_false_excludes_archived(db):
    db.add(StoreItem(name="Visible", cost=10, sort_order=0))
    db.add(StoreItem(name="Hidden", cost=10, is_active=False, sort_order=1))
    await db.commit()

    items = await store_service.all_items(db, show_archived=False)
    assert {i.name for i in items} == {"Visible"}


@pytest.mark.asyncio
async def test_items_ordered_by_sort_order(db):
    db.add(StoreItem(name="Second", cost=5, sort_order=1))
    db.add(StoreItem(name="First", cost=5, sort_order=0))
    await db.commit()

    items = await store_service.active_items(db)
    assert [i.name for i in items] == ["First", "Second"]


@pytest.mark.asyncio
async def test_get_item_returns_none_for_missing(db):
    assert await store_service.get_item(db, 9999) is None


def test_parse_sizes_splits_and_trims():
    assert store_service.parse_sizes("S, M, L,XL") == ["S", "M", "L", "XL"]


def test_parse_sizes_drops_empty_entries():
    assert store_service.parse_sizes("S,,M,") == ["S", "M"]


def test_parse_sizes_of_blank_or_none_is_empty():
    assert store_service.parse_sizes(None) == []
    assert store_service.parse_sizes("") == []
    assert store_service.parse_sizes("   ") == []


@pytest.mark.asyncio
async def test_has_redemptions_false_for_a_never_ordered_item(db):
    item = StoreItem(name="Never Ordered", cost=10)
    db.add(item)
    await db.flush()

    assert await store_service.has_redemptions(db, item.id) is False


@pytest.mark.asyncio
async def test_has_redemptions_true_once_ordered(db, make_member):
    from app.services import wallet as wallet_service

    member = await make_member("Riley")
    await wallet_service.grant(db, member, 100, actor="ada.admin")
    item = StoreItem(name="Poster", cost=10)
    db.add(item)
    await db.flush()
    await wallet_service.purchase(db, member, item)
    await db.commit()

    assert await store_service.has_redemptions(db, item.id) is True
