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
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

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
AFFILIATE_PRODUCTS = [
    "Design Guide",
    "Architecture Plan",
    "Architecture Custom",
    "Fix & Flip Foundations Course",
    "5 Step Method Module",
    "Coaching",
]

# The 4 canonical design guides + the 5 Step Method module — shared catalog,
# purchase status is per-user. Same guide_purchases table backs both.
GUIDE_CATALOG = [
    {"id": "linder", "name": "Modern Farmhouse", "price": 497},
    {"id": "forestoak", "name": "Coastal Brass & Blue", "price": 597},
    {"id": "canyon", "name": "Canyon Collection", "price": 697},
    {"id": "colonial", "name": "Colonial", "price": 797},
    {"id": "five-step-method", "name": "5 Step Method", "price": 49},
]

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
            default_rate REAL NOT NULL DEFAULT 10,
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


# --- Owner account: seeded/promoted on every startup. ---
# Set via env vars so the password isn't hardcoded in source; falls back to a
# dev default for local/preview testing. Rotate OWNER_PASSWORD before real launch.
OWNER_EMAIL = os.environ.get("TRULIFE_OWNER_EMAIL", "justin@trulifeproperties.com").strip().lower()
OWNER_PASSWORD = _require_secret("TRULIFE_OWNER_PASSWORD", "TruLife2026!")
OWNER_NAME = os.environ.get("TRULIFE_OWNER_NAME", "Justin Rice")


def seed_owner_account():
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
    {"slug": "justice", "name": "Justice", "handle": "@justice", "default_rate": 15, "product_rates": {}},
    {"slug": "greylo", "name": "Greylo", "handle": "@greylo", "default_rate": 10, "product_rates": {}},
    {"slug": "erik", "name": "Erik All", "handle": "@erikall", "default_rate": 10, "product_rates": {"Coaching": 5}},
    {"slug": "damaine", "name": "Damaine Wilson", "handle": "@damainewilson", "default_rate": 10, "product_rates": {}},
    {"slug": "tayvon", "name": "Tayvon", "handle": "@tayvon", "default_rate": 10, "product_rates": {"Coaching": 8}},
]

AFFILIATE_ADMIN_EMAIL = os.environ.get("TRULIFE_AFFILIATE_ADMIN_EMAIL", "justin@trulifeproperties.com").strip().lower()

# Internal accounting notification: every completed purchase also sends a
# copy of the receipt here (in addition to the buyer's own receipt), so
# there's a record of transactions for bookkeeping without depending on any
# one person's personal inbox.
ACCOUNTING_EMAIL = os.environ.get("TRULIFE_ACCOUNTING_EMAIL", "info@trulifeproperties.com").strip().lower()
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
    # Ensure the owner/admin has an affiliate-portal login too (full dashboard access).
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
    if content_length and content_length.isdigit() and int(content_length) > _MAX_BODY_BYTES:
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
    "affiliate_login": (10, 60),
    # Forgot-password: tighter limits since these are unauthenticated and
    # trigger an email send / accept a guessable-length code.
    "auth_forgot_password": (3, 300),
    "auth_forgot_password_verify": (8, 300),
    # RentCast API usage is metered on our plan — cap live comps lookups
    # per-IP so one user (or a bot) can't burn the monthly request quota.
    "comps_lookup": (12, 3600),
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
        "is_owner": bool(row["is_owner"]),
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
        "default_rate": row["default_rate"],
        "product_rates": _json.loads(row["product_rates"] or "{}"),
        "is_admin": bool(row["is_admin"]),
    }


def affiliate_rate(row, product: str) -> float:
    import json as _json
    rates = _json.loads(row["product_rates"] or "{}")
    return rates.get(product, row["default_rate"])


# ---------- Schemas ----------

class SignupBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    name: str = Field(default="", max_length=200)


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class ProfileBody(BaseModel):
    name: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=40)


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


class CheckoutBody(BaseModel):
    guide_id: str = Field(max_length=100)


class AffiliateLoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class AffiliateRatesBody(BaseModel):
    default_rate: float | None = Field(default=None, ge=0, le=100)
    product_rates: dict | None = None


class AffiliateCredentialsBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class NewAffiliateBody(BaseModel):
    name: str = Field(max_length=200)
    handle: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=200)
    default_rate: float = Field(default=10, ge=0, le=100)


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


