# Merces — Codebase Guide

**Student rewards app** for FRC teams 4143 (MARS/WARS) and 4423 (MARS' Minions).
Admins/managers grant students a virtual currency ("MARS Moola" by default) for good
behavior and contributions; students spend their balance in an admin-curated **rewards
store** of team merch. FastAPI + SQLAlchemy (async) + Jinja2 + SQLite.

Sibling to **Tempus** (attendance), **Munus** (volunteer hours), and **Legion** (shared
roster + SSO). Intentionally mirrors their stack,
dark theme, and conventions, but is a fully separate app with its own DB, Slack config,
and Docker service (**port 8004**). Nothing is imported across the projects —
integration with Legion is over HTTP only.

## Running

```bash
source venv/bin/activate
uvicorn app.main:app --reload --port 8004
```

Requires a `.env` (see `.env.example`). Key vars: `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`, `BASE_URL`, and the Legion integration — `SSO_SECRET` (must
equal Legion's), `LEGION_BASE_URL`, `LEGION_API_KEY`. There is **no** admin password;
`/admin` is gated by Legion SSO + the `merces-admin` (full) or `merces-manager`
(grant/store/orders — no ledger adjustments/settings/backup) group. The portal (`/me`,
`/store`, `/orders`) is open to any active roster member.

## Testing

```bash
pytest
```

In-memory SQLite with async `pytest-asyncio`. **Do not mock the database** — tests hit a
real (in-memory) DB. `tests/conftest.py` provides a `FakeSlack` recorder (no outbound
Slack), `make_sso_cookie()`, `admin_cookie`/`manager_cookie` fixtures, and a `make_member`
factory.

## Project Layout

```
app/
  main.py            # FastAPI app, router wiring, lifespan (init_db + scheduler)
  config.py          # Settings (pydantic-settings, reads .env)
  database.py        # Engine, session, init_db() — no seed data, the store starts empty
  models.py          # ORM models
  utils.py           # Naive-UTC datetime helpers (utc_to_local, now_utc)
  templating.py      # Shared Jinja2 env (filters + auth-aware globals)
  routers/
    portal.py        # /me balance+history; /store browse+redeem; /orders; /enter one-tap
    admin.py         # /admin — give cash, store CRUD, orders, ledger, roster, settings, backup
    slack.py         # /merces slash command (balance + one-tap store link)
  services/
    wallet.py        # Ledger: balance_for(), grant(), adjust(), purchase(), fulfill(), cancel()
    store.py         # StoreItem CRUD helpers (active_items / all_items / get_item)
    uploads.py       # Store item photo save/delete (validation, random filename)
    sso.py           # Verifies Legion's mw_sso cookie (verify-only) + group helpers
    legion_sync.py   # Pulls the roster from Legion's read-only API into the local mirror
    legion_auth.py   # One-tap sign-in: starts a Legion SSO challenge for a known member
    slack_client.py  # AsyncWebClient wrapper (send_dm / post_to_channel / update_message)
    scheduler.py     # APScheduler: hourly Legion sync, weekly backup
    backup.py        # SQLite snapshot backup + staged restore (VACUUM INTO)
    audit.py         # Append-only mutation log
    app_settings.py  # Persisted runtime settings (legion sync watermark)
```

## Domain model (`app/models.py`)

`Member` — unified roster mirror (students **and** mentors, both balance holders), keyed on
Legion `member_code`, with `kind` and `group_slugs` synced from Legion. `StoreItem` —
admin-editable merch catalog entry (`cost`, optional `stock` cap, `is_active`,
`image_filename` — see "Store item photos" below). `Redemption` — one order: `member_id`
+ `store_item_id`, with `item_name`/`cost`
**snapshotted** at redemption time so a later catalog edit doesn't rewrite history;
`status` is `ordered` → `fulfilled` or `cancelled`. `Transaction` — one append-only ledger
row (`delta`, `kind`, `actor`, optional `reason`/`redemption_id`). Plus `AppSetting` and
`AuditLog`.

There is no `Role`/`Event`/`Shift` table and no seed data — the store
starts empty; admins add items at `/admin/store`.

## Key conventions

### The ledger is the source of truth — balances are never stored
`services/wallet.balance_for()` is always `SUM(Transaction.delta)` for a member; there is
no `balance` column anywhere to drift out of sync. Every mutation (grant, purchase,
refund, manual adjustment) is a **new** `Transaction` row — nothing is ever edited or
deleted, mirroring Munus's approved-hours model. A cancelled
order's refund is a fresh `refund`-kind transaction, not a reversal of the original
`purchase` row.

None of `services/wallet.py`'s functions commit — they `flush()` so a caller can read the
just-written state (e.g. the new balance) in the same call, then the *router* commits
alongside its own `audit.record()` call, matching the transaction-boundary convention
used throughout the sibling apps.

### Redemption is instant and self-serve, not approval-gated
This was a deliberate scope call (unlike Munus's hour-submission approval queue): a
student redeeming an item immediately deducts their balance and creates a `Redemption` in
`ordered` status — there is no mentor sign-off step before that happens
(`routers/portal.py:redeem` → `services/wallet.purchase()`). What *does* require a human
is physical fulfillment: redeeming posts a pickup ping to `SLACK_ANNOUNCE_CHANNEL` (plus a
confirmation DM to the student) so a mentor knows merch needs to change hands, and staff
mark the order **fulfilled** (or **cancel & refund**) at `/admin/orders`. Cancelling
writes a `refund` transaction and restocks the item if it still tracks stock.

### Who can grant cash
Granting cash is deliberately narrow: only
`merces-admin`/`merces-manager` (`services/sso.is_staff`, checked in
`routers/admin.py:give_submit`) — never every mentor. Store CRUD and order
fulfillment/cancellation are also manager-allowed (operational). A manual ledger
**adjustment** (`/admin/ledger/<id>/adjust`, arbitrary sign,
can push a balance negative) is **admin-only** — it's a correction tool, not a day-to-day
action, so it's gated tighter than a grant.

### Who can *receive* cash — students only
Both `services/wallet.grant()` and `.adjust()` reject a non-student `Member`
(`MemberKind.mentor`) outright — mentors are staff in this app, not reward recipients, so
there's no scenario where one should hold a balance. Enforced at the service layer (not
just a UI filter) since that's the one place every balance-changing path funnels through.
Every router-level surface also filters to `MemberKind.student` server-side, so a mentor
is never even offered as an option: `/admin/give`'s recipient dropdown, `give_submit`'s
member lookup, `/admin/ledger`'s list query, and `/admin/ledger/{id}` (GET and its
`/adjust` POST both redirect straight back to `/admin/ledger` for a mentor id rather than
rendering/accepting anything) — `wallet.grant()`/`.adjust()`'s own checks are defense in
depth for a hand-crafted request, not the primary gate. The Roster page's mentor tab
drops the Balance column entirely for the same reason (`admin/roster.html`). Both kinds
still sync from Legion and can sign in (mentors need `/admin` access as staff), so this is
purely about balance eligibility, not roster membership or portal access.

### Ledger sorting (`/admin/ledger`)
Sortable by Name or Balance via clickable column headers (`?sort=name|balance&dir=asc|
desc`), mirroring the `sort_th` macro + toggle-on-reclick pattern from Legion's
`/admin/members` (`admin/ledger.html`'s macro is a copy of Legion's, adapted to two
columns). Default is `balance desc` (unset query params) — matches the page's original
fixed sort, now just one of two selectable options rather than the only one. No "Kind"
column: since the list is students-only (see above), a Kind column showing "student" on
every single row would be pure noise.

### Store items snapshot into a Redemption
`Redemption.item_name`/`.cost`/`.size` are copied from the `StoreItem` at purchase time,
not looked up live — so archiving, repricing, resizing, or even deleting a catalog item
afterward never rewrites a past order's history. Stock is decremented on purchase and
incremented back on cancellation (`services/wallet.cancel(..., restock=True)`), skipped
only if the item's `stock` is `None` (unlimited).

### Archiving vs. deleting a store item
Two different operations, mirroring Munus's opportunities archive/purge split:
- **Archive/Restore** (`POST /admin/store/{id}/archive`, `routers/admin.py:store_archive`)
  toggles `StoreItem.is_active` — reversible, one click, no confirmation needed. An
  archived item disappears from the student store *and*, by default, from the admin
  Store list too (`store_service.all_items(db, show_archived=False)` — the page's
  default view), with a **Show/Hide Archived** toggle button to bring it back into view
  (`?show_archived=1`), exactly like Munus's opportunities list. This is deliberately
  the *only* place `is_active` is set — the item edit form has no Active checkbox, so
  there's one unambiguous way to archive/restore instead of two paths that could drift.
- **Delete** (`POST /admin/store/{id}/delete`, confirm-gated in the UI) is permanent and
  only allowed when `services/store.has_redemptions()` is `False` — i.e. the item was
  never ordered. An item with order history must be archived instead: nothing about the
  redemption-snapshot design *requires* keeping the row (see above — `item_name`/`cost`/
  `size` are already copied out), but forcing archive-first keeps one consistent
  "hide, don't erase" story for anything that's ever actually been part of a real order.

### Store item sizes (`StoreItem.sizes`, `Redemption.size`)
Optional per-item size options (e.g. clothing) — a plain comma-separated string on
`StoreItem.sizes` (`"S,M,L,XL"`), parsed on demand by `services/store.parse_sizes()`
rather than a separate table, matching the codebase's preference for simple string
fields over new tables for small fixed lists. Blank/`None` = the item needs no size
(most items: stickers, buttons, digital perks). When sizes *are* configured,
`services/wallet.purchase()` requires a matching one (`InvalidSizeError` otherwise) and
snapshots the chosen value onto `Redemption.size` — same reasoning as `item_name`/`cost`
above. The admin Store form's "Sizes" field and the student store's size `<select>`
both go through `parse_sizes` (exposed to templates as the `size_list` filter in
`templating.py`), so there's one place that owns the comma-splitting/trimming rule.

### Store item photos (`services/uploads.py`)
Optional per-item photo, uploaded from the admin Store page — falls back to `emoji` in
both the admin table and the student-facing store when unset. Saved under
`settings.upload_dir` (default `data/uploads`) with a random filename
(`secrets.token_hex(16)` + the original extension) — **never** the client-supplied name,
which would otherwise be a path-traversal/collision hazard — and served back out via a
plain `StaticFiles` mount at `/uploads` (registered in `main.py`, alongside `/static`).
Validated on upload: extension in `{.png,.jpg,.jpeg,.webp,.gif}`, content-type starts
with `image/`, size under `settings.max_upload_mb` (default 5). No resizing/thumbnailing
— the browser scales it via CSS; add Pillow-based processing later only if oversized
originals become a real problem.

**Why `data/uploads`, not `static/uploads`:** `static/` is `COPY`'d into the Docker image
at build time (see `Dockerfile`) — anything written there at runtime is wiped on the next
deploy. `data/` is what the compose volume actually mounts
(`apps-infra/docker-compose.yml`: `merces-data:/app/data`), so uploads placed under it
survive rebuilds. `main.py` creates the directory (`os.makedirs`) *before* mounting
`StaticFiles`, since that constructor requires the directory to already exist and runs at
import time — before `lifespan`/`init_db()` ever get a chance to.

Editing an item's photo (`routers/admin.py:store_edit`) deletes the old file before saving
the new one; checking "Remove photo" deletes it and clears the column. Items are archived,
never deleted (see below), so there's no item-delete path that needs to sweep an orphaned
file — the only two places a file is ever removed are those two explicit paths.

### Datetimes
All DB datetimes are **naive UTC** (`app/utils.py`): `utc_to_local()` for display,
`now_utc()` for "now". There's no local-timezone form parsing to speak of (no scheduled
events in the domain model), so `utils.py` is much smaller than the siblings'.

