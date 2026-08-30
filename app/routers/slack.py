"""
Slack routes.

  POST /slack/command — the `/merces` slash command (shows the caller's balance and a
                         one-tap link into the store)

Verified by the Slack signing secret. No interactive components in this MVP — orders are
self-serve on the web and fulfillment happens in person, so there's no button flow to
wire up yet (a future "mark picked up from Slack" button is a documented enhancement).
"""
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Member
from app.services import wallet as wallet_service

router = APIRouter(prefix="/slack")


# ── Signature verification ─────────────────────────────────────────────────────

async def _verify_slack_signature(request: Request) -> bytes:
    """Read raw body and verify Slack request signature. Raises 403 on failure."""
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="Slack integration is not configured (no signing secret set).")

    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    try:
        if abs(time.time() - float(timestamp)) > 300:
            raise HTTPException(status_code=403, detail="Request too old")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack signature")
    return body


# ── Slash command ──────────────────────────────────────────────────────────────

@router.post("/command")
async def slack_command(request: Request, db: AsyncSession = Depends(get_db)):
    await _verify_slack_signature(request)

    form = await request.form()
    user_id = form.get("user_id", "")

    member = (
        await db.execute(select(Member).where(Member.slack_user_id == user_id))
    ).scalars().first()
    if not member:
        return JSONResponse({
            "response_type": "ephemeral",
            "text": "❌ Your Slack account isn't linked to a roster record yet. Ask an admin.",
        })

    balance = await wallet_service.balance_for(db, member.id)
    text = (
        f"💰 You have *{balance} {settings.currency_name}*.\n"
        f"<{settings.base_url}/enter?member={member.member_code}&next=/store|Visit the store>"
    )
    return JSONResponse({
        "response_type": "ephemeral",
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })
