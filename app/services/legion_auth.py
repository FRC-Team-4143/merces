"""
One-tap sign-in — starts a Legion SSO challenge for a member Merces already knows about
(from a Slack payload or a portal link), skipping Legion's username-entry form.

The server-to-server half of the flow; `routers/portal.py`'s `/enter` route is the other
half. See Legion's `routers/sso.py` `POST /sso/challenge` for the full round trip.
"""
import logging
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)


async def start_challenge(member_code: str, *, return_to: str = "/") -> Optional[str]:
    """POST Legion's /sso/challenge for `member_code`. Returns the `/sso/pending/{nonce}`
    URL the browser should be sent to (it fires the Slack Approve/Deny push as a side
    effect), or None if Legion is unreachable/misconfigured/rate-limited."""
    if not settings.legion_base_url or not settings.legion_api_key:
        log.error("Cannot start a Legion SSO challenge: LEGION_BASE_URL/LEGION_API_KEY not set.")
        return None

    headers = {"X-API-Key": settings.legion_api_key}
    try:
        async with httpx.AsyncClient(
            base_url=settings.legion_base_url, headers=headers, timeout=10
        ) as client:
            resp = await client.post(
                "/sso/challenge",
                json={"member_code": member_code, "app": "merces", "return_to": return_to},
            )
            resp.raise_for_status()
            nonce = resp.json()["nonce"]
    except (httpx.HTTPError, KeyError) as e:
        log.error("Legion SSO challenge failed for %s: %s", member_code, e)
        return None

    return f"{settings.legion_base_url}/sso/pending/{nonce}"


def safe_next(path: Optional[str]) -> str:
    """Only allow local, single-slash-rooted redirect targets (no open redirects)."""
    if (
        path
        and path.startswith("/")
        and not path.startswith("//")
        and not path.startswith("/\\")
    ):
        return path
    return "/me"
