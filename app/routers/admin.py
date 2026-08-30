"""
Admin / manager management UI — grant cash, run the rewards store, and fulfill orders.
Gated by Legion SSO: `merces-admin` (full) or `merces-manager` (grant/store/orders, but not
ledger adjustments/settings/backup).

Every mutation records an audit row (services/audit.py) in the same transaction as the
change it describes.
"""
import os
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import (
    AuditLog, Member, MemberKind, Redemption, RedemptionStatus, StoreItem, Transaction, TransactionKind,
)
from app.services import audit, store as store_service, uploads as uploads_service, wallet as wallet_service
from app.services.backup import is_sqlite, list_backups, nightly_backup, stage_restore
from app.services.legion_sync import LegionSyncError, sync_roster
from app.services.slack_client import send_dm
from app.services.sso import is_admin, is_staff, logout_url, make_authorize_url, sso_identity
from app.templating import templates

router = APIRouter(prefix="/admin")


# ── Auth guards ──────────────────────────────────────────────────────────────────

def _authorize_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(make_authorize_url(request), status_code=303)


def _forbidden(request: Request):
    return templates.TemplateResponse(
        "admin/forbidden.html", {"request": request}, status_code=403
    )


# ── Dashboard ────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)

    in_circulation = (
        await db.execute(select(func.coalesce(func.sum(Transaction.delta), 0)))
    ).scalar_one()
    pending_orders = (
        await db.execute(
            select(func.count()).select_from(Redemption)
            .where(Redemption.status == RedemptionStatus.ordered)
        )
    ).scalar_one()
    member_count = (
        await db.execute(
            select(func.count()).select_from(Member).where(Member.is_active.is_(True))
        )
    ).scalar_one()
    recent_grants = (
        await db.execute(
            select(Transaction)
            .options(selectinload(Transaction.member))
            .where(Transaction.kind == TransactionKind.grant)
            .order_by(Transaction.created_at.desc())
            .limit(8)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request, "active_page": "dashboard",
            "in_circulation": in_circulation, "pending_orders": pending_orders,
            "member_count": member_count, "recent_grants": recent_grants,
        },
    )


# ── Give cash ────────────────────────────────────────────────────────────────────