### Legion integration (source of truth for the roster)
Legion owns members and groups; Merces is a **read-only consumer** — data flows Legion →
Merces only. `services/sso.py` verifies the `mw_sso` cookie locally with the shared
`SSO_SECRET` (no callback); on a miss, redirect to
`{LEGION_BASE_URL}/sso/authorize?app=merces`. `services/legion_sync.py` mirrors
`/api/members` hourly (and on the **Sync from Legion** button) keyed on `member_code`,
recording the pull watermark under `app_settings["legion_last_synced"]`
(`services/app_settings.py`) — shown on `/admin/roster` as "Last synced". `/me`'s one-tap
`/enter` uses Legion's `POST /sso/challenge`. **Never add roster CRUD or write-back to
Legion.**

Legion has been told about this consumer (`legion` repo, `main`): `MERCES_API_KEY` in the
API-key list, `merces-admin`/`merces-manager` in `DEFAULT_GROUPS`, and
`merces.marswars.org` in `SSO_ALLOWED_RETURN_HOSTS`. No `slack_dispatch.py` route is
needed — this build has no interactive Slack components.

`/admin/roster` (`admin/roster.html`) splits members into **Students**/**Mentors** tabs,
mirroring Tempus's and Munus's roster page — deliberately, since those are the two other
apps with a comparable "mirror Legion's people into tabs" page, and it was flagged as an
inconsistency when Merces's version was a single flat table instead. Each tab drops
columns that don't apply to that kind rather than showing them as blank/dash: mentors
have no Balance column at all (see "Who can *receive* cash" above), only students do. A
**Linked** badge (`Legion` vs `legacy`) reflects whether `member_code` is set — a `legacy`
row is a stale pre-sync placeholder, not a real synced member. A **Manage in Legion** link
(next to **Sync from Legion**) opens Legion's own `/admin` in a new tab, shown only when
`legion_base_url()` is configured.

### Slack
`/merces` (`routers/slack.py`) is the only slash command — an ephemeral balance plus a
one-tap `/enter?next=/store` link. There are **no interactive buttons/modals** (unlike
Munus's hour-logging flow) — fulfillment is a web action
at `/admin/orders`. A future "mark picked up from Slack" button is a natural next step if
that friction turns out to matter in practice, but it wasn't built for the MVP.

### Database migrations
No Alembic. New tables are picked up automatically by `create_all()`; an additive column
on an *existing* table needs an inspect-guarded ALTER, called from `init_db()` after
`create_all()` — see `database._add_store_item_image_column` for the pattern (a no-op on
a fresh schema, which already has the column from `create_all()`), mirroring the
siblings.

## UI conventions
Single dark theme shared with the siblings (`#0a0a0a` bg, `#111111` panels, accent red
`#cc2200`, borders `#2a1a1a`). Admin pages extend `admin/base.html` (Bootstrap 5,
sidebar); the portal extends `portal/base.html` (navbar).

## Scheduled jobs (`scheduler.py`)

| Job | Trigger |
|-----|---------|
| Legion roster sync | hourly, on the hour |
| Database backup | `BACKUP_DAY` at `BACKUP_TIME` (SQLite snapshot, rotates to `BACKUP_KEEP`) |

No pre-shift-style reminder job — Merces has nothing scheduled in the domain model to
remind anyone about.

## Deployment
Deployed alongside the siblings from the `apps-infra` repo (Docker Compose + Nginx Proxy
Manager) on container port **8004**, public URL `merces.marswars.org`. **Not yet wired
up** — this repo needs `git init` + push to `FRC-Team-4143`, the Legion-side changes
above applied, an `apps-infra` service entry added, and `.env` filled in before it can
deploy. See `apps-infra`'s `docker-compose.yml`, `deploy.sh`, and README.
