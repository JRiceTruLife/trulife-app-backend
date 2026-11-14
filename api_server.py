#!/usr/bin/env python3
"""api_server.py — Tru Life Properties account backend.

Runs on port 8000. Provides:
- Email/password signup, login, logout, session check
- Self-serve forgot-password (emailed 6-digit code) + authenticated change-password
- Per-user saved deals (Deal Analyzer)
- Per-user comps history (Comps Engine)
- Per-user guide purchase records, real purchases via Stripe Checkout + webhook
- Transactional email (password reset codes, purchase receipts) via Resend
- Profile settings
"""
import hmac
import os
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, closing

import bcrypt
import httpx
import jwt
import stripe
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import email_service
import affiliate_tracking
import affiliate_links
import room_planner

# TRULIFE_DB_PATH lets production hosting (e.g. a mounted persistent disk)
# point the database somewhere durable. Falls back to a local file for dev/preview.
DB_PATH = os.environ.get("TRULIFE_DB_PATH", os.path.join(os.path.dirname(__file__), "trulife.db"))

# --- Production-mode switch ---
# Set TRULIFE_ENV=production to enforce that every security-sensitive secret
# below comes from a real env var instead of a dev default. Leave unset (or
# "development") for local/preview work, where safe fallbacks keep things
# running without extra setup.
IS_PRODUCTION = os.environ.get("TRULIFE_ENV", "development").strip().lower() == "production"


def _require_secret(env_name: str, dev_default: str | None = None) -> str:
    """Read a secret from the environment. In production mode, missing or
    still-default secrets raise at startup instead of silently running with
    a known/weak value. In dev mode, fall back so local work stays easy."""
    value = os.environ.get(env_name)
    if IS_PRODUCTION:
        if not value:
            raise RuntimeError(
                f"{env_name} must be set when TRULIFE_ENV=production (no dev default allowed)."
            )
        if dev_default is not None and value == dev_default:
            raise RuntimeError(
                f"{env_name} is still set to the known dev default value. Set a real secret before going live."
            )
        return value
    return value or dev_default


if IS_PRODUCTION and not os.environ.get("TRULIFE_JWT_SECRET"):
    raise RuntimeError(
        "TRULIFE_JWT_SECRET must be set when TRULIFE_ENV=production — an "
        "auto-generated secret would invalidate every session on each restart "
        "and differs across multiple server instances."
    )
JWT_SECRET = os.environ.get("TRULIFE_JWT_SECRET") or secrets.token_hex(32)
JWT_ALG = "HS256"
SESSION_DAYS = 30

# Affiliate portal sessions are short-lived on purpose — real commission money
# is tied to this login, so tokens expire fast and the frontend enforces an
# idle-timeout logout independent of this ceiling (see AFFILIATE_IDLE_MINUTES
# usage in the frontend). The JWT expiry below is just the hard outer bound.
AFFILIATE_SESSION_MINUTES = 30

# Commission products for the affiliate program.
# Flat program commission applied to every in-app sale (see affiliate_tracking.PROGRAM_RATE).
AFFILIATE_COMMISSION_RATE = affiliate_tracking.PROGRAM_RATE

AFFILIATE_PRODUCTS = [
    "Design Guide",
    "Architecture Plan",
    "Architecture Custom",
    "Fix & Flip Module",
    "5 Step Method Module",
    "Coaching",
    "Discord Membership",
]

# Shared secret the Tru Life Discord hub uses to forward paid membership
# invoices (POST /api/affiliate/external-sale). Unset = endpoint disabled.
HUB_SHARED_SECRET = os.environ.get("TRULIFE_HUB_SHARED_SECRET", "").strip()

# Maps a Stripe-sold catalog item (GUIDE_CATALOG id) to the affiliate
# commission product it counts as. Used by the webhook to auto-credit the
# referring affiliate when a referred client buys anything, at any time.
GUIDE_TO_AFFILIATE_PRODUCT = {
    "linder": "Design Guide",
    "forestoak": "Design Guide",
    "canyon": "Design Guide",
    "colonial": "Design Guide",
    "five-step-method": "5 Step Method Module",
    "fix-flip": "Fix & Flip Module",
}

# How long a ?ref=<slug> visit stays eligible to attach a new signup to the
# affiliate. The frontend stores the ref locally and sends it with signup;
# this is the server-side ceiling on that window (days).
AFFILIATE_ATTRIBUTION_DAYS = 90

# The 4 canonical design guides + the 5 Step Method module — shared catalog,
# purchase status is per-user. Same guide_purchases table backs both.
GUIDE_CATALOG = [
    {"id": "linder", "name": "Modern Farmhouse", "price": 497},
    {"id": "forestoak", "name": "Coastal Brass & Blue", "price": 597},
    {"id": "canyon", "name": "Canyon Collection", "price": 697},
    {"id": "colonial", "name": "Colonial", "price": 797},
    {"id": "five-step-method", "name": "5 Step Method", "price": 49},
    # Brick by Brick course (Level 01 Fix & Flip Foundations, with later
    # modules added to the same purchase as Justin releases them).
    {"id": "fix-flip", "name": "Brick by Brick: Fix & Flip Foundations", "price": 499, "kind": "course"},
]

# Catalog items whose paid media is served through the protected /brick
# route below (anything else in /brick — posters, title cards — is public).
BRICK_COURSE_GUIDE_ID = "fix-flip"
MEDIA_TOKEN_SECONDS = 8 * 3600

# --- Admin unlock key: legacy pre-Stripe mechanism, kept only as an owner/
# support fallback (e.g. comping a guide for a customer manually). Real
# purchases go through Stripe Checkout below. ---
# Set via env var. In production this MUST be a real secret — no dev default
# is accepted.
ADMIN_KEY = _require_secret("TRULIFE_ADMIN_KEY", "trulife-admin-2026")

# --- Stripe ---
# TRULIFE_STRIPE_SECRET_KEY / TRULIFE_STRIPE_WEBHOOK_SECRET are set once a
# real Stripe account exists. In production both are required. In dev,
# Stripe stays disabled (checkout endpoint returns 503) until keys are
# provided — no fake/test key is baked in as a default.
if IS_PRODUCTION and not os.environ.get("TRULIFE_STRIPE_SECRET_KEY"):
    raise RuntimeError("TRULIFE_STRIPE_SECRET_KEY must be set when TRULIFE_ENV=production.")
if IS_PRODUCTION and not os.environ.get("TRULIFE_STRIPE_WEBHOOK_SECRET"):
    raise RuntimeError("TRULIFE_STRIPE_WEBHOOK_SECRET must be set when TRULIFE_ENV=production.")
STRIPE_SECRET_KEY = os.environ.get("TRULIFE_STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("TRULIFE_STRIPE_WEBHOOK_SECRET", "")
STRIPE_ENABLED = bool(STRIPE_SECRET_KEY)
if STRIPE_ENABLED:
    stripe.api_key = STRIPE_SECRET_KEY
# Where Stripe should send the shopper back after Checkout. The app is a
# single-page frontend, so both success and cancel land on the same page;
# the frontend reads the query params to resume the right screen/guide.
APP_BASE_URL = os.environ.get("TRULIFE_APP_BASE_URL", "").rstrip("/")

# --- Paywall switch ---
# Controls whether the frontend enforces purchase gates (design guides, 5 Step
# Method, etc.). Off = review/free-access mode where every member sees all
# content. Flip to "true" on Render (no code change) to start charging. Owner
# accounts bypass every gate regardless of this flag.
PAYWALL_ENABLED = os.environ.get("TRULIFE_PAYWALL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

# --- Transactional email (Resend) ---
# TRULIFE_RESEND_API_KEY is required in production so password-reset codes
# and purchase receipts actually go out. In dev, sends are logged and
# skipped rather than raising when the key is unset, so local work isn't
# blocked on having an email provider configured.
if IS_PRODUCTION and not os.environ.get("TRULIFE_RESEND_API_KEY"):
    raise RuntimeError("TRULIFE_RESEND_API_KEY must be set when TRULIFE_ENV=production.")
EMAIL_ENABLED = email_service.EMAIL_ENABLED

# --- Comps Engine data (RentCast) ---
# TRULIFE_RENTCAST_API_KEY powers real sold-comp and rent-estimate lookups.
# Optional in both dev and production: without it, the endpoint below returns
# a 503 and the frontend falls back to its built-in sample comps rather than
# breaking the tool entirely.
RENTCAST_API_KEY = os.environ.get("TRULIFE_RENTCAST_API_KEY", "")
RENTCAST_ENABLED = bool(RENTCAST_API_KEY)
RENTCAST_BASE_URL = "https://api.rentcast.io/v1"


def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


db = get_db()


def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            is_owner INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS account_state (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            preferences TEXT NOT NULL DEFAULT '{}',
            project TEXT
        );
        CREATE TABLE IF NOT EXISTS seed_suppression (
            role TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS saved_deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            deal_type TEXT NOT NULL,
            label TEXT DEFAULT '',
            payload TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS comps_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            search_label TEXT DEFAULT '',
            payload TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS arc_inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT NOT NULL DEFAULT 'inquiry',
            plan TEXT,
            name TEXT,
            email TEXT,
            phone TEXT,
            city TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS guide_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            guide_id TEXT NOT NULL,
            unlocked_by TEXT DEFAULT 'admin',
            stripe_session_id TEXT,
            stripe_payment_intent TEXT,
            amount_paid REAL,
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, guide_id)
        );

        -- Pending Stripe Checkout sessions, created when the user clicks
        -- "Unlock" and resolved (or expired) by the webhook. Lets us map an
        -- incoming webhook event back to the user + guide without trusting
        -- any client-supplied identifiers, and gives idempotency so a
        -- retried webhook delivery can't double-grant or double-count.
        CREATE TABLE IF NOT EXISTS checkout_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stripe_session_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            guide_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Self-serve "forgot password" flow: a short-lived, single-use code
        -- emailed to the account's address. Never store the code itself —
        -- only a hash of it, same principle as password storage, so a DB
        -- read can't be used to reset someone's password.
        CREATE TABLE IF NOT EXISTS password_reset_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code_hash TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS affiliates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            handle TEXT DEFAULT '',
            email TEXT UNIQUE,
            password_hash TEXT,
            default_rate REAL NOT NULL DEFAULT 20,
            product_rates TEXT NOT NULL DEFAULT '{}',
            is_admin INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS affiliate_clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_id INTEGER NOT NULL REFERENCES affiliates(id) ON DELETE CASCADE,
            path TEXT DEFAULT '',
            referrer TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS affiliate_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_id INTEGER NOT NULL REFERENCES affiliates(id) ON DELETE CASCADE,
            product TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT DEFAULT '',
            sale_date TEXT NOT NULL,
            logged_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Room Planner (Floor Plan Measurement Form) submissions. Sent only
        -- to the fixed back-office inbox; photos/sketches are kept on disk
        -- next to the database under room_planner_uploads/<id>/.
        -- Room Planner access granted by owner/staff to a client who paid
        -- for architecture drawings outside the app. Revoking keeps the row.
        CREATE TABLE IF NOT EXISTS room_planner_grants (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            granted_by TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            revoked_by TEXT
        );

        CREATE TABLE IF NOT EXISTS room_plan_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            reference TEXT UNIQUE NOT NULL,
            client_ref TEXT,
            payload TEXT NOT NULL,
            photo_files TEXT NOT NULL DEFAULT '[]',
            sketch_files TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'received',
            email_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, client_ref)
        );

        CREATE TABLE IF NOT EXISTS affiliate_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_id INTEGER NOT NULL REFERENCES affiliates(id) ON DELETE CASCADE,
            amount REAL NOT NULL,
            note TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    db.commit()
    # Migration: lifetime affiliate attribution on the user record. A user
    # is attached to the affiliate whose link they signed up through, and
    # every later purchase credits that affiliate (residual income).
    user_cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    for col, ddl in [
        ("referred_by_affiliate_id", "ALTER TABLE users ADD COLUMN referred_by_affiliate_id INTEGER"),
        ("referred_at", "ALTER TABLE users ADD COLUMN referred_at TIMESTAMP"),
    ]:
        if col not in user_cols:
            db.execute(ddl)
            db.commit()
    # Migration: affiliate_sales gains a link to the buying client, a source
    # marker (manual vs. automatic Stripe credit) and the Stripe session id
    # for idempotency so a retried webhook can never double-credit.
    sale_cols = {r["name"] for r in db.execute("PRAGMA table_info(affiliate_sales)").fetchall()}
    for col, ddl in [
        ("user_id", "ALTER TABLE affiliate_sales ADD COLUMN user_id INTEGER"),
        ("source", "ALTER TABLE affiliate_sales ADD COLUMN source TEXT DEFAULT 'manual'"),
        ("stripe_session_id", "ALTER TABLE affiliate_sales ADD COLUMN stripe_session_id TEXT"),
    ]:
        if col not in sale_cols:
            db.execute(ddl)
            db.commit()
    # Migration for DBs created before is_owner existed.
    cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    if "is_owner" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN is_owner INTEGER DEFAULT 0")
        db.commit()
    # Migration for guide_purchases created before Stripe columns existed.
    gp_cols = {r["name"] for r in db.execute("PRAGMA table_info(guide_purchases)").fetchall()}
    for col, ddl in [
        ("stripe_session_id", "ALTER TABLE guide_purchases ADD COLUMN stripe_session_id TEXT"),
        ("stripe_payment_intent", "ALTER TABLE guide_purchases ADD COLUMN stripe_payment_intent TEXT"),
        ("amount_paid", "ALTER TABLE guide_purchases ADD COLUMN amount_paid REAL"),
    ]:
        if col not in gp_cols:
            db.execute(ddl)
            db.commit()
    affiliate_tracking.migrate(db)
    affiliate_links.migrate(db)