# ---------- Auth routes ----------

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
    cur = db.execute(
        "INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)",
        [email, pw_hash, body.name.strip()],
    )
    db.commit()
    user_id = cur.lastrowid
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
        raise HTTPException(401, "Current password is incorrect")
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
    if row["is_owner"]:
        # Owner accounts see everything unlocked without needing purchase records.
        return [{**g, "purchased": True} for g in GUIDE_CATALOG]
    owned = {
        r["guide_id"]
        for r in db.execute("SELECT guide_id FROM guide_purchases WHERE user_id = ?", [row["id"]]).fetchall()
    }
    return [{**g, "purchased": g["id"] in owned} for g in GUIDE_CATALOG]


@app.post("/api/guides/unlock")
def unlock_guide(body: UnlockBody, authorization: str | None = Header(default=None)):
    """Owner/support manual grant — NOT the purchase path. Real purchases go
    through /api/guides/checkout + the Stripe webhook below. This stays only
    so the owner account can comp a guide for a customer (refund goodwill,
    support issue, etc.) without needing database access."""
    row = current_user(authorization)
    if not row["is_owner"]:
        raise HTTPException(403, "Only the owner account can use manual unlock.")
    if body.admin_key != ADMIN_KEY:
        raise HTTPException(403, "Invalid admin key")
    if body.guide_id not in {g["id"] for g in GUIDE_CATALOG}:
        raise HTTPException(404, "Unknown guide")
    db.execute(
        "INSERT OR IGNORE INTO guide_purchases (user_id, guide_id, unlocked_by) VALUES (?, ?, 'admin')",
        [row["id"], body.guide_id],
    )
    db.commit()
    return {"guide_id": body.guide_id, "purchased": True}


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
    if already_owned or row["is_owner"]:
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

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"].to_dict()
        session_id = session["id"]
        pending = db.execute(
            "SELECT * FROM checkout_sessions WHERE stripe_session_id = ?", [session_id]
        ).fetchone()
        if not pending:
            # Unknown session (or already processed and pruned) — ack so
            # Stripe stops retrying, but grant nothing.
            return {"received": True}
        if pending["status"] == "completed":
            # Duplicate webhook delivery for a session we already fulfilled.
            return {"received": True}
        if session.get("payment_status") != "paid":
            return {"received": True}
        amount_total = session.get("amount_total") or 0
        db.execute(
            """INSERT INTO guide_purchases
                   (user_id, guide_id, unlocked_by, stripe_session_id, stripe_payment_intent, amount_paid)
               VALUES (?, ?, 'stripe', ?, ?, ?)
               ON CONFLICT(user_id, guide_id) DO UPDATE SET
                   stripe_session_id = excluded.stripe_session_id,
                   stripe_payment_intent = excluded.stripe_payment_intent,
                   amount_paid = excluded.amount_paid""",
            [
                pending["user_id"],
                pending["guide_id"],
                session_id,
                session.get("payment_intent"),
                amount_total / 100.0,
            ],
        )
        db.execute(
            "UPDATE checkout_sessions SET status = 'completed' WHERE stripe_session_id = ?", [session_id]
        )
        db.commit()
        # Receipt email — best-effort, never blocks fulfillment. If this
        # fails the purchase is still correctly granted above; only the
        # email send itself is skipped/logged.
        buyer = db.execute("SELECT * FROM users WHERE id = ?", [pending["user_id"]]).fetchone()
        guide = next((g for g in GUIDE_CATALOG if g["id"] == pending["guide_id"]), None)
        if buyer and guide:
            import datetime
            purchased_at_str = datetime.datetime.utcnow().strftime("%B %d, %Y")
            email_service.send_email(
                to=buyer["email"],
                subject=f"Your receipt — {guide['name']} Guide",
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
                subject=f"[Purchase] {guide['name']} Guide — ${amount_total / 100.0:,.2f}",
                html=email_service.render_accounting_notification_email(
                    buyer_email=buyer["email"],
                    guide_name=guide["name"],
                    amount_paid=amount_total / 100.0,
                    purchased_at=purchased_at_str,
                ),
            )
    elif event["type"] in ("checkout.session.expired",):
        session = event["data"]["object"].to_dict()
        db.execute(
            "UPDATE checkout_sessions SET status = 'expired' WHERE stripe_session_id = ?",
            [session["id"]],
        )
        db.commit()
    return {"received": True}


# ---------- Affiliate program ----------
# Public: tracking-link click logging (no auth — fires when anyone lands on
# a ?ref=<slug> link anywhere on the site).

