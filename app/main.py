import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app.config import settings
from app.database import init_db
from app.routers import portal, admin, slack
from app.services.scheduler import create_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler = create_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown()


app = FastAPI(title="Merces", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

# StaticFiles requires the directory to exist at mount time, which runs before lifespan —
# create it here rather than relying on init_db()'s makedirs.
os.makedirs(settings.upload_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

app.include_router(portal.router)
app.include_router(admin.router)
app.include_router(slack.router)

# Preview deploys only — see routers/dev_login.py. Unset in production ⇒ never mounted.
if settings.dev_login_secret:
    from app.routers import dev_login
    app.include_router(dev_login.router)
    print("⚠️  Merces /dev-login is ENABLED — this must not be a production config.")