# --- Owner account: seeded/promoted on every startup. ---
# Set via env vars so the password isn't hardcoded in source; falls back to a
# dev default for local/preview testing. Rotate OWNER_PASSWORD before real launch.
OWNER_EMAIL = os.environ.get("TRULIFE_OWNER_EMAIL", "justin@trulifeproperties.com").strip().lower()

# Staff accounts (content/media team) get the same full-access rights as the
# owner account: every course, guide, and module is unlocked regardless of
# purchases. Comma-separated emails in TRULIFE_STAFF_EMAILS. Evaluated at
# request time, so adding an email takes effect on the next login without
# a database migration.
STAFF_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get("TRULIFE_STAFF_EMAILS", "").split(",")
    if e.strip()
)


def account_is_owner(row) -> bool:
    """True for the owner account and any configured staff account."""
    if not row:
        return False
    if row["is_owner"]:
        return True
    return (row["email"] or "").strip().lower() in STAFF_EMAILS


OWNER_PASSWORD = _require_secret("TRULIFE_OWNER_PASSWORD", "TruLife2026!")
OWNER_NAME = os.environ.get("TRULIFE_OWNER_NAME", "Justin Rice")


def seed_owner_account():
    if db.execute("SELECT 1 FROM seed_suppression WHERE role='owner'").fetchone():
        return
    existing = db.execute("SELECT * FROM users WHERE email = ?", [OWNER_EMAIL]).fetchone()
    pw_hash = bcrypt.hashpw(OWNER_PASSWORD.encode(), bcrypt.gensalt()).decode()
    if existing:
        # Ensure the owner flag is set even if the account was created earlier
        # as a normal signup with this email.
        db.execute("UPDATE users SET is_owner = 1, name = ? WHERE id = ?", [OWNER_NAME, existing["id"]])
        db.commit()
    else:
        db.execute(
            "INSERT INTO users (email, password_hash, name, is_owner) VALUES (?, ?, ?, 1)",
            [OWNER_EMAIL, pw_hash, OWNER_NAME],
        )
        db.commit()


# --- Default affiliates: seeded on every startup if the table is empty. ---
# Real affiliate accounts/passwords are set by the owner from the Affiliates
# tab once live; these are starting records with placeholder credentials so
# the program has data on day one.
DEFAULT_AFFILIATES = [
    {"slug": "justice", "name": "Justice", "handle": "@justice", "default_rate": 20, "product_rates": {}},
    {"slug": "greylo", "name": "Greylo", "handle": "@greylo", "default_rate": 20, "product_rates": {}},
    {"slug": "erik", "name": "Erik All", "handle": "@erikall", "default_rate": 20, "product_rates": {}},
    {"slug": "damaine", "name": "Damaine Wilson", "handle": "@damainewilson", "default_rate": 20, "product_rates": {}},
    {"slug": "tayvon", "name": "Tayvon", "handle": "@tayvon", "default_rate": 20, "product_rates": {}},
]

AFFILIATE_ADMIN_EMAIL = os.environ.get("TRULIFE_AFFILIATE_ADMIN_EMAIL", "justin@trulifeproperties.com").strip().lower()

# Internal accounting notification: every completed purchase also sends a
# copy of the receipt here (in addition to the buyer's own receipt), so
# there's a record of transactions for bookkeeping without depending on any
# one person's personal inbox.
ACCOUNTING_EMAIL = os.environ.get("TRULIFE_ACCOUNTING_EMAIL", "finances@trulifefiles.com").strip().lower()
# Where architecture reservations / plan inquiries are forwarded.
INQUIRY_EMAIL = os.environ.get("TRULIFE_INQUIRY_EMAIL", "info@trulifeproperties.com").strip().lower() or "info@trulifeproperties.com"
if not ACCOUNTING_EMAIL or ACCOUNTING_EMAIL == "info@trulifeproperties.com":
    ACCOUNTING_EMAIL = "finances@trulifefiles.com"
AFFILIATE_ADMIN_PASSWORD = _require_secret("TRULIFE_AFFILIATE_ADMIN_PASSWORD", "TruLife2026!")