@app.post("/api/affiliate/click", status_code=201)
def log_affiliate_click(body: ClickBody):
    row = db.execute("SELECT id FROM affiliates WHERE slug = ? AND active = 1", [body.ref.strip().lower()]).fetchone()
    if not row:
        return {"logged": False}
    db.execute(
        "INSERT INTO affiliate_clicks (affiliate_id, path, referrer) VALUES (?, ?, ?)",
        [row["id"], body.path[:500], body.referrer[:500]],
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
    grand_sales = grand_comm = grand_paid = grand_clicks = 0.0
    for a in affiliates:
        sales = db.execute("SELECT * FROM affiliate_sales WHERE affiliate_id = ?", [a["id"]]).fetchall()
        total_sales = sum(s["amount"] for s in sales)
        total_comm = sum(s["amount"] * affiliate_rate(a, s["product"]) / 100 for s in sales)
        total_paid = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS t FROM affiliate_payouts WHERE affiliate_id = ?", [a["id"]]
        ).fetchone()["t"]
        click_count = db.execute(
            "SELECT COUNT(*) AS c FROM affiliate_clicks WHERE affiliate_id = ?", [a["id"]]
        ).fetchone()["c"]
        cards.append({
            **affiliate_public(a),
            "sale_count": len(sales),
            "total_sales": total_sales,
            "total_commission": total_comm,
            "total_paid": total_paid,
            "balance": total_comm - total_paid,
            "click_count": click_count,
            "tracking_link": f"https://trulifeproperties.com/?ref={a['slug']}",
        })
        grand_sales += total_sales
        grand_comm += total_comm
        grand_paid += total_paid
        grand_clicks += click_count

    return {
        "is_admin": bool(me["is_admin"]),
        "affiliates": cards,
        "totals": {
            "sales": grand_sales,
            "commission": grand_comm,
            "paid": grand_paid,
            "balance": grand_comm - grand_paid,
            "clicks": grand_clicks,
        },
        "products": AFFILIATE_PRODUCTS,
    }


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
        rate = affiliate_rate(aff, r["product"])
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
            "commission": r["amount"] * rate / 100,
            "created_at": r["created_at"],
        })
    return out


@app.post("/api/affiliate/sales", status_code=201)
def affiliate_log_sale(body: SaleBody, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    target_id = body.affiliate_id if me["is_admin"] else me["id"]
    aff = db.execute("SELECT * FROM affiliates WHERE id = ? AND active = 1", [target_id]).fetchone()
    if not aff:
        raise HTTPException(404, "Affiliate not found")
    if body.amount <= 0:
        raise HTTPException(400, "Sale amount must be greater than 0")
    if body.product not in AFFILIATE_PRODUCTS:
        raise HTTPException(400, "Unknown product")
    sale_date = body.sale_date.strip() or time.strftime("%Y-%m-%d")
    cur = db.execute(
        "INSERT INTO affiliate_sales (affiliate_id, product, amount, note, sale_date, logged_by) VALUES (?, ?, ?, ?, ?, ?)",
        [target_id, body.product, body.amount, body.note.strip(), sale_date, me["id"]],
    )
    db.commit()
    rate = affiliate_rate(aff, body.product)
    return {"id": cur.lastrowid, "commission": body.amount * rate / 100, "rate": rate}


@app.delete("/api/affiliate/sales/{sale_id}")
def affiliate_delete_sale(sale_id: int, authorization: str | None = Header(default=None)):
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        db.execute("DELETE FROM affiliate_sales WHERE id = ? AND affiliate_id = ?", [sale_id, me["id"]])
    else:
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
        [slug, body.name.strip(), body.handle.strip(), email, pw_hash, body.default_rate, _json.dumps({})],
    )
    db.commit()
    row = db.execute("SELECT * FROM affiliates WHERE id = ?", [cur.lastrowid]).fetchone()
    return affiliate_public(row)


@app.put("/api/affiliate/list/{affiliate_id}/rates")
def affiliate_admin_update_rates(affiliate_id: int, body: AffiliateRatesBody, authorization: str | None = Header(default=None)):
    import json as _json
    me = current_affiliate(authorization)
    if not me["is_admin"]:
        raise HTTPException(403, "Admin access required")
    row = db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone()
    if not row:
        raise HTTPException(404, "Affiliate not found")
    default_rate = body.default_rate if body.default_rate is not None else row["default_rate"]
    product_rates = body.product_rates if body.product_rates is not None else _json.loads(row["product_rates"] or "{}")
    db.execute(
        "UPDATE affiliates SET default_rate = ?, product_rates = ? WHERE id = ?",
        [default_rate, _json.dumps(product_rates), affiliate_id],
    )
    db.commit()
    updated = db.execute("SELECT * FROM affiliates WHERE id = ?", [affiliate_id]).fetchone()
    return affiliate_public(updated)


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
