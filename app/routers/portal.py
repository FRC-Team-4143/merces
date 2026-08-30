"""
Member portal — a person's own merces at `/me`: balance, transaction history, the rewards
store, and their order history.

Identity is the shared `mw_sso` Legion cookie (see `services/sso.py`); any active roster
member (student or mentor) gets in — no group required. A fresh browser gets onto the
cookie via `/enter`, the one-tap Slack bootstrap.
"""
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Member, Redemption, Transaction
from app.services import legion_auth, store as store_service, wallet as wallet_service
from app.services import audit
from app.services.legion_auth import safe_next
from app.services.slack_client import post_to_channel, send_dm
from app.services.sso import logout_url, make_authorize_url, sso_identity
from app.templating import templates

router = APIRouter()


# ── Member identity ──────────────────────────────────────────────────────────────

async def _current_member(request: Request, db: AsyncSession) -> Optional[Member]:
    identity = sso_identity(request)
    if identity is None:
        return None
    member = (
        await db.execute(select(Member).where(Member.member_code == identity["member_code"]))
    ).scalars().first()
    if member is None or not member.is_active:
        return None
    return member


def _signin_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"/me?next={quote(next_path, safe='')}", status_code=303)


# ── Landing / dashboard ──────────────────────────────────────────────────────────

@router.get("/")
async def root():
    return RedirectResponse("/me", status_code=307)


@router.get("/me", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db), next: str = ""):
    member = await _current_member(request, db)
    if not member:
        identity = sso_identity(request)
        return_to = safe_next(next) if next else None
        context = {"request": request, "authorize_url": make_authorize_url(request, return_to=return_to)}
        if identity is not None:
            context["not_synced"] = True
            context["signed_in_name"] = identity.get("name") or "that account"
        return templates.TemplateResponse("portal/identify.html", context)

    balance = await wallet_service.balance_for(db, member.id)
    recent = (
        await db.execute(
            select(Transaction)
            .where(Transaction.member_id == member.id)
            .order_by(Transaction.created_at.desc())
            .limit(15)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        "portal/home.html",
        {
            "request": request, "active_page": "home",
            "member": member, "balance": balance, "recent": recent,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/enter")
async def enter(
    request: Request, member: str = "", next: str = "/me", db: AsyncSession = Depends(get_db)
):
    """One-tap sign-in bootstrap. If the browser already holds a live `mw_sso` cookie,
    skip Legion entirely; otherwise start a Legion SSO challenge for the known member and
    send the browser to the "check Slack" pending page. Passes an **absolute** return_to
    so the fresh-sign-in path lands back on Merces's host."""
    next_path = safe_next(next)
    if sso_identity(request) is not None:
        return RedirectResponse(next_path, status_code=303)

    row = None
    if member:
        row = (
            await db.execute(
                select(Member).where(Member.member_code == member, Member.is_active.is_(True))
            )
        ).scalars().first()
    if row is None:
        return RedirectResponse(make_authorize_url(request, return_to=next_path), status_code=303)

    pending_url = await legion_auth.start_challenge(
        row.member_code, return_to=f"{settings.base_url}{next_path}"
    )
    if pending_url is None:
        return templates.TemplateResponse(
            "portal/sso_unavailable.html", {"request": request}, status_code=503
        )
    return RedirectResponse(pending_url, status_code=303)


@router.get("/me/logout")
async def logout(request: Request):
    return RedirectResponse(logout_url(request, return_to="/me"), status_code=303)


# ── Rewards store ─────────────────────────────────────────────────────────────────

@router.get("/store", response_class=HTMLResponse)
async def browse_store(request: Request, db: AsyncSession = Depends(get_db)):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/store")
    items = await store_service.active_items(db)
    balance = await wallet_service.balance_for(db, member.id)
    return templates.TemplateResponse(
        "portal/store.html",
        {
            "request": request, "active_page": "store",
            "member": member, "items": items, "balance": balance,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/store/{item_id}/redeem")
async def redeem(
    item_id: int, request: Request, background_tasks: BackgroundTasks,
    size: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/store")

    item = await store_service.get_item(db, item_id)
    if item is None:
        return RedirectResponse("/store?message=That+item+wasn't+found.", status_code=303)

    try:
        redemption = await wallet_service.purchase(db, member, item, size=size)
    except (
        wallet_service.InsufficientBalanceError, wallet_service.ItemUnavailableError,
        wallet_service.InvalidSizeError,
    ) as e:
        return RedirectResponse(f"/store?message={quote(str(e), safe='')}", status_code=303)

    balance = await wallet_service.balance_for(db, member.id)
    await audit.record(
        db, request, "redemption.create",
        f"{member.name} redeemed {item.name} for {item.cost}",
        entity_type="redemption", entity_id=redemption.id, actor=member.name,
    )
    await db.commit()

    # Capture plain values (not ORM objects) before the background task runs without a
    # live session — matches the sibling apps' "capture recipients while open" pattern.
    background_tasks.add_task(
        _notify_purchase, member.name, member.slack_user_id, item.name, item.cost, balance,
    )
    # The whole message is percent-encoded (not just item.name) — a raw emoji dropped
    # straight into a Location header isn't latin-1-encodable and would crash the response.
    msg = quote(f"🎉 You redeemed {item.name}!", safe="")
    return RedirectResponse(f"/store?message={msg}", status_code=303)


async def _notify_purchase(member_name, slack_user_id, item_name, cost, balance):
    if settings.slack_announce_channel:
        text = (
            f"🛍️ *{member_name}* redeemed *{item_name}* for {cost} {settings.currency_name}. "
            f"New balance: {balance}. Hand it over, then mark it fulfilled at "
            f"{settings.base_url}/admin/orders"
        )
        await post_to_channel(settings.slack_announce_channel, text, automated=True)
    if slack_user_id:
        await send_dm(
            slack_user_id,
            f"✅ You redeemed *{item_name}* for {cost} {settings.currency_name}. "
            f"A staff member will hand it over soon.",
            automated=True,
        )


# ── My orders ─────────────────────────────────────────────────────────────────────

@router.get("/orders", response_class=HTMLResponse)
async def my_orders(request: Request, db: AsyncSession = Depends(get_db)):
    member = await _current_member(request, db)
    if not member:
        return _signin_redirect("/orders")
    orders = (
        await db.execute(
            select(Redemption)
            .where(Redemption.member_id == member.id)
            .order_by(Redemption.created_at.desc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "portal/orders.html",
        {"request": request, "active_page": "orders", "member": member, "orders": orders},
    )