def seed_affiliates():
    import json as _json
    count = db.execute("SELECT COUNT(*) AS c FROM affiliates").fetchone()["c"]
    if count == 0:
        for a in DEFAULT_AFFILIATES:
            db.execute(
                "INSERT INTO affiliates (slug, name, handle, default_rate, product_rates) VALUES (?, ?, ?, ?, ?)",
                [a["slug"], a["name"], a["handle"], a["default_rate"], _json.dumps(a["product_rates"])],
            )
        db.commit()
    # Flat program commission: normalize any legacy per-affiliate rates so the
    # stored values match what is actually paid.
    db.execute(
        "UPDATE affiliates SET default_rate = ?, product_rates = '{}' WHERE default_rate != ? OR product_rates != '{}'",
        [AFFILIATE_COMMISSION_RATE, AFFILIATE_COMMISSION_RATE],
    )
    db.commit()
    # Ensure the owner/admin has an affiliate-portal login too (full dashboard access).
    if db.execute("SELECT 1 FROM seed_suppression WHERE role='affiliate_admin'").fetchone():
        return
    existing = db.execute("SELECT id FROM affiliates WHERE email = ?", [AFFILIATE_ADMIN_EMAIL]).fetchone()
    pw_hash = bcrypt.hashpw(AFFILIATE_ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
    if existing:
        db.execute("UPDATE affiliates SET password_hash = ?, is_admin = 1 WHERE id = ?", [pw_hash, existing["id"]])
    else:
        db.execute(
            "INSERT INTO affiliates (slug, name, handle, email, password_hash, default_rate, is_admin) "
            "VALUES ('admin', 'Justin Rice', '@owner', ?, ?, 0, 1)",
            [AFFILIATE_ADMIN_EMAIL, pw_hash],
        )
    db.commit()


init_db()
seed_owner_account()
seed_affiliates()


@asynccontextmanager
async def lifespan(app):
    yield
    db.close()


app = FastAPI(lifespan=lifespan)

# --- CORS ---
# TRULIFE_ALLOWED_ORIGINS is a comma-separated allowlist, e.g.
# "https://trulifeproperties.com,https://www.trulifeproperties.com".
# In production this MUST be set to the real site origin(s) — wildcard "*"
# is refused. In dev/preview (including the sandboxed iframe preview used
# during development, which has no fixed origin) wildcard stays allowed so
# testing isn't blocked.
_allowed_origins_env = os.environ.get("TRULIFE_ALLOWED_ORIGINS", "").strip()
if IS_PRODUCTION:
    if not _allowed_origins_env or _allowed_origins_env == "*":
        raise RuntimeError(
            "TRULIFE_ALLOWED_ORIGINS must be set to a comma-separated list of "
            "real site origins when TRULIFE_ENV=production — wildcard CORS is not allowed."
        )
    _cors_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
else:
    _cors_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- Serve the site itself from this same service/domain ---
# Lets a single Render web service (and a single custom domain) host both
# the API and the app, instead of splitting the frontend onto a separate
# static host. Static assets (images, PDFs, videos under /design, logos,
# etc.) are served at their existing relative paths; "/" and any
# non-/api/* path serve index.html so client-side navigation still works.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/design", StaticFiles(directory=os.path.join(_STATIC_DIR, "design")), name="design-assets")
    if os.path.isdir(os.path.join(_STATIC_DIR, "assets")):
        app.mount("/assets", StaticFiles(directory=os.path.join(_STATIC_DIR, "assets")), name="assets")
    # Brick by Brick course media. Posters/title cards are public; module
    # videos (.mp4) are paid content. When the paywall is on, a video is
    # served only with a short-lived media token issued to a signed-in
    # account that owns the course (or the owner account). FileResponse
    # honors HTTP Range requests so the <video> player can seek.
    _BRICK_DIR = os.path.realpath(os.path.join(_STATIC_DIR, "brick"))

    @app.get("/brick/{path:path}", include_in_schema=False)
    def brick_media(path: str, t: str | None = None):
        full = os.path.realpath(os.path.join(_BRICK_DIR, path))
        if not full.startswith(_BRICK_DIR + os.sep) or not os.path.isfile(full):
            raise HTTPException(404, "Not found")
        if full.lower().endswith(".mp4") and PAYWALL_ENABLED:
            if not t:
                raise HTTPException(403, "Course access required")
            try:
                payload = jwt.decode(t, JWT_SECRET, algorithms=[JWT_ALG])
            except jwt.PyJWTError:
                raise HTTPException(403, "Media link expired, reload the course")
            if payload.get("media") != BRICK_COURSE_GUIDE_ID or not isinstance(payload.get("uid"), int):
                raise HTTPException(403, "Course access required")
        return FileResponse(full)

    _INDEX_HTML_PATH = os.path.join(_STATIC_DIR, "index.html")

    @app.get("/{fname}", include_in_schema=False)
    async def _static_root_file(fname: str):
        """Serves top-level static files (logo.svg, icon.svg, hero.png, ...)
        and falls back to index.html for app routes (e.g. /dashboard) so
        client-side navigation and hard refreshes both work. /api/* is
        never reached here since FastAPI matches those routes first."""
        candidate = os.path.join(_STATIC_DIR, fname)
        if os.path.isfile(candidate) and os.path.commonpath([_STATIC_DIR, candidate]) == _STATIC_DIR:
            return FileResponse(candidate)
        return FileResponse(_INDEX_HTML_PATH)

    @app.get("/", include_in_schema=False)
    async def _static_index():
        return FileResponse(_INDEX_HTML_PATH)

# --- Request body size cap ---
# Field-level Field(max_length=...) constraints cover plain string fields,
# but a few schemas accept an open-ended `payload: dict` (deal snapshots,
# comps search state). Without a ceiling, a client could send an oversized
# JSON body to exhaust memory. Reject anything over 256KB up front, before
# it reaches request parsing/validation.
_MAX_BODY_BYTES = 256 * 1024


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    limit = room_planner.MAX_SUBMIT_BODY_BYTES if request.url.path == "/api/room-planner/submit" else _MAX_BODY_BYTES
    if content_length and content_length.isdigit() and int(content_length) > limit:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    return await call_next(request)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------- Rate limiting ----------
# Simple in-memory sliding-window limiter. Good enough for a single-process
# deployment like this one; keyed by (bucket, client_ip) so one noisy client
# can't lock out everyone else. Swap for a shared store (e.g. Redis) only if
# this backend is ever scaled to multiple processes/instances.
_rate_buckets: dict[tuple[str, str], deque] = defaultdict(deque)
_rate_lock = threading.Lock()

RATE_LIMITS = {
    # bucket: (max_requests, window_seconds)
    "auth_login": (10, 60),
    "auth_signup": (5, 60),
    "auth_reset": (5, 60),
    "auth_delete": (5, 300),
    "affiliate_login": (10, 60),
    # Forgot-password: tighter limits since these are unauthenticated and
    # trigger an email send / accept a guessable-length code.
    "auth_forgot_password": (3, 300),
    "auth_forgot_password_verify": (8, 300),
    # RentCast API usage is metered on our plan — cap live comps lookups
    # per-IP so one user (or a bot) can't burn the monthly request quota.
    "comps_lookup": (12, 3600),
    # Room Planner submissions carry photos and send an email each time.
    "room_planner_submit": (6, 3600),
    # Wrong shared-secret attempts against the hub bridge.
    "hub_external_sale": (5, 300),
}


def enforce_rate_limit(bucket: str, request: Request):
    limit, window = RATE_LIMITS[bucket]
    client_ip = request.client.host if request.client else "unknown"
    key = (bucket, client_ip)
    now = time.monotonic()
    with _rate_lock:
        q = _rate_buckets[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Too many requests — please wait a moment and try again")
        q.append(now)


# ---------- Auth helpers ----------

def make_token(user_id: int) -> str:
    payload = {"uid": user_id, "exp": int(time.time()) + SESSION_DAYS * 86400}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def current_user(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired, please log in again")
    if not isinstance(payload.get("uid"), int) or "aff" in payload:
        raise HTTPException(401, "Invalid account session")
    row = db.execute("SELECT * FROM users WHERE id = ?", [payload["uid"]]).fetchone()
    if not row:
        raise HTTPException(401, "Account not found")
    return row


def user_public(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "phone": row["phone"],
        "is_owner": account_is_owner(row),
    }


# ---------- Affiliate auth helpers ----------
# Separate JWT namespace ("aff" claim) from the main app's "uid" claim so an
# investor-app token can never be replayed against affiliate endpoints and
# vice versa. Short expiry by design — real commission money is tied to this
# login. The frontend additionally enforces an idle-timeout logout well
# inside this window; this is just the hard outer bound.


def make_affiliate_token(affiliate_id: int) -> str:
    payload = {"aff": affiliate_id, "exp": int(time.time()) + AFFILIATE_SESSION_MINUTES * 60}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def current_affiliate(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired, please log in again")
    if "aff" not in payload:
        raise HTTPException(401, "Not authenticated")
    row = db.execute("SELECT * FROM affiliates WHERE id = ? AND active = 1", [payload["aff"]]).fetchone()
    if not row:
        raise HTTPException(401, "Account not found")
    return row


def affiliate_public(row) -> dict:
    import json as _json
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "handle": row["handle"],
        "email": row["email"],
        # Program rate for everyone unless admin set a per-affiliate override.
        "default_rate": affiliate_tracking.effective_rate(row),
        "rate_override": _row_get(row, "rate_override"),
        "discord_invite_code": _row_get(row, "discord_invite_code") or "",
        "discord_user_id": _row_get(row, "discord_user_id") or "",
        "source": _row_get(row, "source") or "",
        "hub_synced_at": _row_get(row, "hub_synced_at") or "",
        "has_login": bool(row["password_hash"]),
        "product_rates": {},
        "program_rate": AFFILIATE_COMMISSION_RATE,
        "is_admin": bool(row["is_admin"]),
    }


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def affiliate_rate(row=None, product: str | None = None) -> float:
    """Program rate (20%) on every in-app item, or the affiliate's override."""
    return affiliate_tracking.effective_rate(row)


def mask_email(email: str | None) -> str:
    """j***@gmail.com — enough for an affiliate to recognise their own
    referral without handing them the client's full contact details."""
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    return (local[:1] or "*") + "***@" + domain


def affiliate_by_slug(slug: str | None):
    if not slug:
        return None
    slug = slug.strip().lower()[:100]
    if not slug:
        return None
    return db.execute("SELECT * FROM affiliates WHERE slug = ? AND active = 1", [slug]).fetchone()


# ---------- Schemas ----------

class SignupBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    name: str = Field(default="", max_length=200)
    # Affiliate slug captured from a ?ref=<slug> visit (stored client-side
    # until signup). Optional; unknown/inactive slugs are ignored silently.
    ref: str = Field(default="", max_length=100)


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class ProfileBody(BaseModel):
    name: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=40)


class DeleteAccountBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    confirmation: str


class PreferencesBody(BaseModel):
    market: str = Field(default="", max_length=200)
    min_coc: float = Field(default=15, ge=0, le=1000)
    min_profit: float = Field(default=30000, ge=0, le=1e10)
    rate: float = Field(default=9.5, ge=0, le=100)
    refi_ltv: float = Field(default=75, ge=0, le=100)
    mao: float = Field(default=0.7, ge=0, le=1)


class ProjectBody(BaseModel):
    project: dict


class ResetPasswordBody(BaseModel):
    """Authenticated change-password: requires the CURRENT password, not just
    the account email. The old email-only reset endpoint let anyone who knew
    a user's email address take over that account with no verification —
    this is the interim fix until real emailed reset codes ship alongside
    the email-provider integration."""
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class ForgotPasswordBody(BaseModel):
    """Step 1 of the logged-out reset flow: request a code be emailed."""
    email: str = Field(max_length=254)


class ForgotPasswordVerifyBody(BaseModel):
    """Step 2: submit the emailed code + a new password to complete the reset."""
    email: str = Field(max_length=254)
    code: str = Field(max_length=12)
    new_password: str = Field(max_length=200)


class DealBody(BaseModel):
    deal_type: str = Field(max_length=40)
    label: str = Field(default="", max_length=300)
    payload: dict = Field(default_factory=dict)


class CompsBody(BaseModel):
    search_label: str = Field(default="", max_length=300)
    payload: dict = Field(default_factory=dict)


class CompsSearchBody(BaseModel):
    address: str = Field(max_length=300)
    beds: float = Field(default=3, ge=0, le=20)
    baths: float = Field(default=2, ge=0, le=20)
    sqft: float = Field(default=1500, ge=100, le=50000)
    property_type: str = Field(default="Single Family", max_length=50)


class UnlockBody(BaseModel):
    guide_id: str = Field(max_length=100)
    admin_key: str = Field(max_length=200)
    # Optional: email of the member to comp. Defaults to the calling account.
    email: str | None = Field(default=None, max_length=200)


class CheckoutBody(BaseModel):
    guide_id: str = Field(max_length=100)


class AffiliateLoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class AffiliateRatesBody(BaseModel):
    default_rate: float | None = Field(default=None, ge=0, le=100)
    product_rates: dict | None = None


class AffiliateDiscordBody(BaseModel):
    invite_code: str = Field(default="", max_length=200)


class AffiliateLinkCreateBody(BaseModel):
    label: str = Field(max_length=80)
    product: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)
    code: str = Field(default="", max_length=60)
    affiliate_id: int | None = None  # admin only: create on behalf of an affiliate


class AffiliateLinkUpdateBody(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    active: bool | None = None


class ExternalSaleBody(BaseModel):
    provider: str = Field(max_length=40)
    transaction_id: str = Field(max_length=200)
    email: str = Field(default="", max_length=254)
    amount_cents: int = Field(ge=0)
    product: str = Field(default="Discord Membership", max_length=80)
    description: str = Field(default="", max_length=200)
    hub_affiliate_code: str = Field(default="", max_length=100)
    member_label: str = Field(default="", max_length=120)
    occurred_at: str = Field(default="", max_length=40)
    # Who owns the invite (so the app can create the affiliate itself if
    # nobody has claimed the code yet).
    hub_affiliate_name: str = Field(default="", max_length=80)
    hub_affiliate_discord_id: str = Field(default="", max_length=40)
    hub_affiliate_email: str = Field(default="", max_length=254)


class HubRosterEntry(BaseModel):
    discord_user_id: str = Field(max_length=40)
    name: str = Field(default="", max_length=80)
    invite_code: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=254)
    active: bool = True


class HubRosterBody(BaseModel):
    affiliates: list[HubRosterEntry] = Field(default_factory=list, max_length=500)


class AffiliateMergeBody(BaseModel):
    into_id: int


class AffiliateCredentialsBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class NewAffiliateBody(BaseModel):
    name: str = Field(max_length=200)
    handle: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=200)
    default_rate: float = Field(default=20, ge=0, le=100)  # accepted for compatibility; program rate applies


class SaleBody(BaseModel):
    affiliate_id: int
    product: str = Field(max_length=100)
    amount: float = Field(gt=0, le=1_000_000)
    note: str = Field(default="", max_length=500)
    sale_date: str = Field(default="", max_length=40)


class PayoutBody(BaseModel):
    affiliate_id: int
    amount: float = Field(gt=0, le=1_000_000)
    note: str = Field(default="", max_length=500)


class ClickBody(BaseModel):
    ref: str = Field(max_length=100)
    path: str = Field(default="", max_length=500)
    referrer: str = Field(default="", max_length=500)


class AssignClientBody(BaseModel):
    email: str = Field(max_length=254)
    affiliate_id: int | None = None


# ---------- Auth routes ----------

@app.get("/api/health")
def health():
    with closing(get_db()) as conn:
        conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    return {"status": "ok"}

@app.get("/api/config")
def public_config():
    """Non-secret runtime flags the frontend reads at boot."""
    return {
        "paywall_enabled": PAYWALL_ENABLED,
        "payments_ready": STRIPE_ENABLED and bool(STRIPE_WEBHOOK_SECRET),
    }

@app.post("/api/auth/signup", status_code=201)
def signup(body: SignupBody, request: Request):
    enforce_rate_limit("auth_signup", request)
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    existing = db.execute("SELECT id FROM users WHERE email = ?", [email]).fetchone()
    if existing:
        raise HTTPException(409, "An account with this email already exists")
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    # Lifetime affiliate attribution happens exactly once, at signup. The
    # slug comes from the ?ref= link the visitor arrived on; the frontend
    # keeps it for AFFILIATE_ATTRIBUTION_DAYS and sends it here.
    ref_aff, ref_link = affiliate_links.resolve_ref(db, body.ref)
    if body.ref.strip() and not ref_aff:
        raise HTTPException(400, "Invite code is invalid or inactive. Correct it or clear it to continue without a referral.")
    if ref_aff and (ref_aff["email"] or "").lower() == email:
        ref_aff, ref_link = None, None
    try:
        with closing(get_db()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO users (email,password_hash,name,referred_by_affiliate_id,referred_link_id,referred_at) "
                "VALUES (?,?,?,?,?,CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE NULL END)",
                [email, pw_hash, body.name.strip(), ref_aff["id"] if ref_aff else None,
                 ref_link["id"] if ref_link else None, ref_aff["id"] if ref_aff else None],
            )
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account with this email already exists")
    row = db.execute("SELECT * FROM users WHERE id = ?", [user_id]).fetchone()
    return {"token": make_token(user_id), "user": user_public(row)}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request):
    enforce_rate_limit("auth_login", request)
    email = body.email.strip().lower()
    row = db.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
    if not row or not bcrypt.checkpw(body.password.encode(), row["password_hash"].encode()):
        raise HTTPException(401, "Incorrect email or password")
    return {"token": make_token(row["id"]), "user": user_public(row)}


