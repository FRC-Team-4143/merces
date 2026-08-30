from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": tolerate leftover keys in a deployed .env instead of failing to boot.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    # Where a redemption ping goes so staff know to hand over merch (channel ID, e.g.
    # C0ABCDE123). Blank = purchase pings are skipped. The bot must be a member of it.
    slack_announce_channel: str = ""

    # Legion SSO — /admin and the portal (/me, /store, /orders) are gated by the shared
    # `mw_sso` cookie. Merces only *verifies* the cookie (Legion mints it); `sso_secret`
    # must equal Legion's SSO_SECRET. There is no local admin password — the first admin is
    # granted `merces-admin` in Legion's /admin/groups.
    sso_secret: str = ""
    sso_session_ttl: int = 43200  # 12h; must match Legion's cookie max_age
    sso_cookie_domain: str = ""   # e.g. ".marswars.org" so one login spans subdomains

    # Legion roster API + one-tap SSO challenge — the read-only source of truth Merces
    # mirrors people from, and the server-to-server trigger for the one-tap sign-in link.
    legion_base_url: str = ""     # e.g. "https://legion.marswars.org"
    legion_api_key: str = ""      # presented as X-API-Key to Legion's /api/* and /sso/challenge

    database_url: str = "sqlite+aiosqlite:///./merces.db"

    timezone: str = "America/New_York"

    # Public base URL used when Slack messages link back to Merces.
    base_url: str = "http://localhost:8004"

    # Display label for the virtual currency awarded to students (purely cosmetic —
    # doesn't affect storage, which is always a plain signed integer).
    currency_name: str = "MARS Moola"

    # Store item photo uploads. Deliberately under "data/" (not "static/") — static/ is
    # baked into the Docker image at build time and would lose anything written there on
    # the next deploy, while "data/" is the directory the compose volume actually mounts
    # (see apps-infra/docker-compose.yml: `merces-data:/app/data`), so uploads persist.
    upload_dir: str = "data/uploads"
    max_upload_mb: int = 5

    # Database backups (SQLite only)
    backup_dir: str = "backups"
    backup_keep: int = 14  # number of snapshots to retain
    backup_time: str = "23:30"  # HH:MM 24h local time for the weekly snapshot
    backup_day: str = "sun"  # day of week for the weekly backup (mon-sun)

    # Global toggle for all automated updates (Slack messages, scheduled jobs)
    updates_enabled: bool = True

    # Dev / preview sign-in shim. When set, mounts `/dev-login` (see routers/dev_login.py),
    # which mints an `mw_sso` cookie for THIS host — needed only on a preview deploy where
    # Legion's real cookie (scoped to .marswars.org) can't reach. MUST stay unset in
    # production; every /dev-login request has to present this exact value.
    dev_login_secret: str = ""


settings = Settings()