@router.get("/give", response_class=HTMLResponse)
async def give_form(request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    members = (
        await db.execute(
            select(Member)
            .where(Member.is_active.is_(True), Member.kind == MemberKind.student)
            .order_by(Member.name)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/give.html",
        {"request": request, "active_page": "give", "members": members,
         "message": request.query_params.get("message")},
    )


@router.post("/give")
async def give_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    member_ids: list[int] = Form(...),
    amount: int = Form(...),
    reason: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)

    if amount <= 0:
        return RedirectResponse("/admin/give?message=Enter+a+positive+amount.", status_code=303)

    actor = identity.get("name") or identity.get("username") or "admin"
    reason_clean = reason.strip() if reason and reason.strip() else None

    # Students only — mirrors the give.html dropdown, and belt-and-suspenders against a
    # hand-crafted request naming a mentor id that was never actually offered as an option.
    members = (
        await db.execute(
            select(Member).where(Member.id.in_(member_ids), Member.kind == MemberKind.student)
        )
    ).scalars().all()

    recipients: list[tuple[str, str]] = []  # (slack_user_id, text), captured while open
    for member in members:
        new_balance = await wallet_service.grant(db, member, amount, actor=actor, reason=reason_clean)
        await audit.record(
            db, request, "transaction.grant",
            f"{actor} granted {member.name} {amount} {settings.currency_name}"
            + (f" for {reason_clean}" if reason_clean else ""),
            entity_type="member", entity_id=member.id, actor=actor,
        )
        if member.slack_user_id:
            text = (
                f"🎉 You got *{amount} {settings.currency_name}* from {actor}"
                + (f" for {reason_clean}" if reason_clean else "")
                + f"! New balance: {new_balance}."
            )
            recipients.append((member.slack_user_id, text))
    await db.commit()

    background_tasks.add_task(_deliver_dms, recipients)
    return RedirectResponse(
        f"/admin/give?message=Granted+{amount}+to+{len(members)}+member(s).", status_code=303
    )


async def _deliver_dms(recipients: list[tuple[str, str]]) -> None:
    for slack_user_id, text in recipients:
        await send_dm(slack_user_id, text, automated=True)


# ── Store ────────────────────────────────────────────────────────────────────────

@router.get("/store", response_class=HTMLResponse)
async def store_admin(request: Request, show_archived: int = 0, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    items = await store_service.all_items(db, show_archived=bool(show_archived))
    return templates.TemplateResponse(
        "admin/store.html",
        {"request": request, "active_page": "store", "items": items,
         "show_archived": bool(show_archived),
         "message": request.query_params.get("message")},
    )


@router.post("/store")
async def store_create(
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    cost: int = Form(...),
    stock: Optional[str] = Form(None),
    emoji: Optional[str] = Form(None),
    sizes: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    if cost <= 0:
        return RedirectResponse("/admin/store?message=Cost+must+be+positive.", status_code=303)

    # A browser submits an empty file part (filename="") when no file is chosen — FastAPI
    # gives us an UploadFile either way, so "was something actually uploaded?" is judged
    # by filename, not by `image is None`.
    image_filename = None
    if image is not None and image.filename:
        try:
            image_filename = await uploads_service.save_store_image(image)
        except uploads_service.InvalidImageError as e:
            return RedirectResponse(f"/admin/store?message={quote(str(e), safe='')}", status_code=303)

    order = len((await db.execute(select(StoreItem.id))).scalars().all())
    item = StoreItem(
        name=name.strip(),
        description=description.strip() if description and description.strip() else None,
        cost=cost,
        stock=int(stock) if stock and stock.strip() else None,
        emoji=emoji.strip() if emoji and emoji.strip() else None,
        sizes=", ".join(store_service.parse_sizes(sizes)) or None,
        image_filename=image_filename,
        sort_order=order,
    )
    db.add(item)
    await db.flush()
    await audit.record(db, request, "store_item.create", f"Added store item {item.name}",
                       entity_type="store_item", entity_id=item.id)
    await db.commit()
    return RedirectResponse("/admin/store?message=Item+added", status_code=303)


@router.post("/store/{item_id}/edit")
async def store_edit(
    item_id: int,
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    cost: int = Form(...),
    stock: Optional[str] = Form(None),
    emoji: Optional[str] = Form(None),
    sizes: Optional[str] = Form(None),
    sort_order: int = Form(0),
    image: Optional[UploadFile] = File(None),
    remove_image: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)

    item = await store_service.get_item(db, item_id)
    if item is None:
        return RedirectResponse("/admin/store", status_code=303)

    if image is not None and image.filename:
        try:
            new_filename = await uploads_service.save_store_image(image)
        except uploads_service.InvalidImageError as e:
            return RedirectResponse(f"/admin/store?message={quote(str(e), safe='')}", status_code=303)
        uploads_service.delete_store_image(item.image_filename)
        item.image_filename = new_filename
    elif remove_image:
        uploads_service.delete_store_image(item.image_filename)
        item.image_filename = None

    item.name = name.strip()
    item.description = description.strip() if description and description.strip() else None
    item.cost = max(1, cost)
    item.stock = int(stock) if stock and stock.strip() else None
    item.emoji = emoji.strip() if emoji and emoji.strip() else None
    item.sizes = ", ".join(store_service.parse_sizes(sizes)) or None
    item.sort_order = sort_order
    await audit.record(db, request, "store_item.edit", f"Edited store item {item.name}",
                       entity_type="store_item", entity_id=item.id)
    await db.commit()
    return RedirectResponse("/admin/store?message=Item+saved", status_code=303)


@router.post("/store/{item_id}/archive")
async def store_archive(item_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Toggle an item's active state. Archiving hides it from the student store and
    (by default) this admin list, without touching its order history — the reversible,
    one-click counterpart to /delete."""
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)

    item = await store_service.get_item(db, item_id)
    if item:
        item.is_active = not item.is_active
        verb = "archive" if not item.is_active else "restore"
        await audit.record(
            db, request, f"store_item.{verb}", f"{verb.capitalize()}d store item {item.name}",
            entity_type="store_item", entity_id=item.id,
        )
        await db.commit()
    return RedirectResponse("/admin/store?show_archived=1", status_code=303)


@router.post("/store/{item_id}/delete")
async def store_delete(item_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Permanently remove a store item — only allowed when it's never been ordered.
    An item with order history must be archived instead (uncheck Active), since
    Redemption.item_name/cost/size are snapshots, not a live lookup, but the item still
    needs to exist for admin bookkeeping (and its stale photo file to get cleaned up
    only once nothing references it)."""
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)

    item = await store_service.get_item(db, item_id)
    if item is None:
        return RedirectResponse("/admin/store", status_code=303)

    if await store_service.has_redemptions(db, item.id):
        msg = "This item has order history — archive it instead of deleting."
        return RedirectResponse(f"/admin/store?show_archived=1&message={quote(msg, safe='')}", status_code=303)

    uploads_service.delete_store_image(item.image_filename)
    name = item.name
    await db.delete(item)
    await audit.record(db, request, "store_item.delete", f"Deleted store item {name}",
                       entity_type="store_item", entity_id=item_id)
    await db.commit()
    return RedirectResponse("/admin/store?show_archived=1&message=Item+deleted", status_code=303)


# ── Orders ───────────────────────────────────────────────────────────────────────

@router.get("/orders", response_class=HTMLResponse)
async def orders_list(request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    orders = (
        await db.execute(
            select(Redemption)
            .options(selectinload(Redemption.member))
            .order_by(Redemption.status.asc(), Redemption.created_at.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/orders.html",
        {"request": request, "active_page": "orders", "orders": orders,
         "message": request.query_params.get("message")},
    )


@router.post("/orders/{order_id}/fulfill")
async def orders_fulfill(order_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)

    redemption = await db.get(Redemption, order_id)
    if redemption is None:
        return RedirectResponse("/admin/orders", status_code=303)

    by = identity.get("name") or identity.get("username") or "admin"
    try:
        await wallet_service.fulfill(db, redemption, by=by)
    except ValueError as e:
        return RedirectResponse(f"/admin/orders?message={quote(str(e), safe='')}", status_code=303)

    await audit.record(db, request, "redemption.fulfill", f"Fulfilled order #{redemption.id}",
                       entity_type="redemption", entity_id=redemption.id, actor=by)
    await db.commit()
    return RedirectResponse("/admin/orders?message=Order+marked+fulfilled", status_code=303)


@router.post("/orders/{order_id}/cancel")
async def orders_cancel(
    order_id: int, request: Request, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)

    redemption = await db.get(Redemption, order_id)
    if redemption is None:
        return RedirectResponse("/admin/orders", status_code=303)

    by = identity.get("name") or identity.get("username") or "admin"
    member = await db.get(Member, redemption.member_id)
    item_name, cost = redemption.item_name, redemption.cost
    try:
        new_balance = await wallet_service.cancel(db, redemption, by=by)
    except ValueError as e:
        return RedirectResponse(f"/admin/orders?message={quote(str(e), safe='')}", status_code=303)

    await audit.record(
        db, request, "redemption.cancel",
        f"Cancelled order #{redemption.id} ({item_name}) — refunded {cost}",
        entity_type="redemption", entity_id=redemption.id, actor=by,
    )
    await db.commit()

    if member and member.slack_user_id:
        text = (
            f"↩️ Your *{item_name}* order was cancelled and {cost} {settings.currency_name} "
            f"refunded. New balance: {new_balance}."
        )
        background_tasks.add_task(send_dm, member.slack_user_id, text, automated=True)
    return RedirectResponse("/admin/orders?message=Order+cancelled+and+refunded", status_code=303)


# ── Ledger / balances ────────────────────────────────────────────────────────────

_LEDGER_SORT_KEYS = ("name", "balance")


@router.get("/ledger", response_class=HTMLResponse)
async def ledger_list(
    request: Request, sort: str = "balance", dir: str = "desc",
    db: AsyncSession = Depends(get_db),
):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    # Students only — mentors can never hold a balance (see wallet.grant()), so they'd
    # only ever show a dead $0 row here.
    members = (
        await db.execute(
            select(Member)
            .where(Member.is_active.is_(True), Member.kind == MemberKind.student)
            .order_by(Member.name)
        )
    ).scalars().all()
    balances = await wallet_service.balances_for_all(db)
    rows = [{"member": m, "balance": balances.get(m.id, 0)} for m in members]

    sort = sort if sort in _LEDGER_SORT_KEYS else "balance"
    sort_dir = dir if dir in ("asc", "desc") else "desc"
    sort_key = (lambda r: r["member"].name.lower()) if sort == "name" else (lambda r: r["balance"])
    rows.sort(key=sort_key, reverse=(sort_dir == "desc"))

    return templates.TemplateResponse(
        "admin/ledger.html",
        {"request": request, "active_page": "ledger", "rows": rows, "sort": sort, "dir": sort_dir},
    )


@router.get("/ledger/{member_id}", response_class=HTMLResponse)
async def ledger_member(member_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    member = await db.get(Member, member_id)
    if member is None or member.kind != MemberKind.student:
        return RedirectResponse("/admin/ledger", status_code=303)
    transactions = (
        await db.execute(
            select(Transaction).where(Transaction.member_id == member_id)
            .order_by(Transaction.created_at.desc())
        )
    ).scalars().all()
    balance = await wallet_service.balance_for(db, member_id)
    return templates.TemplateResponse(
        "admin/member.html",
        {
            "request": request, "active_page": "ledger",
            "member": member, "transactions": transactions, "balance": balance,
            "can_adjust": is_admin(identity),
            "message": request.query_params.get("message"),
        },
    )


@router.post("/ledger/{member_id}/adjust")
async def ledger_adjust(
    member_id: int, request: Request,
    delta: int = Form(...), reason: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_admin(identity):
        return _forbidden(request)

    member = await db.get(Member, member_id)
    if member is None or member.kind != MemberKind.student:
        return RedirectResponse("/admin/ledger", status_code=303)
    if delta == 0:
        return RedirectResponse(
            f"/admin/ledger/{member_id}?message=Enter+a+non-zero+amount.", status_code=303
        )
    actor = identity.get("name") or identity.get("username") or "admin"
    reason_clean = reason.strip() if reason and reason.strip() else None
    await wallet_service.adjust(db, member, delta, actor=actor, reason=reason_clean)
    await audit.record(
        db, request, "transaction.adjust",
        f"{actor} adjusted {member.name} by {delta:+d}" + (f" for {reason_clean}" if reason_clean else ""),
        entity_type="member", entity_id=member.id, actor=actor,
    )
    await db.commit()
    return RedirectResponse(f"/admin/ledger/{member_id}?message=Adjustment+applied", status_code=303)


# ── Roster ─────────────────────────────────────────────────────────────────────────

@router.get("/roster", response_class=HTMLResponse)
async def roster(request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    members = (
        await db.execute(select(Member).order_by(Member.is_active.desc(), Member.name))
    ).scalars().all()
    balances = await wallet_service.balances_for_all(db)

    from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting
    last_synced = await get_setting(db, LEGION_LAST_SYNCED_KEY)

    return templates.TemplateResponse(
        "admin/roster.html",
        {"request": request, "active_page": "roster",
         "students": [m for m in members if m.kind == MemberKind.student],
         "mentors": [m for m in members if m.kind == MemberKind.mentor],
         "balances": balances, "last_synced": last_synced,
         "legion_configured": bool(settings.legion_base_url and settings.legion_api_key),
         "message": request.query_params.get("message")},
    )


@router.post("/roster/sync")
async def roster_sync(request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    try:
        summary = await sync_roster(db, full=True)
        msg = f"Synced {summary}"
    except LegionSyncError as e:
        msg = f"Sync failed: {e}"
    return RedirectResponse(f"/admin/roster?message={msg.replace(' ', '+')}", status_code=303)


# ── Audit log ──────────────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def audit_log(request: Request, db: AsyncSession = Depends(get_db)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_staff(identity):
        return _forbidden(request)
    rows = (
        await db.execute(select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(200))
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/audit.html", {"request": request, "active_page": "audit", "rows": rows}
    )


# ── Backup (admin only) ──────────────────────────────────────────────────────────

@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_admin(identity):
        return _forbidden(request)
    return templates.TemplateResponse(
        "admin/backup.html",
        {"request": request, "active_page": "backup", "backups": list_backups(),
         "is_sqlite": is_sqlite(), "message": request.query_params.get("message")},
    )


@router.post("/backup/snapshot")
async def backup_snapshot(request: Request):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_admin(identity):
        return _forbidden(request)
    try:
        nightly_backup()
        msg = "Snapshot created"
    except Exception:
        msg = "Snapshot failed"
    return RedirectResponse(f"/admin/backup?message={msg.replace(' ', '+')}", status_code=303)


@router.post("/backup/restore")
async def backup_restore(request: Request, file: UploadFile = File(...)):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_admin(identity):
        return _forbidden(request)
    ok, message = stage_restore(await file.read())
    return RedirectResponse(f"/admin/backup?message={message.replace(' ', '+')}", status_code=303)


@router.get("/backup/download/{name}")
async def backup_download(name: str, request: Request):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_admin(identity):
        return _forbidden(request)
    # Guard against path traversal — only a bare filename in the backup dir.
    safe = os.path.basename(name)
    path = os.path.join(settings.backup_dir, safe)
    if safe != name or not os.path.isfile(path):
        return RedirectResponse("/admin/backup?message=Not+found", status_code=303)
    return FileResponse(path, filename=safe, media_type="application/octet-stream")


# ── Settings (admin only, read-only view) ─────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    identity = sso_identity(request)
    if identity is None:
        return _authorize_redirect(request)
    if not is_admin(identity):
        return _forbidden(request)
    return templates.TemplateResponse(
        "admin/settings.html", {"request": request, "active_page": "settings", "settings": settings}
    )


@router.get("/logout")
async def admin_logout(request: Request):
    return RedirectResponse(logout_url(request, return_to="/admin"), status_code=303)