@app.get("/api/auth/me")
def me(authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    return {"user": user_public(row)}


# SECURITY FIX: this used to reset a password by email match alone — anyone
# who knew a user's email (often public) could take over their account with
# zero verification. It is now an authenticated change-password endpoint:
# the caller must already hold a valid session token AND know the current
# password. The logged-out self-serve flow (emailed one-time code) is the
# pair of endpoints below.
@app.post("/api/auth/reset-password")
def reset_password(body: ResetPasswordBody, authorization: str | None = Header(default=None), request: Request = None):
    if request is not None:
        enforce_rate_limit("auth_reset", request)
    row = current_user(authorization)
    if not bcrypt.checkpw(body.current_password.encode(), row["password_hash"].encode()):
        raise HTTPException(403, "Current password is incorrect")
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    pw_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", [pw_hash, row["id"]])
    db.commit()
    return {"ok": True}


RESET_CODE_TTL_MINUTES = 15


def _hash_reset_code(code: str, user_id: int) -> str:
    # Salted with the user id so identical codes for different users never
    # collide in storage, and hashed (not stored raw) so a DB read alone
    # can't be used to complete someone else's reset.
    import hashlib
    return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()


@app.post("/api/auth/forgot-password")
def forgot_password(body: ForgotPasswordBody, request: Request):
    """Logged-out step 1: if the email matches an account, email it a 6-digit
    code. Always returns the same generic response whether or not the email
    matched, so this endpoint can't be used to probe which emails have
    accounts (enumeration)."""
    enforce_rate_limit("auth_forgot_password", request)
    email = body.email.strip().lower()
    generic = {"ok": True, "message": "If an account exists for that email, a reset code has been sent."}
    if not EMAIL_RE.match(email):
        return generic
    row = db.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
    if not row:
        return generic
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _hash_reset_code(code, row["id"])
    import datetime
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=RESET_CODE_TTL_MINUTES)).isoformat()
    # Invalidate any earlier unused codes for this user before issuing a new
    # one, so only the most recently emailed code is ever valid.
    db.execute("UPDATE password_reset_codes SET used = 1 WHERE user_id = ? AND used = 0", [row["id"]])
    db.execute(
        "INSERT INTO password_reset_codes (user_id, code_hash, expires_at) VALUES (?, ?, ?)",
        [row["id"], code_hash, expires_at],
    )
    db.commit()
    email_service.send_email(
        to=row["email"],
        subject="Your Tru Life Properties password reset code",
        html=email_service.render_reset_code_email(code, RESET_CODE_TTL_MINUTES),
    )
    return generic


@app.post("/api/auth/forgot-password/verify")
def forgot_password_verify(body: ForgotPasswordVerifyBody, request: Request):
    """Logged-out step 2: submit the emailed code + a new password."""
    enforce_rate_limit("auth_forgot_password_verify", request)
    email = body.email.strip().lower()
    code = body.code.strip()
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    row = db.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
    if not row or not code:
        raise HTTPException(400, "Invalid or expired code")
    code_hash = _hash_reset_code(code, row["id"])
    import datetime
    now_iso = datetime.datetime.utcnow().isoformat()
    pending = db.execute(
        """SELECT * FROM password_reset_codes
               WHERE user_id = ? AND code_hash = ? AND used = 0 AND expires_at > ?
               ORDER BY id DESC LIMIT 1""",
        [row["id"], code_hash, now_iso],
    ).fetchone()
    if not pending:
        raise HTTPException(400, "Invalid or expired code")
    pw_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", [pw_hash, row["id"]])
    db.execute("UPDATE password_reset_codes SET used = 1 WHERE id = ?", [pending["id"]])
    db.commit()
    return {"ok": True}


