from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables. No seed data — the store starts empty; admins add items."""
    from app import models  # noqa: F401 — imported for side-effect (table registration)

    # Apply a staged database restore (if any) before the engine touches the file.
    from app.services.backup import apply_pending_restore
    apply_pending_restore()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # No Alembic. Additive column changes run here as an inspect-guarded ALTER
        # (no-op on a fresh schema, which already has the column from create_all()),
        # mirroring the sibling apps.
        await conn.run_sync(_add_store_item_image_column)
        await conn.run_sync(_add_store_item_sizes_column)
        await conn.run_sync(_add_redemption_size_column)


def _add_store_item_image_column(conn) -> None:
    """Add `image_filename` to store_items if not already present."""
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    if "store_items" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("store_items")]
    if "image_filename" not in columns:
        conn.execute(text("ALTER TABLE store_items ADD COLUMN image_filename VARCHAR(64)"))


def _add_store_item_sizes_column(conn) -> None:
    """Add `sizes` to store_items if not already present."""
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    if "store_items" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("store_items")]
    if "sizes" not in columns:
        conn.execute(text("ALTER TABLE store_items ADD COLUMN sizes VARCHAR(200)"))


def _add_redemption_size_column(conn) -> None:
    """Add `size` to redemptions if not already present."""
    from sqlalchemy import inspect, text
    inspector = inspect(conn)
    if "redemptions" not in inspector.get_table_names():
        return
    columns = [c["name"] for c in inspector.get_columns("redemptions")]
    if "size" not in columns:
        conn.execute(text("ALTER TABLE redemptions ADD COLUMN size VARCHAR(40)"))
