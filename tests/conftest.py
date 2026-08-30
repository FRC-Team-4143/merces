"""
Test fixtures — a fresh in-memory SQLite database per test (real DB, not mocked), an
httpx client wired to it, an mw_sso cookie minter, and a fake Slack recorder so no
outbound Slack call ever leaves the process.
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Member, MemberKind


# ── Database ───────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


# ── Fake Slack ───────────────────────────────────────────────────────────────────

class FakeSlack:
    """Records every DM / channel post instead of hitting Slack."""
    def __init__(self):
        self.dms: list[dict] = []
        self.channel_posts: list[dict] = []

    async def conversations_open(self, users):
        return {"channel": {"id": f"D{users}"}}

    async def chat_postMessage(self, channel, text, blocks=None):
        record = {"channel": channel, "text": text, "blocks": blocks}
        (self.dms if channel.startswith("D") else self.channel_posts).append(record)
        return {"ts": "1.0"}

    async def chat_update(self, channel, ts, text, blocks=None):
        return {"ts": ts}


@pytest.fixture(autouse=True)
def fake_slack():
    """Install a fake Slack client for every test; updates stay enabled so sends are
    actually attempted (and recorded)."""
    from app.services import slack_client
    fake = FakeSlack()
    slack_client._client = fake
    settings.updates_enabled = True
    yield fake
    slack_client._client = None


# ── HTTP client ────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(session_factory, db):
    async def _get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ── SSO cookie ─────────────────────────────────────────────────────────────────────

def make_sso_cookie(
    *, member_code="m0000001", name="Test Member", role="student",
    groups=None, slack_user_id=None, team_number=4143,
):
    signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")
    return signer.dumps({
        "member_code": member_code,
        "username": name.lower().replace(" ", "."),
        "name": name,
        "role": role,
        "team_number": team_number,
        "groups": groups or [],
        "slack_user_id": slack_user_id,
    })


@pytest.fixture
def admin_cookie():
    return make_sso_cookie(
        member_code="admin001", name="Ada Admin", role="mentor",
        groups=["merces-admin"], slack_user_id="UADMIN",
    )


@pytest.fixture
def manager_cookie():
    return make_sso_cookie(
        member_code="mgr00001", name="Mel Manager", role="mentor",
        groups=["merces-manager"], slack_user_id="UMGR",
    )


# ── Convenience factory ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def make_member(db):
    counter = {"n": 0}

    async def _make(name="Member", *, kind=MemberKind.student, slack_user_id=None,
                    member_code=None, groups=None, team_number=4143):
        counter["n"] += 1
        m = Member(
            member_code=member_code or f"code{counter['n']:04d}",
            name=name, kind=kind, slack_user_id=slack_user_id,
            team_number=team_number,
            group_slugs=",".join(groups) if groups else None,
        )
        db.add(m)
        await db.commit()
        await db.refresh(m)
        return m

    return _make