@app.put("/api/auth/profile")
def update_profile(body: ProfileBody, authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    db.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", [body.name.strip(), body.phone.strip(), row["id"]])
    db.commit()
    updated = db.execute("SELECT * FROM users WHERE id = ?", [row["id"]]).fetchone()
    return {"user": user_public(updated)}


# ---------- Saved deals ----------

@app.get("/api/account/state")
def get_account_state(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    state = db.execute("SELECT * FROM account_state WHERE user_id=?", [user["id"]]).fetchone()
    return {
        "preferences": json.loads(state["preferences"]) if state else {},
        "project": json.loads(state["project"]) if state and state["project"] else None,
    }


@app.put("/api/account/preferences")
def save_preferences(body: PreferencesBody, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    data = body.model_dump()
    with closing(get_db()) as conn, conn:
        conn.execute("INSERT INTO account_state(user_id,preferences) VALUES (?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET preferences=excluded.preferences",
                     [user["id"], json.dumps(data)])
    return {"preferences": data}


@app.put("/api/account/project")
def save_project(body: ProjectBody, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    try:
        payload = json.dumps(body.project, allow_nan=False)
    except ValueError:
        raise HTTPException(422, "Project values must be finite")
    if len(payload.encode()) > 240_000:
        raise HTTPException(413, "Project is too large")
    if not isinstance(body.project.get("phases"), dict) or not isinstance(body.project.get("name"), str):
        raise HTTPException(422, "Invalid project")
    with closing(get_db()) as conn, conn:
        conn.execute("INSERT INTO account_state(user_id,project) VALUES (?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET project=excluded.project",
                     [user["id"], payload])
    return {"ok": True}


@app.post("/api/auth/delete-account")
def delete_account(body: DeleteAccountBody, request: Request,
                   authorization: str | None = Header(default=None)):
    enforce_rate_limit("auth_delete", request)
    user = current_user(authorization)
    if body.confirmation != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    if not bcrypt.checkpw(body.current_password.encode(), user["password_hash"].encode()):
        raise HTTPException(403, "Current password is incorrect")
    with closing(get_db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("saved_deals", "comps_history", "password_reset_codes", "account_state", "guide_purchases"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", [user["id"]])
        # Room Planner copies stored in the app (the back-office email copy is
        # a business record outside the app and is not affected).
        rp_ids = [r["id"] for r in conn.execute("SELECT id FROM room_plan_submissions WHERE user_id=?", [user["id"]]).fetchall()]
        conn.execute("DELETE FROM room_plan_submissions WHERE user_id=?", [user["id"]])
        conn.execute("DELETE FROM room_planner_grants WHERE user_id=?", [user["id"]])
        # Keep transaction identifiers for disputes/refunds, without an active account.
        conn.execute("UPDATE checkout_sessions SET status='account_deleted' "
                     "WHERE user_id=? AND status!='completed'", [user["id"]])
        if user["email"] == OWNER_EMAIL:
            conn.execute("INSERT OR IGNORE INTO seed_suppression(role) VALUES ('owner')")
        if user["email"] == AFFILIATE_ADMIN_EMAIL:
            conn.execute("INSERT OR IGNORE INTO seed_suppression(role) VALUES ('affiliate_admin')")
        linked = conn.execute("SELECT id FROM affiliates WHERE lower(email)=?", [user["email"]]).fetchall()
        for affiliate in linked:
            aid = affiliate["id"]
            conn.execute("DELETE FROM affiliate_clicks WHERE affiliate_id=?", [aid])
            conn.execute("UPDATE affiliates SET name='Deleted affiliate',handle='',email=NULL,"
                         "password_hash=NULL,active=0,is_admin=0,slug=? WHERE id=?",
                         ["deleted-" + uuid.uuid4().hex, aid])
        conn.execute("DELETE FROM users WHERE id=?", [user["id"]])
    for rp_id in rp_ids:
        _rp_remove_files(rp_id)
    return {"ok": True, "message": "Account deleted. Required financial records are retained as explained in the privacy policy."}

@app.get("/api/deals")
def list_deals(authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    rows = db.execute(
        "SELECT id, deal_type, label, payload, created_at FROM saved_deals WHERE user_id = ? ORDER BY id DESC",
        [row["id"]],
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/deals", status_code=201)
def save_deal(body: DealBody, authorization: str | None = Header(default=None)):
    import json
    row = current_user(authorization)
    cur = db.execute(
        "INSERT INTO saved_deals (user_id, deal_type, label, payload) VALUES (?, ?, ?, ?)",
        [row["id"], body.deal_type, body.label, json.dumps(body.payload)],
    )
    db.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/deals/{deal_id}")
def delete_deal(deal_id: int, authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    db.execute("DELETE FROM saved_deals WHERE id = ? AND user_id = ?", [deal_id, row["id"]])
    db.commit()
    return {"deleted": deal_id}


# ---------- Comps history ----------

@app.get("/api/comps")
def list_comps(authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    rows = db.execute(
        "SELECT id, search_label, payload, created_at FROM comps_history WHERE user_id = ? ORDER BY id DESC",
        [row["id"]],
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/comps", status_code=201)
def save_comps(body: CompsBody, authorization: str | None = Header(default=None)):
    import json
    row = current_user(authorization)
    cur = db.execute(
        "INSERT INTO comps_history (user_id, search_label, payload) VALUES (?, ?, ?)",
        [row["id"], body.search_label, json.dumps(body.payload)],
    )
    db.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/comps/{comps_id}")
def delete_comps(comps_id: int, authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    db.execute("DELETE FROM comps_history WHERE id = ? AND user_id = ?", [comps_id, row["id"]])
    db.commit()
    return {"deleted": comps_id}


@app.post("/api/comps/lookup")
def comps_lookup(body: CompsSearchBody, request: Request):
    """Live sold-comp + rent-estimate lookup via RentCast.

    No login required (the Comps Engine screener is usable by guests), but
    rate-limited per-IP since RentCast usage is metered on our plan. Returns
    503 if no RentCast key is configured, or 502 if RentCast itself errors —
    either way the frontend falls back to its built-in sample comps.
    """
    if not RENTCAST_ENABLED:
        raise HTTPException(503, "Live comps data is not configured")
    enforce_rate_limit("comps_lookup", request)

    address = body.address.strip()
    if not address:
        raise HTTPException(400, "Address is required")

    headers = {"Accept": "application/json", "X-Api-Key": RENTCAST_API_KEY}
    bed_lo = max(0, body.beds - 1)
    bed_hi = body.beds + 1

    def fetch_sold(radius_miles: float):
        params = {
            "address": address,
            "radius": radius_miles,
            "saleDateRange": 365,
            "propertyType": body.property_type,
            "bedrooms": f"{bed_lo}:{bed_hi}",
            "limit": 25,
        }
        resp = httpx.get(f"{RENTCAST_BASE_URL}/properties", params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    try:
        properties = fetch_sold(1.5)
        if len(properties) < 4:
            properties = fetch_sold(4)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"RentCast lookup failed: {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "RentCast lookup failed — please try again") from exc

    comps = []
    for p in properties:
        if not p.get("lastSaleDate") or not p.get("lastSalePrice") or not p.get("squareFootage"):
            continue
        comps.append({
            "addr": p.get("formattedAddress", ""),
            "sold": p["lastSaleDate"][:10],
            "price": p["lastSalePrice"],
            "sqft": p["squareFootage"],
            "beds": p.get("bedrooms", body.beds),
            "baths": p.get("bathrooms", body.baths),
            "dom": None,  # not provided by RentCast's sold-property data
        })

    # Reno tier is our own screening concept, not a RentCast field. Estimate
    # it from where each comp's $/sqft falls relative to the group's median
    # — above-median implies more finished/renovated, below implies dated.
    if comps:
        psf_values = sorted(c["price"] / c["sqft"] for c in comps)
        median_psf = psf_values[len(psf_values) // 2]
        for c in comps:
            psf = c["price"] / c["sqft"]
            if psf >= median_psf * 1.1:
                c["reno"] = "high"
            elif psf <= median_psf * 0.9:
                c["reno"] = "low"
            else:
                c["reno"] = "mid"

    rent_estimate = None
    try:
        rent_params = {
            "address": address,
            "propertyType": body.property_type,
            "bedrooms": body.beds,
            "bathrooms": body.baths,
            "squareFootage": body.sqft,
        }
        rent_resp = httpx.get(f"{RENTCAST_BASE_URL}/avm/rent/long-term", params=rent_params, headers=headers, timeout=15)
        if rent_resp.status_code == 200:
            rent_estimate = rent_resp.json().get("rent")
    except httpx.HTTPError:
        pass  # rent estimate is a nice-to-have; sold comps are the core data

    if rent_estimate:
        for c in comps:
            c["rent"] = rent_estimate

    return {"source": "rentcast", "comps": comps, "rentEstimate": rent_estimate}


# ---------- Guide purchases ----------

@app.get("/api/guides")
def list_guides(authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    if account_is_owner(row):
        # Owner accounts see everything unlocked without needing purchase records.
        return [{**g, "purchased": True} for g in GUIDE_CATALOG]
    owned = {
        r["guide_id"]
        for r in db.execute("SELECT guide_id FROM guide_purchases WHERE user_id = ?", [row["id"]]).fetchall()
    }
    return [{**g, "purchased": g["id"] in owned} for g in GUIDE_CATALOG]


def user_owns_guide(row, guide_id: str) -> bool:
    if account_is_owner(row):
        return True
    return db.execute(
        "SELECT 1 FROM guide_purchases WHERE user_id = ? AND guide_id = ?", [row["id"], guide_id]
    ).fetchone() is not None


@app.get("/api/brick/media-token")
def brick_media_token(authorization: str | None = Header(default=None)):
    """Short-lived token that lets the <video> element fetch paid Brick by
    Brick modules while the paywall is on. Issued only to accounts that own
    the course (or the owner account). The token carries no session rights."""
    row = current_user(authorization)
    if PAYWALL_ENABLED and not user_owns_guide(row, BRICK_COURSE_GUIDE_ID):
        raise HTTPException(403, "Purchase Brick by Brick to stream the modules.")
    payload = {"uid": row["id"], "media": BRICK_COURSE_GUIDE_ID, "exp": int(time.time()) + MEDIA_TOKEN_SECONDS}
    return {"token": jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG), "expires_in": MEDIA_TOKEN_SECONDS}


class ArcInquiryBody(BaseModel):
    kind: str = Field(default="inquiry", max_length=32)  # waitlist | inquiry | custom
    plan: str = Field(default="", max_length=160)
    name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=60)
    city: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=2000)


@app.post("/api/arc/inquiries")
def create_arc_inquiry(body: ArcInquiryBody, authorization: str | None = Header(default=None)):
    """Architecture launch-notification requests and plan inquiries. Stored,
    then forwarded to the inquiry inbox. Architecture is Coming Soon only:
    no payment or pre-purchase is taken."""
    row = current_user(authorization)
    kind = body.kind.strip().lower() if body.kind.strip().lower() in ("waitlist", "inquiry", "custom") else "inquiry"
    email = (body.email or row["email"] or "").strip().lower()
    name = (body.name or row["name"] or "").strip()
    if not email:
        raise HTTPException(400, "Email is required")
    cur = db.execute(
        """INSERT INTO arc_inquiries (user_id, kind, plan, name, email, phone, city, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [row["id"], kind, body.plan.strip(), name, email, body.phone.strip(), body.city.strip(), body.notes.strip()],
    )
    db.commit()
    inquiry_id = cur.lastrowid
    label = {"waitlist": "Launch notification request", "custom": "Custom consultation request", "inquiry": "Plan inquiry"}[kind]
    html = email_service.render_arc_inquiry_email(
        label=label, plan=body.plan.strip(), name=name, email=email, phone=body.phone.strip(),
        city=body.city.strip(), notes=body.notes.strip(), account_email=row["email"],
    )
    email_service.send_email(to=INQUIRY_EMAIL, subject=f"[Architecture] {label}: {body.plan.strip() or 'General'}", html=html)
    return {"ok": True, "id": inquiry_id, "kind": kind}


# ---------- Room Planner (Floor Plan Measurement Form) ----------
# Only clients who paid for architecture drawings (verified in-app purchase,
# or an owner/staff grant for drawings paid outside the app) plus the owner/
# staff accounts can use it. Submissions go only to room_planner.ROOM_PLANNER_EMAIL.
_RP_UPLOAD_DIR = os.path.join(os.path.dirname(DB_PATH) or ".", "room_planner_uploads")


class RoomPlanPhoto(BaseModel):
    label: str = Field(default="", max_length=80)
    data_url: str = Field(max_length=6 * 1024 * 1024)


class RoomPlanSketch(BaseModel):
    floor: str = Field(default="", max_length=40)
    png: str = Field(max_length=3 * 1024 * 1024)


class RoomPlanSubmitBody(BaseModel):
    client_ref: str = Field(min_length=8, max_length=64)
    client: dict = Field(default_factory=dict)
    front_faces: str = Field(default="", max_length=4)
    walls: list[dict] = Field(default_factory=list, max_length=room_planner.MAX_WALLS)
    rooms: list[dict] = Field(default_factory=list, max_length=room_planner.MAX_ROOMS)
    fixed_elements: str = Field(default="", max_length=3000)
    ceiling_heights: str = Field(default="", max_length=500)
    comments: str = Field(default="", max_length=3000)
    photos: list[RoomPlanPhoto] = Field(default_factory=list, max_length=room_planner.MAX_PHOTOS)
    sketches: list[RoomPlanSketch] = Field(default_factory=list, max_length=room_planner.MAX_SKETCHES)


def room_planner_allowed(row) -> bool:
    if account_is_owner(row):
        return True
    return room_planner.access_reason(db, row["id"]) is not None


def _rp_remove_files(submission_id: int):
    import shutil
    folder = os.path.join(_RP_UPLOAD_DIR, str(int(submission_id)))
    shutil.rmtree(folder, ignore_errors=True)


def _rp_safe_name(label: str, idx: int, ext: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", label or "").strip("-")[:40] or "photo"
    return f"{idx:02d}-{base}.{ext}"


def _rp_send_backoffice_email(submission_id: int) -> bool:
    import base64
    row = db.execute("SELECT * FROM room_plan_submissions WHERE id=?", [submission_id]).fetchone()
    if not row:
        return False
    sub = json.loads(row["payload"])
    summary = room_planner.measurements_summary(sub)
    folder = os.path.join(_RP_UPLOAD_DIR, str(submission_id))
    attachments, photo_labels = [], []
    for f in json.loads(row["sketch_files"]) + json.loads(row["photo_files"]):
        path = os.path.join(folder, f["file"])
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            attachments.append({"filename": f["file"], "content": base64.b64encode(fh.read()).decode()})
        if f.get("kind") == "photo":
            photo_labels.append(f.get("label") or f["file"])
    account = db.execute("SELECT email FROM users WHERE id=?", [row["user_id"]]).fetchone() if row["user_id"] else None
    html = room_planner.render_submission_email(
        submission_id=submission_id, reference=row["reference"], sub=sub, summary=summary,
        account_email=account["email"] if account else "", photo_labels=photo_labels,
        sketch_count=len(json.loads(row["sketch_files"])),
    )
    c = sub["client"]
    ok = email_service.send_email(
        to=room_planner.ROOM_PLANNER_EMAIL,
        subject=f"[Room Planner] {c['name']} — {c['address']} ({row['reference']})",
        html=html, attachments=attachments,
    )
    db.execute("UPDATE room_plan_submissions SET status=?, email_attempts=email_attempts+1 WHERE id=?",
               ["sent" if ok else "email_failed", submission_id])
    db.commit()
    return ok


@app.get("/api/room-planner/access")
def room_planner_access(authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    is_staff = account_is_owner(row)
    reason = "owner" if is_staff else (room_planner.access_reason(db, row["id"]) or "architecture_required")
    return {
        "allowed": reason != "architecture_required",
        "reason": reason,
        "can_grant": is_staff,
        "submits_to": "Tru Life Properties back office",
    }


@app.post("/api/room-planner/submit", status_code=201)
def room_planner_submit(body: RoomPlanSubmitBody, request: Request, authorization: str | None = Header(default=None)):
    row = current_user(authorization)
    if not room_planner_allowed(row):
        raise HTTPException(403, "The Room Planner opens once you've paid for architecture drawings.")
    existing = db.execute(
        "SELECT * FROM room_plan_submissions WHERE user_id=? AND client_ref=?", [row["id"], body.client_ref]
    ).fetchone()
    if existing:
        # Double-tap / retried request: never create or email a duplicate.
        return {"ok": True, "id": existing["id"], "reference": existing["reference"],
                "email_sent": existing["status"] == "sent", "duplicate": True}
    enforce_rate_limit("room_planner_submit", request)
    try:
        sub = room_planner.normalize_submission(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    photos, total = [], 0
    for i, p in enumerate(body.photos):
        try:
            mime, raw = room_planner.decode_image(p.data_url, room_planner.MAX_PHOTO_BYTES)
        except ValueError as exc:
            raise HTTPException(400, f"Photo {i + 1}: {exc}")
        total += len(raw)
        if total > room_planner.MAX_TOTAL_PHOTO_BYTES:
            raise HTTPException(413, "Photos are too large in total. Remove a few and try again.")
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
        label = re.sub(r"\s+", " ", p.label or "").strip()[:80] or f"Photo {i + 1}"
        photos.append({"kind": "photo", "label": label, "file": _rp_safe_name(label, i + 1, ext), "raw": raw})
    sketches = []
    for i, sk in enumerate(body.sketches):
        try:
            mime, raw = room_planner.decode_image(sk.png, room_planner.MAX_SKETCH_BYTES)
        except ValueError:
            continue  # the sketch is a convenience copy; numbers are authoritative
        if mime != "image/png":
            continue
        label = "sketch " + (sk.floor or "overview")
        sketches.append({"kind": "sketch", "label": label, "file": _rp_safe_name(label, i + 1, "png"), "raw": raw})

    import datetime
    reference = "RP-" + datetime.datetime.now(datetime.timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()
    cur = db.execute(
        """INSERT INTO room_plan_submissions (user_id, reference, client_ref, payload, photo_files, sketch_files)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [row["id"], reference, body.client_ref, json.dumps(sub),
         json.dumps([{k: v for k, v in f.items() if k != "raw"} for f in photos]),
         json.dumps([{k: v for k, v in f.items() if k != "raw"} for f in sketches])],
    )
    db.commit()
    submission_id = cur.lastrowid
    folder = os.path.join(_RP_UPLOAD_DIR, str(submission_id))
    os.makedirs(folder, exist_ok=True)
    for f in sketches + photos:
        with open(os.path.join(folder, f["file"]), "wb") as fh:
            fh.write(f["raw"])

    email_sent = _rp_send_backoffice_email(submission_id)
    email_service.send_email(
        to=row["email"], subject="We received your floor plan measurements",
        html=room_planner.render_client_confirmation_email(
            name=sub["client"]["name"], reference=reference, address=sub["client"]["address"]),
    )
    return {"ok": True, "id": submission_id, "reference": reference, "email_sent": email_sent}


@app.get("/api/room-planner/submissions")
def room_planner_list(authorization: str | None = Header(default=None)):
    """Back-office list (owner/staff only)."""
    row = current_user(authorization)
    if not account_is_owner(row):
        raise HTTPException(403, "Back office only")
    out = []
    for r in db.execute(
        """SELECT s.*, u.email AS account_email FROM room_plan_submissions s
           LEFT JOIN users u ON u.id = s.user_id ORDER BY s.id DESC LIMIT 200"""
    ).fetchall():
        sub = json.loads(r["payload"])
        out.append({
            "id": r["id"], "reference": r["reference"], "status": r["status"],
            "created_at": r["created_at"], "account_email": r["account_email"],
            "client": sub["client"], "rooms": len(sub["rooms"]), "walls": len(sub["walls"]),
            "photos": len(json.loads(r["photo_files"])),
        })
    return out


class RoomPlanGrantBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    note: str = Field(default="", max_length=300)


def _rp_require_staff(authorization):
    row = current_user(authorization)
    if not account_is_owner(row):
        raise HTTPException(403, "Back office only")
    return row


@app.get("/api/room-planner/grants")
def room_planner_grants(authorization: str | None = Header(default=None)):
    """Clients given Room Planner access for architecture drawings paid outside the app."""
    _rp_require_staff(authorization)
    rows = db.execute(
        """SELECT g.user_id, u.email, u.name, g.note, g.granted_by, g.created_at
             FROM room_planner_grants g JOIN users u ON u.id = g.user_id
            WHERE g.revoked_at IS NULL ORDER BY g.created_at DESC LIMIT 500"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/room-planner/grants", status_code=201)
def room_planner_grant(body: RoomPlanGrantBody, authorization: str | None = Header(default=None)):
    staff = _rp_require_staff(authorization)
    email = body.email.strip().lower()
    target = db.execute("SELECT id, email, name FROM users WHERE lower(email)=?", [email]).fetchone()
    if not target:
        raise HTTPException(404, "No app account uses that email yet. Ask the client to sign up first, then grant access.")
    db.execute(
        """INSERT INTO room_planner_grants (user_id, granted_by, note) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET granted_by=excluded.granted_by, note=excluded.note,
               created_at=CURRENT_TIMESTAMP, revoked_at=NULL, revoked_by=NULL""",
        [target["id"], staff["email"], body.note.strip()],
    )
    db.commit()
    return {"ok": True, "user_id": target["id"], "email": target["email"], "name": target["name"]}


@app.delete("/api/room-planner/grants/{user_id}")
def room_planner_revoke(user_id: int, authorization: str | None = Header(default=None)):
    staff = _rp_require_staff(authorization)
    cur = db.execute(
        "UPDATE room_planner_grants SET revoked_at=CURRENT_TIMESTAMP, revoked_by=? WHERE user_id=? AND revoked_at IS NULL",
        [staff["email"], user_id],
    )
    db.commit()
    if not cur.rowcount:
        raise HTTPException(404, "No active grant for that account")
    return {"ok": True}


@app.post("/api/room-planner/submissions/{submission_id}/resend")
def room_planner_resend(submission_id: int, authorization: str | None = Header(default=None)):
    """Retry the back-office email (owner/staff only), e.g. after a provider outage."""
    row = current_user(authorization)
    if not account_is_owner(row):
        raise HTTPException(403, "Back office only")
    if not db.execute("SELECT 1 FROM room_plan_submissions WHERE id=?", [submission_id]).fetchone():
        raise HTTPException(404, "Not found")
    return {"ok": True, "email_sent": _rp_send_backoffice_email(submission_id)}


@app.post("/api/guides/unlock")
def unlock_guide(body: UnlockBody, authorization: str | None = Header(default=None)):
    """Owner/support manual grant — NOT the purchase path. Real purchases go
    through /api/guides/checkout + the Stripe webhook below. This stays only
    so the owner account can comp a guide for a customer (refund goodwill,
    support issue, etc.) without needing database access."""
    row = current_user(authorization)
    if not account_is_owner(row):
        raise HTTPException(403, "Only the owner account can use manual unlock.")
    if body.admin_key != ADMIN_KEY:
        raise HTTPException(403, "Invalid admin key")
    if body.guide_id not in {g["id"] for g in GUIDE_CATALOG}:
        raise HTTPException(404, "Unknown guide")
    target = row
    if body.email and body.email.strip().lower() != (row["email"] or "").lower():
        target = db.execute(
            "SELECT * FROM users WHERE lower(email) = ?", [body.email.strip().lower()]
        ).fetchone()
        if not target:
            raise HTTPException(404, "No member with that email")
    db.execute(
        "INSERT OR IGNORE INTO guide_purchases (user_id, guide_id, unlocked_by) VALUES (?, ?, 'admin')",
        [target["id"], body.guide_id],
    )
    db.commit()
    return {"guide_id": body.guide_id, "purchased": True, "email": target["email"]}


@app.post("/api/guides/checkout")
def create_guide_checkout(body: CheckoutBody, authorization: str | None = Header(default=None)):
    """Create a real Stripe Checkout Session for a one-time guide purchase.
    The frontend redirects the browser to the returned URL. Purchase is only
    granted once Stripe confirms payment via the webhook below — never here."""
    if not STRIPE_ENABLED:
        raise HTTPException(503, "Payments are not configured yet. Please check back soon.")
    row = current_user(authorization)
    guide = next((g for g in GUIDE_CATALOG if g["id"] == body.guide_id), None)
    if not guide:
        raise HTTPException(404, "Unknown guide")
    already_owned = db.execute(
        "SELECT 1 FROM guide_purchases WHERE user_id = ? AND guide_id = ?", [row["id"], guide["id"]]
    ).fetchone()
    if already_owned or account_is_owner(row):
        raise HTTPException(400, "You already own this guide.")
    base = APP_BASE_URL or ""
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(round(guide["price"] * 100)),
                    "product_data": {"name": f"Tru Life Properties — {guide['name']} Guide"},
                },
                "quantity": 1,
            }],
            customer_email=row["email"],
            client_reference_id=f"user:{row['id']}:guide:{guide['id']}",
            metadata={"user_id": str(row["id"]), "guide_id": guide["id"]},
            success_url=f"{base}/?checkout=success&guide={guide['id']}",
            cancel_url=f"{base}/?checkout=cancelled&guide={guide['id']}",
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(502, f"Stripe error: {exc.user_message or 'could not start checkout'}")
    db.execute(
        "INSERT INTO checkout_sessions (stripe_session_id, user_id, guide_id, status) VALUES (?, ?, ?, 'pending')",
        [session.id, row["id"], guide["id"]],
    )
    db.commit()
    return {"checkout_url": session.url, "session_id": session.id}


def fulfill_checkout_session(session: dict) -> dict:
    """Grant a paid Stripe Checkout session: unlock the guide, auto-credit the
    referring affiliate (lifetime attribution), send receipts. Called from the
    verified webhook below. Idempotent: a session is only fulfilled once."""
    session_id = session["id"]
    if session.get("payment_status") != "paid":
        return {"received": True, "fulfilled": False}
    amount_total = session.get("amount_total") or 0
    # Dedicated connection + write lock: receipt, entitlement and affiliate credit
    # either all commit or all roll back. Concurrent retries cannot double-credit.
    with closing(get_db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT * FROM checkout_sessions WHERE stripe_session_id=?", [session_id]
        ).fetchone()
        if not pending or pending["status"] == "completed":
            return {"received": True, "fulfilled": False}
        if pending["status"] == "account_deleted":
            # Session remains available for accounting reconciliation; never recreate
            # a deleted account, entitlement or referral from a late provider event.
            import logging
            logging.getLogger("trulife.payments").warning(
                "Payment for deleted account requires reconciliation: %s", session_id)
            return {"received": True, "fulfilled": False, "reconciliation_required": True}
        buyer = conn.execute("SELECT * FROM users WHERE id=?", [pending["user_id"]]).fetchone()
        guide = next((g for g in GUIDE_CATALOG if g["id"] == pending["guide_id"]), None)
        if not buyer or not guide:
            raise HTTPException(409, "Purchase cannot be reconciled")
        credited = affiliate_tracking.record_verified_purchase(
            conn, provider="stripe", transaction_id=session_id, user_id=buyer["id"],
            product_id=guide["id"], product_name=guide["name"],
            category=GUIDE_TO_AFFILIATE_PRODUCT.get(guide["id"], "Design Guide"),
            amount_cents=amount_total, currency=session.get("currency", "usd"),
            payment_reference=session.get("payment_intent"),
        )
        conn.execute(
            """INSERT INTO guide_purchases
                   (user_id,guide_id,unlocked_by,stripe_session_id,stripe_payment_intent,amount_paid)
               VALUES (?,?,'stripe',?,?,?)
               ON CONFLICT(user_id,guide_id) DO UPDATE SET
                   unlocked_by='stripe', stripe_session_id=excluded.stripe_session_id,
                   stripe_payment_intent=excluded.stripe_payment_intent,
                   amount_paid=excluded.amount_paid""",
            [buyer["id"], guide["id"], session_id, session.get("payment_intent"), amount_total / 100],
        )
        conn.execute("UPDATE checkout_sessions SET status='completed' WHERE stripe_session_id=?", [session_id])
    if buyer and guide:
        # Receipt email — best-effort, never blocks fulfillment. If this
        # fails the purchase is still correctly granted above; only the
        # email send itself is skipped/logged.
        import datetime
        purchased_at_str = datetime.datetime.utcnow().strftime("%B %d, %Y")
        email_service.send_email(
            to=buyer["email"],
            subject=f"Your receipt — {guide['name']}" + ("" if guide.get("kind") == "course" else " Guide"),
            html=email_service.render_receipt_email(
                guide_name=guide["name"],
                amount_paid=amount_total / 100.0,
                purchased_at=purchased_at_str,
            ),
        )
        # Internal accounting copy — separate send so a failure/delay on
        # one side never blocks or depends on the other.
        email_service.send_email(
            to=ACCOUNTING_EMAIL,
            subject=f"[Purchase] {guide['name']}" + ("" if guide.get("kind") == "course" else " Guide") + f" — ${amount_total / 100.0:,.2f}",
            html=email_service.render_accounting_notification_email(
                buyer_email=buyer["email"],
                guide_name=guide["name"],
                amount_paid=amount_total / 100.0,
                purchased_at=purchased_at_str,
            ),
        )
    return {"received": True, "fulfilled": True, "affiliate_credit": credited}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")):
    """Stripe calls this directly — never trust it without verifying the
    signature against the webhook signing secret. This is the ONLY place a
    real purchase is granted."""
    if not STRIPE_ENABLED or not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Payments are not configured yet.")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except (stripe.error.SignatureVerificationError, ValueError):
        raise HTTPException(400, "Invalid webhook signature")

    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"].to_dict()
        fulfill_checkout_session(session)
    elif event["type"] == "charge.refunded":
        charge = event["data"]["object"].to_dict()
        if charge.get("payment_intent"):
            with closing(get_db()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                affiliate_tracking.apply_verified_refund(
                    conn, payment_reference=charge["payment_intent"],
                    refunded_cents=charge.get("amount_refunded", 0),
                )
    elif event["type"] in ("checkout.session.expired",):
        session = event["data"]["object"].to_dict()
        db.execute(
            "UPDATE checkout_sessions SET status = 'expired' WHERE stripe_session_id = ? AND status != 'completed'",
            [session["id"]],
        )
        db.commit()
    return {"received": True}


# ---------- Affiliate program ----------
# Public: tracking-link click logging (no auth — fires when anyone lands on
# a ?ref=<slug> link anywhere on the site).

@app.post("/api/affiliate/click", status_code=201)
def log_affiliate_click(body: ClickBody):
    row, link = affiliate_links.resolve_ref(db, body.ref)
    if not row:
        return {"logged": False}
    db.execute(
        "INSERT INTO affiliate_clicks (affiliate_id, path, referrer, link_id) VALUES (?, ?, ?, ?)",
        [row["id"], body.path[:500], body.referrer[:500], link["id"] if link else None],
    )
    db.commit()
    return {"logged": True}


# ---------- Affiliate auth ----------

@app.post("/api/affiliate/login")
def affiliate_login(body: AffiliateLoginBody, request: Request):
    enforce_rate_limit("affiliate_login", request)
    email = body.email.strip().lower()
    row = db.execute("SELECT * FROM affiliates WHERE email = ? AND active = 1", [email]).fetchone()
    if not row or not row["password_hash"] or not bcrypt.checkpw(body.password.encode(), row["password_hash"].encode()):
        raise HTTPException(401, "Incorrect email or password")
    return {"token": make_affiliate_token(row["id"]), "affiliate": affiliate_public(row)}


@app.get("/api/affiliate/me")
def affiliate_me(authorization: str | None = Header(default=None)):
    row = current_affiliate(authorization)
    return {"affiliate": affiliate_public(row)}


@app.post("/api/affiliate/refresh")
def affiliate_refresh(authorization: str | None = Header(default=None)):
    """Called by the frontend on user activity to slide the session forward.
    Keeps the JWT itself short-lived while activity continues to renew it;
    true inactivity lets the token (and the frontend idle-timer) expire."""
    row = current_affiliate(authorization)
    return {"token": make_affiliate_token(row["id"])}


# ---------- Affiliate dashboard data ----------

@app.get("/api/affiliate/overview")
def affiliate_overview(authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    affiliates = db.execute("SELECT * FROM affiliates WHERE active = 1 ORDER BY name").fetchall()
    # Non-admin affiliates only ever see their own row.
    if not me["is_admin"]:
        affiliates = [a for a in affiliates if a["id"] == me["id"]]

    cards = []
    grand_sales = grand_comm = grand_paid = grand_clicks = grand_clients = 0.0
    for a in affiliates:
        sales = db.execute("SELECT * FROM affiliate_sales WHERE affiliate_id = ?", [a["id"]]).fetchall()
        total_sales = sum(affiliate_tracking.net_sale(s) for s in sales)
        total_comm = sum(affiliate_tracking.net_sale(s) * affiliate_tracking.sale_rate(s, a) / 100 for s in sales)
        total_paid = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS t FROM affiliate_payouts WHERE affiliate_id = ?", [a["id"]]
        ).fetchone()["t"]
        click_count = db.execute(
            "SELECT COUNT(*) AS c FROM affiliate_clicks WHERE affiliate_id = ?", [a["id"]]
        ).fetchone()["c"]
        client_count = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE referred_by_affiliate_id = ?", [a["id"]]
        ).fetchone()["c"]
        auto_count = sum(1 for s in sales if (s["source"] or "manual") == "stripe")
        cards.append({
            **affiliate_public(a),
            "sale_count": len(sales),
            "auto_sale_count": auto_count,
            "total_sales": total_sales,
            "total_commission": total_comm,
            "total_paid": total_paid,
            "balance": total_comm - total_paid,
            "click_count": click_count,
            "client_count": client_count,
            "tracking_link": tracking_link_for(a["slug"]),
        })
        grand_sales += total_sales
        grand_comm += total_comm
        grand_paid += total_paid
        grand_clicks += click_count
        grand_clients += client_count

    return {
        "is_admin": bool(me["is_admin"]),
        "affiliates": cards,
        "totals": {
            "sales": grand_sales,
            "commission": grand_comm,
            "paid": grand_paid,
            "balance": grand_comm - grand_paid,
            "clicks": grand_clicks,
            "clients": grand_clients,
        },
        "products": AFFILIATE_PRODUCTS,
    }


def tracking_link_for(slug: str) -> str:
    # The ?ref= capture + signup attribution runs inside the app itself, so
    # the link must land on the app origin (not the marketing site).
    base = APP_BASE_URL or "https://app.trulifeproperties.com"
    return f"{base}/?ref={slug}"


# ---------- Campaign tracking links ----------

def _link_base() -> str:
    return APP_BASE_URL or "https://app.trulifeproperties.com"


def _links_card(aff) -> dict:
    rows = db.execute(
        "SELECT * FROM affiliate_links WHERE affiliate_id = ? ORDER BY active DESC, id DESC", [aff["id"]]
    ).fetchall()
    return {
        "affiliate_id": aff["id"],
        "name": aff["name"],
        "slug": aff["slug"],
        "main_link": tracking_link_for(aff["slug"]),
        "main_stats": affiliate_links.main_link_stats(db, aff),
        "links": [{**affiliate_links.link_public(r, _link_base()), "stats": affiliate_links.link_stats(db, aff, r)} for r in rows],
    }


@app.get("/api/affiliate/links")
def affiliate_links_list(authorization: str | None = Header(default=None)):
    """Every tracking link with its own clicks / signups / sales. Affiliates
    see their own; admin sees everyone's."""
    me = current_affiliate(authorization)
    if me["is_admin"]:
        affs = db.execute("SELECT * FROM affiliates WHERE active = 1 ORDER BY name").fetchall()
    else:
        affs = [me]
    return {
        "is_admin": bool(me["is_admin"]),
        "products": AFFILIATE_PRODUCTS,
        "destinations": affiliate_links.PRODUCT_DESTINATIONS,
        "affiliates": [_links_card(a) for a in affs],
    }


@app.post("/api/affiliate/links", status_code=201)
def affiliate_links_create(body: AffiliateLinkCreateBody, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    target = me
    if body.affiliate_id is not None and body.affiliate_id != me["id"]:
        if not me["is_admin"]:
            raise HTTPException(403, "Admin access required")
        target = db.execute("SELECT * FROM affiliates WHERE id = ? AND active = 1", [body.affiliate_id]).fetchone()
        if not target:
            raise HTTPException(404, "Affiliate not found")
    try:
        row = affiliate_links.create_link(
            db, affiliate=target, label=body.label, product=body.product, note=body.note,
            code=body.code or None, allowed_products=AFFILIATE_PRODUCTS,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**affiliate_links.link_public(row, _link_base()), "stats": affiliate_links.link_stats(db, target, row)}


@app.patch("/api/affiliate/links/{link_id}")
def affiliate_links_update(link_id: int, body: AffiliateLinkUpdateBody, authorization: str | None = Header(default=None)):
    """Rename, annotate or archive (active=false) a link. Archived links stop
    resolving for new visitors but keep their history and the clients they
    already brought in."""
    me = current_affiliate(authorization)
    row = db.execute("SELECT * FROM affiliate_links WHERE id = ?", [link_id]).fetchone()
    if not row or (not me["is_admin"] and row["affiliate_id"] != me["id"]):
        raise HTTPException(404, "Link not found")
    if body.label is not None:
        label = body.label.strip()[:80]
        if not label:
            raise HTTPException(400, "Label cannot be empty")
        db.execute("UPDATE affiliate_links SET label = ? WHERE id = ?", [label, link_id])
    if body.note is not None:
        db.execute("UPDATE affiliate_links SET note = ? WHERE id = ?", [body.note.strip()[:500], link_id])
    if body.active is not None:
        db.execute("UPDATE affiliate_links SET active = ? WHERE id = ?", [1 if body.active else 0, link_id])
    db.commit()
    row = db.execute("SELECT * FROM affiliate_links WHERE id = ?", [link_id]).fetchone()
    aff = db.execute("SELECT * FROM affiliates WHERE id = ?", [row["affiliate_id"]]).fetchone()
    return {**affiliate_links.link_public(row, _link_base()), "stats": affiliate_links.link_stats(db, aff, row)}


def require_hub_key(request: Request, key: str | None):
    """Server-to-server auth for the Tru Life Discord hub (shared secret)."""
    if not HUB_SHARED_SECRET:
        raise HTTPException(503, "Hub integration is not configured")
    if not key or not hmac.compare_digest(key, HUB_SHARED_SECRET):
        enforce_rate_limit("hub_external_sale", request)
        raise HTTPException(401, "Invalid hub key")


@app.post("/api/affiliate/external-sale")
def affiliate_external_sale(body: ExternalSaleBody, request: Request, x_trulife_hub_key: str | None = Header(default=None)):
    """Server-to-server: the Tru Life Discord hub forwards each paid Stripe
    membership invoice. Authenticated by a shared secret, never callable
    from a browser session. Idempotent per provider + transaction id."""
    require_hub_key(request, x_trulife_hub_key)
    if body.provider.strip().lower() not in ("discord-hub",):
        raise HTTPException(400, "Unknown provider")
    hub_affiliate = None
    if body.hub_affiliate_discord_id.strip():
        hub_affiliate = {"discord_user_id": body.hub_affiliate_discord_id, "name": body.hub_affiliate_name,
                         "email": body.hub_affiliate_email}
    try:
        result = affiliate_links.record_external_sale(
            db, provider=body.provider, transaction_id=body.transaction_id, email=body.email,
            amount_cents=body.amount_cents, product=body.product or "Discord Membership",
            description=body.description, hub_affiliate_code=body.hub_affiliate_code,
            member_label=body.member_label, occurred_at=body.occurred_at or None,
            hub_affiliate=hub_affiliate,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result


@app.post("/api/affiliate/hub-roster")
def affiliate_hub_roster(body: HubRosterBody, request: Request, x_trulife_hub_key: str | None = Header(default=None)):
    """Server-to-server: the Discord hub pushes its affiliate roster (on
    /affiliate add|remove, at startup and daily). The app links each Discord
    affiliate to the matching app affiliate (by Discord id, claimed invite
    code, or email) or creates the record, so nobody has to type invite codes
    by hand. Idempotent: pushing the same roster twice changes nothing."""
    require_hub_key(request, x_trulife_hub_key)
    return affiliate_links.sync_hub_roster(db, [e.model_dump() for e in body.affiliates])


@app.post("/api/affiliate/list/{affiliate_id}/merge")
def affiliate_admin_merge(affiliate_id: int, body: AffiliateMergeBody, authorization: str | None = Header(default=None)):
    """Admin: fold a Discord-created affiliate (no login yet) into the same
    person's real app affiliate account. Sales, clients, links and the
    Discord identity move across; nothing is deleted."""
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    try:
        res = affiliate_links.merge_affiliates(db, source_id=affiliate_id, target_id=body.into_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    res["affiliate"] = affiliate_public(db.execute("SELECT * FROM affiliates WHERE id = ?", [body.into_id]).fetchone())
    return res


@app.get("/api/affiliate/sales")
def affiliate_sales_list(authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if me["is_admin"]:
        rows = db.execute(
            "SELECT s.*, a.name AS affiliate_name, a.slug AS affiliate_slug FROM affiliate_sales s "
            "JOIN affiliates a ON a.id = s.affiliate_id ORDER BY s.id DESC"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT s.*, a.name AS affiliate_name, a.slug AS affiliate_slug FROM affiliate_sales s "
            "JOIN affiliates a ON a.id = s.affiliate_id WHERE s.affiliate_id = ? ORDER BY s.id DESC",
            [me["id"]],
        ).fetchall()
    out = []
    for r in rows:
        aff = db.execute("SELECT * FROM affiliates WHERE id = ?", [r["affiliate_id"]]).fetchone()
        rate = affiliate_tracking.sale_rate(r, aff)
        client_label = ""
        if r["user_id"]:
            u = db.execute("SELECT email, name FROM users WHERE id = ?", [r["user_id"]]).fetchone()
            if u:
                client_label = u["email"] if me["is_admin"] else mask_email(u["email"])
        out.append({
            "id": r["id"],
            "affiliate_id": r["affiliate_id"],
            "affiliate_name": r["affiliate_name"],
            "affiliate_slug": r["affiliate_slug"],
            "product": r["product"],
            "amount": r["amount"],
            "note": r["note"],
            "sale_date": r["sale_date"],
            "rate": rate,
            "commission": affiliate_tracking.net_sale(r) * rate / 100,
            "net_amount": affiliate_tracking.net_sale(r),
            "refunded_amount": r["refunded_amount"],
            "created_at": r["created_at"],
            "source": r["source"] or "manual",
            "client": client_label,
        })
    return out


@app.post("/api/affiliate/sales", status_code=201)
def affiliate_log_sale(body: SaleBody, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Only an administrator may record manual sales")
    target_id = body.affiliate_id if me["is_admin"] else me["id"]
    aff = db.execute("SELECT * FROM affiliates WHERE id = ? AND active = 1", [target_id]).fetchone()
    if not aff:
        raise HTTPException(404, "Affiliate not found")
    if body.amount <= 0:
        raise HTTPException(400, "Sale amount must be greater than 0")
    if body.product not in AFFILIATE_PRODUCTS:
        raise HTTPException(400, "Unknown product")
    sale_date = body.sale_date.strip() or time.strftime("%Y-%m-%d")
    rate = affiliate_rate(aff, body.product)
    cur = db.execute(
        "INSERT INTO affiliate_sales (affiliate_id, product, amount, note, sale_date, logged_by, rate_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [target_id, body.product, body.amount, body.note.strip(), sale_date, me["id"], rate],
    )
    db.commit()
    rate = affiliate_rate(aff, body.product)
    return {"id": cur.lastrowid, "commission": body.amount * rate / 100, "rate": rate}


@app.delete("/api/affiliate/sales/{sale_id}")
def affiliate_delete_sale(sale_id: int, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    sale = db.execute("SELECT * FROM affiliate_sales WHERE id=?", [sale_id]).fetchone()
    if not sale:
        raise HTTPException(404, "Sale not found")
    if (sale["source"] or "manual") != "manual":
        raise HTTPException(409, "Verified purchases cannot be deleted; refund through the payment provider")
    db.execute("DELETE FROM affiliate_sales WHERE id = ?", [sale_id])
    db.commit()
    return {"deleted": sale_id}


@app.get("/api/affiliate/list")
def affiliate_admin_list(authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    rows = db.execute("SELECT * FROM affiliates WHERE active = 1 ORDER BY name").fetchall()
    return [affiliate_public(r) for r in rows]


@app.post("/api/affiliate/list", status_code=201)
def affiliate_admin_create(body: NewAffiliateBody, authorization: str | None = Header(default=None)):
    import json as _json
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.strip().lower()).strip("-") or secrets.token_hex(4)
    base_slug, n = slug, 2
    while db.execute("SELECT id FROM affiliates WHERE slug = ?", [slug]).fetchone():
        slug, n = f"{base_slug}-{n}", n + 1
    email = body.email.strip().lower() or None
    if email and not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode() if body.password else None
    cur = db.execute(
        "INSERT INTO affiliates (slug, name, handle, email, password_hash, default_rate, product_rates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [slug, body.name.strip(), body.handle.strip(), email, pw_hash, AFFILIATE_COMMISSION_RATE, _json.dumps({})],
    )
    db.commit()
    row = db.execute("SELECT * FROM affiliates WHERE id = ?", [cur.lastrowid]).fetchone()
    return affiliate_public(row)


@app.put("/api/affiliate/list/{affiliate_id}/rates")
def affiliate_admin_update_rates(affiliate_id: int, body: AffiliateRatesBody, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    row = db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone()
    if not row:
        raise HTTPException(404, "Affiliate not found")
    # default_rate == None (or equal to the program rate) clears the override
    # so the affiliate follows the program rate again. Only future sales are
    # affected: every sale keeps the rate_snapshot it was recorded with.
    override = body.default_rate
    if override is not None and abs(override - AFFILIATE_COMMISSION_RATE) < 1e-9:
        override = None
    db.execute("UPDATE affiliates SET rate_override = ? WHERE id = ?", [override, affiliate_id])
    db.commit()
    return affiliate_public(db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone())


@app.put("/api/affiliate/list/{affiliate_id}/discord")
def affiliate_admin_set_discord(affiliate_id: int, body: AffiliateDiscordBody, authorization: str | None = Header(default=None)):
    """Admin: claim a Discord invite code for an affiliate so hub membership
    invoices from members who joined through that invite pay this affiliate
    even when the member never created an app account."""
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    row = db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone()
    if not row:
        raise HTTPException(404, "Affiliate not found")
    code = (body.invite_code or "").strip()
    code = code.rsplit("/", 1)[-1][:60]  # accept a full discord.gg/XXXX URL too
    if code:
        clash = db.execute(
            "SELECT id FROM affiliates WHERE LOWER(discord_invite_code) = ? AND id != ?", [code.lower(), affiliate_id]
        ).fetchone()
        if clash:
            raise HTTPException(409, "That Discord invite code is already claimed by another affiliate")
    db.execute("UPDATE affiliates SET discord_invite_code = ? WHERE id = ?", [code or None, affiliate_id])
    db.commit()
    return affiliate_public(db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone())


@app.put("/api/affiliate/list/{affiliate_id}/credentials")
def affiliate_admin_set_credentials(affiliate_id: int, body: AffiliateCredentialsBody, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    row = db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone()
    if not row:
        raise HTTPException(404, "Affiliate not found")
    email = body.email.strip().lower()
    if not email or not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    if not body.password or len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    dupe = db.execute("SELECT id FROM affiliates WHERE email = ? AND id != ?", [email, affiliate_id]).fetchone()
    if dupe:
        raise HTTPException(400, "That email is already used by another affiliate")
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    db.execute("UPDATE affiliates SET email = ?, password_hash = ? WHERE id = ?", [email, pw_hash, affiliate_id])
    db.commit()
    updated = db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone()
    return affiliate_public(updated)


@app.post("/api/affiliate/payouts", status_code=201)
def affiliate_admin_payout(body: PayoutBody, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    row = db.execute("SELECT * FROM affiliates WHERE id = ?", [body.affiliate_id]).fetchone()
    if not row:
        raise HTTPException(404, "Affiliate not found")
    if body.amount <= 0:
        raise HTTPException(400, "Payout amount must be greater than 0")
    db.execute(
        "INSERT INTO affiliate_payouts (affiliate_id, amount, note) VALUES (?, ?, ?)",
        [body.affiliate_id, body.amount, body.note.strip()],
    )
    db.commit()
    return {"ok": True}


# ---------- Referred clients (lifetime attribution) ----------

def _client_row(u, is_admin: bool) -> dict:
    # Ledger includes any product routed through a verified payment adapter.
    ledger = db.execute(
        "SELECT * FROM purchase_ledger WHERE user_id=? ORDER BY created_at,id", [u["id"]]
    ).fetchall()
    purchases = db.execute(
        "SELECT guide_id, amount_paid, purchased_at, unlocked_by, stripe_session_id FROM guide_purchases WHERE user_id = ? ORDER BY purchased_at",
        [u["id"]],
    ).fetchall()
    # Preserve historical purchases without double-counting migrated receipts.
    recorded = {p["transaction_id"] for p in ledger if p["provider"] == "stripe"}
    paid = [p for p in purchases if p["unlocked_by"] == "stripe" and p["stripe_session_id"] not in recorded]
    total = sum((p["amount_paid"] or 0) for p in paid) + sum((p["amount_cents"] - p["refunded_cents"]) / 100 for p in ledger)
    names = [p["product_name"] + (" (refunded)" if p["refunded_cents"] == p["amount_cents"] and p["amount_cents"] else "") for p in ledger]
    for p in paid:
        g = next((x for x in GUIDE_CATALOG if x["id"] == p["guide_id"]), None)
        names.append(g["name"] if g else p["guide_id"])
    aff = db.execute("SELECT name, slug FROM affiliates WHERE id = ?", [u["referred_by_affiliate_id"]]).fetchone() if u["referred_by_affiliate_id"] else None
    link_id = _row_get(u, "referred_link_id")
    link = db.execute("SELECT label, code FROM affiliate_links WHERE id = ?", [link_id]).fetchone() if link_id else None
    return {
        "user_id": u["id"],
        "via_link": (link["label"] or link["code"]) if link else "",
        "name": u["name"] if is_admin else (u["name"].split(" ")[0] if u["name"] else ""),
        "email": u["email"] if is_admin else mask_email(u["email"]),
        "joined": u["created_at"],
        "referred_at": u["referred_at"],
        "affiliate_id": u["referred_by_affiliate_id"],
        "affiliate_name": aff["name"] if aff else "",
        "purchase_count": len(paid) + len(ledger),
        "total_spent": total,
        "products": names,
        "last_purchase": max([p["purchased_at"] for p in paid] + [p["created_at"] for p in ledger], default=None),
        "purchases": [{"product": p["product_name"], "category": p["category"],
                       "amount": p["amount_cents"] / 100, "refunded": p["refunded_cents"] / 100,
                       "date": p["created_at"], "provider": p["provider"]} for p in ledger],
    }


@app.get("/api/affiliate/clients")
def affiliate_clients(authorization: str | None = Header(default=None)):
    """Clients attached to an affiliate. Admin sees everyone (with full
    contact details); an affiliate sees only their own referrals, with
    emails masked. Every purchase a client makes, now or later, shows here
    and is auto-credited to the affiliate."""
    me = current_affiliate(authorization)
    if me["is_admin"]:
        rows = db.execute(
            "SELECT * FROM users WHERE referred_by_affiliate_id IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM users WHERE referred_by_affiliate_id = ? ORDER BY created_at DESC", [me["id"]]
        ).fetchall()
    return [_client_row(u, bool(me["is_admin"])) for u in rows]


@app.post("/api/affiliate/clients/assign")
def affiliate_assign_client(body: AssignClientBody, authorization: str | None = Header(default=None)):
    """Admin: manually attach (or detach, with affiliate_id null) an existing
    app account to an affiliate — for signups where the tracking link was
    not used but the referral is known."""
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    email = body.email.strip().lower()
    user = db.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone()
    if not user:
        raise HTTPException(404, "No app account with that email")
    if user["referred_by_affiliate_id"] is not None:
        if body.affiliate_id == user["referred_by_affiliate_id"]:
            return _client_row(user, True)
        raise HTTPException(409, "This account already belongs to an affiliate; attribution cannot be replaced")
    if body.affiliate_id is None:
        db.execute("UPDATE users SET referred_by_affiliate_id = NULL, referred_at = NULL WHERE id = ?", [user["id"]])
    else:
        aff = db.execute("SELECT id, email FROM affiliates WHERE id = ? AND active = 1", [body.affiliate_id]).fetchone()
        if not aff:
            raise HTTPException(404, "Affiliate not found")
        if (aff["email"] or "").lower() == user["email"].lower():
            raise HTTPException(400, "Self-referrals are not allowed")
        db.execute(
            "UPDATE users SET referred_by_affiliate_id = ?, referred_at = CURRENT_TIMESTAMP WHERE id = ?",
            [body.affiliate_id, user["id"]],
        )
    db.commit()
    user = db.execute("SELECT * FROM users WHERE id = ?", [user["id"]]).fetchone()
    return _client_row(user, True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
