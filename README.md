# Merces

**Student rewards app** for FRC teams 4143 (MARS/WARS) and 4423 (MARS' Minions).
Admins and managers award students **MARS Moola** for good behavior and contributions;
students spend their balance in a **rewards store** of real team merch.

- **Ledger** — every point is an append-only `Transaction`; a balance is always the sum of
  a member's transactions, so it can never drift out of sync with the history.
- **Give cash** — admins/managers grant a positive amount to one or more members, with an
  optional reason. The recipient gets a Slack DM.
- **Rewards store** — an admin/manager-curated catalog of merch (name, cost, optional
  stock cap, optional photo — falls back to an emoji when unset). Students redeem
  **instantly, self-serve** — no approval gate — which deducts the balance immediately
  and creates an order.
- **Orders** — every redemption creates a `Redemption` staff fulfills in person. Redeeming
  posts a pickup ping to a Slack channel (and DMs the student a confirmation) so a mentor
  knows to hand over the merch; staff then mark it **picked up** or **cancel & refund**.
- **Ledger view** — every member's balance, drill into their full transaction history, and
  (admin-only) a manual adjustment for corrections.

Sibling to **[Tempus](https://github.com/FRC-Team-4143/tempus)** (attendance),
**[Munus](https://github.com/FRC-Team-4143/munus)** (volunteer hours), and
**[Legion](https://github.com/FRC-Team-4143/legion)** (shared roster + SSO). Same
stack; separate app. FastAPI + async SQLAlchemy + Jinja2 + SQLite, port **8004**.

## Quick start (local)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in Slack + Legion values (see below)
uvicorn app.main:app --reload --port 8004
```

- Members: <http://localhost:8004/me> (`/` redirects here), plus `/store` and `/orders`
- Admin: <http://localhost:8004/admin>

The SQLite DB is created on first boot. The store starts empty — add items at
`/admin/store`.

## How it works

### Identity — Legion SSO
No local passwords. Merces verifies Legion's shared `mw_sso` cookie locally with the
shared `SSO_SECRET` (set it identical to Legion's). `/admin` needs the `merces-admin`
(full) or `merces-manager` (grant cash, run the store, fulfill orders — but not ledger
adjustments, settings, or backup) group; the portal is open to any active roster member.
Grant the first admin `merces-admin` in Legion's `/admin/groups`.

### Roster
People are mirrored read-only from Legion's `/api/members` (keyed on `member_code`)
hourly and via the **Sync from Legion** button on the Roster page. Merces never writes
roster data back.

### Slack
Uses the shared MARS/WARS Slack app. Outbound DMs need `chat:write` + `im:write`. The
`/merces` slash command shows a member their balance with a one-tap link into the store.
A redemption pings `SLACK_ANNOUNCE_CHANNEL` so staff know to hand over the merch. No
interactive components (buttons/modals) in this build — fulfillment happens on the web
at `/admin/orders`.

## Configuration

See [`.env.example`](.env.example). Essentials:

| Var | Purpose |
|-----|---------|
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | Slack DMs + inbound request verification |
| `SLACK_ANNOUNCE_CHANNEL` | Where a redemption pings staff to fulfill it (optional) |
| `SSO_SECRET` | Must equal Legion's — verifies the `mw_sso` cookie |
| `LEGION_BASE_URL`, `LEGION_API_KEY` | Roster sync + one-tap sign-in (Legion's `MERCES_API_KEY`) |
| `BASE_URL` | Merces's public URL, used in Slack links |
| `CURRENCY_NAME` | Display label for the virtual currency (default "MARS Moola") |
| `UPLOAD_DIR`, `MAX_UPLOAD_MB` | Where store item photos are saved, and the size cap (default 5 MB) |
| `UPDATES_ENABLED` | Master switch for all automated Slack messages |

On the Legion side, register this consumer once: set `MERCES_API_KEY`, seed the
`merces-admin`/`merces-manager` groups, and add `merces.marswars.org` to
`SSO_ALLOWED_RETURN_HOSTS`.

## Testing

```bash
pytest
```

In-memory SQLite, real queries (no DB mocking). Tests cover auth gating, the ledger
(grants/purchases/refunds/adjustments never letting a balance drift), store CRUD, the
self-serve redeem flow and its Slack pickup ping, order fulfillment/cancellation, and
roster sync.

## Deployment

Deployed with the sibling apps from the `apps-infra` repo (Docker Compose + Nginx Proxy
Manager). Pushing to `main` runs the Tests workflow, which on success triggers the Deploy
workflow (SSH → `apps-infra/deploy.sh`). See `apps-infra/README.md` → "Adding Merces".
