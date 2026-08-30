import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Integer, String, Boolean, DateTime, Text,
    ForeignKey, Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MemberKind(str, enum.Enum):
    """Mirrors Legion's `role`. Both kinds are synced and can sign in, but only
    students are ever granted a balance (services/wallet.grant()) — mentors are staff,
    not reward recipients, in this app."""
    student = "student"
    mentor = "mentor"


class TransactionKind(str, enum.Enum):
    grant = "grant"            # admin/manager awards cash
    purchase = "purchase"      # student redeems a store item (negative delta)
    refund = "refund"          # a cancelled order's cost given back (positive delta)
    adjustment = "adjustment"  # admin manual correction (either sign)


class RedemptionStatus(str, enum.Enum):
    ordered = "ordered"
    fulfilled = "fulfilled"
    cancelled = "cancelled"


class AppSetting(Base):
    """Small key/value store for runtime-configurable app settings."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Member(Base):
    """Unified roster mirror of Legion — students and mentors alike, though only
    students ever hold a balance (see MemberKind). Read-only: synced from Legion by
    `member_code` (see services/legion_sync.py); Merces never writes roster data back."""
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Legion's stable sync key. Nullable only so a legacy row can exist transiently until
    # the first sync back-links it by slack_user_id/name.
    member_code: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[MemberKind] = mapped_column(
        SAEnum(MemberKind), nullable=False, default=MemberKind.student
    )
    team_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slack_user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Legion group slugs this member holds, comma-joined (e.g. "merces-admin"). Not
    # currently used for authorization decisions (those read the live mw_sso cookie
    # instead), but kept for parity with the sibling apps and future use.
    group_slugs: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction", back_populates="member"
    )

    def has_group(self, slug: str) -> bool:
        if not self.group_slugs:
            return False
        return slug in {s.strip() for s in self.group_slugs.split(",") if s.strip()}


class StoreItem(Base):
    """A rewards-store catalog entry (team merch), admin/manager-editable."""
    __tablename__ = "store_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    # Remaining units. Null = unlimited (e.g. a digital perk with no physical stock).
    stock: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    emoji: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Filename only (not a path) of an uploaded photo under settings.upload_dir, served at
    # /uploads/<filename> — see services/uploads.py. Null = fall back to `emoji` in the UI.
    image_filename: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Comma-separated size options (e.g. "S,M,L,XL") for items like clothing that need one
    # picked at redemption time — see services/store.parse_sizes(). Null/blank = no size
    # selection needed (most items: stickers, buttons, digital perks, ...).
    sizes: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    redemptions: Mapped[List["Redemption"]] = relationship("Redemption", back_populates="item")


class Redemption(Base):
    """A student's order for a store item — created instantly on redeem (self-serve, no
    approval gate), then tracked through physical pickup so a mentor can confirm the merch
    actually changed hands. `item_name`/`cost` are snapshots taken at redemption time so a
    later catalog edit (or deletion) doesn't rewrite history."""
    __tablename__ = "redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(Integer, ForeignKey("members.id"), nullable=False)
    store_item_id: Mapped[int] = mapped_column(Integer, ForeignKey("store_items.id"), nullable=False)
    item_name: Mapped[str] = mapped_column(String(200), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    # Snapshotted like item_name/cost above, for the same reason: a later edit to the
    # item's `sizes` list shouldn't rewrite a past order. Null when the item had no
    # sizes configured at redemption time.
    size: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    status: Mapped[RedemptionStatus] = mapped_column(
        SAEnum(RedemptionStatus), nullable=False, default=RedemptionStatus.ordered
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)  # SSO username

    member: Mapped["Member"] = relationship("Member")
    item: Mapped["StoreItem"] = relationship("StoreItem", back_populates="redemptions")
    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction", back_populates="redemption"
    )


class Transaction(Base):
    """One append-only ledger entry. A member's balance is never stored — it's always the
    SUM of their transactions (see services/wallet.balance_for) — so it can't drift out of
    sync with this history."""
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(Integer, ForeignKey("members.id"), nullable=False, index=True)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)  # signed
    kind: Mapped[TransactionKind] = mapped_column(SAEnum(TransactionKind), nullable=False)
    # SSO username of who granted/adjusted it, or "self" for a student's own purchase.
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    # Links a purchase/refund back to its order. Null for a grant or a bare adjustment.
    redemption_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("redemptions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    member: Mapped["Member"] = relationship("Member", back_populates="transactions")
    redemption: Mapped[Optional["Redemption"]] = relationship(
        "Redemption", back_populates="transactions"
    )


class AuditLog(Base):
    """Append-only record of admin/manager mutations."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    actor: Mapped[str] = mapped_column(String(80), nullable=False, default="admin")
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "transaction.grant"
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
