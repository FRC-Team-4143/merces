"""Auth gating and basic reachability."""
import pytest

from tests.conftest import make_sso_cookie


@pytest.mark.asyncio
async def test_root_redirects_to_me(client):
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/me"


@pytest.mark.asyncio
async def test_me_unauthenticated_shows_identify(client):
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert "Sign in with Legion" in resp.text


@pytest.mark.asyncio
async def test_store_requires_signin(client):
    resp = await client.get("/store", follow_redirects=False)
    assert resp.status_code == 303
    assert "next=" in resp.headers["location"]


@pytest.mark.asyncio
async def test_admin_requires_login(client):
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "sso/authorize" in resp.headers["location"]


@pytest.mark.asyncio
async def test_admin_non_staff_forbidden(client):
    cookie = make_sso_cookie(role="student", groups=[])
    resp = await client.get("/admin", cookies={"mw_sso": cookie})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_dashboard_ok_for_admin(client, admin_cookie):
    resp = await client.get("/admin", cookies={"mw_sso": admin_cookie})
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


@pytest.mark.asyncio
async def test_settings_and_backup_are_admin_only(client, manager_cookie):
    # Manager can see the dashboard and give-cash form...
    assert (await client.get("/admin", cookies={"mw_sso": manager_cookie})).status_code == 200
    assert (await client.get("/admin/give", cookies={"mw_sso": manager_cookie})).status_code == 200
    # ...but not the admin-only Settings/Backup pages.
    assert (await client.get("/admin/settings", cookies={"mw_sso": manager_cookie})).status_code == 403
    assert (await client.get("/admin/backup", cookies={"mw_sso": manager_cookie})).status_code == 403


@pytest.mark.asyncio
async def test_slack_command_requires_signing_secret(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "slack_signing_secret", "")
    resp = await client.post("/slack/command", data={"user_id": "U123"})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_slack_command_rejects_bad_signature(client, monkeypatch):
    # Configured but unsigned — fails closed with 403, not the same as 503-unconfigured.
    from app.config import settings
    monkeypatch.setattr(settings, "slack_signing_secret", "test-secret")
    resp = await client.post("/slack/command", data={"user_id": "U123"})
    assert resp.status_code == 403
