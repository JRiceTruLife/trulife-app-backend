"""Campaign tracking links, per-affiliate commission overrides and
external (Discord hub) recurring sales for the affiliate program.

Why this exists (Justin, Nov 14 2026):
* One affiliate promotes several products with several videos, so they need
  several links that all tie back to the same affiliate account.
* Every link must report its own clicks / signups / sales, while the client
  stays attached to the affiliate for life (first attribution wins, exactly
  as before) so later purchases of *other* items still pay out.
* The Discord community is billed monthly by Stripe through the separate
  Tru Life Hub service. The hub forwards every paid invoice here so the
  referring affiliate earns a monthly residual on it.
* Admin can give an individual affiliate a different percentage than the
  flat program rate.
"""
import datetime
import re
import secrets

import affiliate_tracking

CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")
MAX_LINKS_PER_AFFILIATE = 50

# Product -> in-app screen the campaign link should open after the referral
# is captured. Keys mirror AFFILIATE_PRODUCTS in api_server.py.
PRODUCT_DESTINATIONS = {
    "Fix & Flip Module": "brick",
    "5 Step Method Module": "five-step",
    "Design Guide": "design",
    "Architecture Plan": "architecture",
    "Architecture Custom": "architecture",
    "Coaching": "consult",
    "Discord Membership": "",
}


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS affiliate_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_id INTEGER NOT NULL REFERENCES affiliates(id) ON DELETE CASCADE,
            code TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL DEFAULT '',
            product TEXT NOT NULL DEFAULT '',
            destination TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS affiliate_links_aff_idx ON affiliate_links(affiliate_id);
    """)
    for table, col, ddl in (
        ("affiliates", "rate_override", "ALTER TABLE affiliates ADD COLUMN rate_override REAL"),
        ("affiliates", "discord_invite_code", "ALTER TABLE affiliates ADD COLUMN discord_invite_code TEXT"),
        ("affiliate_clicks", "link_id", "ALTER TABLE affiliate_clicks ADD COLUMN link_id INTEGER"),
        ("users", "referred_link_id", "ALTER TABLE users ADD COLUMN referred_link_id INTEGER"),
        ("affiliate_sales", "external_id", "ALTER TABLE affiliate_sales ADD COLUMN external_id TEXT"),
    ):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(ddl)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS sale_external_idx ON affiliate_sales(external_id) WHERE external_id IS NOT NULL"
    )
    conn.commit()


# ------------------------------------------------------------------ resolve

def normalise_code(value: str | None) -> str:
    return (value or "").strip().lower()[:100]


def resolve_ref(conn, ref: str | None):
    """A ?ref= value is either an affiliate's own slug (their main link /
    invite code) or one of their campaign link codes. Returns
    (affiliate_row | None, link_row | None)."""
    code = normalise_code(ref)
    if not code:
        return None, None
    aff = conn.execute("SELECT * FROM affiliates WHERE slug = ? AND active = 1", [code]).fetchone()
    if aff:
        return aff, None
    link = conn.execute("SELECT * FROM affiliate_links WHERE code = ? AND active = 1", [code]).fetchone()
    if not link:
        return None, None
    aff = conn.execute("SELECT * FROM affiliates WHERE id = ? AND active = 1", [link["affiliate_id"]]).fetchone()
    if not aff:
        return None, None
    return aff, link


def code_taken(conn, code: str) -> bool:
    if conn.execute("SELECT 1 FROM affiliates WHERE slug = ?", [code]).fetchone():
        return True
    return bool(conn.execute("SELECT 1 FROM affiliate_links WHERE code = ?", [code]).fetchone())


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:40]


def generate_code(conn, slug: str, label: str, requested: str | None = None) -> str:
    """Codes are namespaced under the affiliate's slug (john-fixflip-reel) so
    the affiliate is obvious from the link and codes never collide with
    another affiliate's invite code."""
    if requested:
        code = normalise_code(requested)
        if not CODE_RE.match(code):
            raise ValueError("Link code may only use letters, numbers and dashes (2-60 characters)")
        if not code.startswith(slug + "-"):
            code = f"{slug}-{code}"
        if code_taken(conn, code):
            raise ValueError("That link code is already in use")
        return code
    base = f"{slug}-{slugify(label) or 'link'}"
    code = base
    while code_taken(conn, code):
        code = f"{base}-{secrets.token_hex(2)}"
    return code


def create_link(conn, *, affiliate, label: str, product: str, note: str = "",
                code: str | None = None, allowed_products=()):
    label = (label or "").strip()[:80]
    product = (product or "").strip()[:80]
    if not label:
        raise ValueError("Give the link a label (e.g. 'Fix & Flip reel #2')")
    if product and allowed_products and product not in allowed_products:
        raise ValueError("Unknown product")
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM affiliate_links WHERE affiliate_id = ? AND active = 1", [affiliate["id"]]
    ).fetchone()["c"]
    if count >= MAX_LINKS_PER_AFFILIATE:
        raise ValueError(f"Limit of {MAX_LINKS_PER_AFFILIATE} active links reached; archive one first")
    final_code = generate_code(conn, affiliate["slug"], label, code)
    cur = conn.execute(
        "INSERT INTO affiliate_links (affiliate_id, code, label, product, destination, note) VALUES (?,?,?,?,?,?)",
        [affiliate["id"], final_code, label, product, PRODUCT_DESTINATIONS.get(product, ""), (note or "").strip()[:500]],
    )
    conn.commit()
    return conn.execute("SELECT * FROM affiliate_links WHERE id = ?", [cur.lastrowid]).fetchone()


def link_url(base: str, code: str, destination: str = "") -> str:
    url = f"{base}/?ref={code}"
    if destination:
        url += f"&open={destination}"
    return url


# -------------------------------------------------------------------- stats

def _sales_for(conn, affiliate_id: int, where: str, params: list):
    return conn.execute(
        "SELECT s.* FROM affiliate_sales s LEFT JOIN users u ON u.id = s.user_id "
        f"WHERE s.affiliate_id = ? AND {where}",
        [affiliate_id, *params],
    ).fetchall()


def _summarise(conn, affiliate, sales):
    revenue = sum(affiliate_tracking.net_sale(s) for s in sales)
    commission = sum(affiliate_tracking.net_sale(s) * affiliate_tracking.sale_rate(s, affiliate) / 100 for s in sales)
    return {"sale_count": len(sales), "revenue": round(revenue, 2), "commission": round(commission, 2)}


def link_stats(conn, affiliate, link_row):
    clicks = conn.execute(
        "SELECT COUNT(*) AS c FROM affiliate_clicks WHERE affiliate_id = ? AND link_id = ?",
        [affiliate["id"], link_row["id"]],
    ).fetchone()["c"]
    signups = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE referred_by_affiliate_id = ? AND referred_link_id = ?",
        [affiliate["id"], link_row["id"]],
    ).fetchone()["c"]
    sales = _sales_for(conn, affiliate["id"], "u.referred_link_id = ?", [link_row["id"]])
    return {"clicks": clicks, "signups": signups, **_summarise(conn, affiliate, sales)}


def main_link_stats(conn, affiliate):
    """Traffic that used the plain slug link / invite code (no campaign)."""
    clicks = conn.execute(
        "SELECT COUNT(*) AS c FROM affiliate_clicks WHERE affiliate_id = ? AND link_id IS NULL", [affiliate["id"]]
    ).fetchone()["c"]
    signups = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE referred_by_affiliate_id = ? AND referred_link_id IS NULL",
        [affiliate["id"]],
    ).fetchone()["c"]
    sales = _sales_for(conn, affiliate["id"], "(s.user_id IS NULL OR u.referred_link_id IS NULL)", [])
    return {"clicks": clicks, "signups": signups, **_summarise(conn, affiliate, sales)}


def link_public(row, base: str) -> dict:
    return {
        "id": row["id"],
        "affiliate_id": row["affiliate_id"],
        "code": row["code"],
        "label": row["label"],
        "product": row["product"],
        "destination": row["destination"],
        "note": row["note"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "url": link_url(base, row["code"], row["destination"]),
    }


# ----------------------------------------------------------- external sales

def record_external_sale(conn, *, provider: str, transaction_id: str, email: str,
                         amount_cents: int, product: str, description: str = "",
                         hub_affiliate_code: str = "", member_label: str = "",
                         occurred_at: str | None = None):
    """Credit a verified payment that happened outside the app (today: a
    Discord membership invoice paid through the hub's Stripe account).

    Attribution order:
      1. The payer's email matches an app account that is attached to an
         affiliate -> lifetime attribution, recorded through the shared
         purchase ledger (so it shows under that client too).
      2. Otherwise the hub tells us which Discord invite code the member
         joined through; if an app affiliate has claimed that code, credit
         them directly.
      3. Otherwise nothing is credited (reported back so the hub can log it).
    Idempotent per (provider, transaction_id).
    """
    provider = (provider or "").strip().lower()[:40]
    transaction_id = (transaction_id or "").strip()[:200]
    if not provider or not transaction_id:
        raise ValueError("provider and transaction_id are required")
    if type(amount_cents) is not int or amount_cents < 0:
        raise ValueError("amount_cents must be a nonnegative integer")
    external_id = f"{provider}:{transaction_id}"
    existing = conn.execute("SELECT id, affiliate_id FROM affiliate_sales WHERE external_id = ?", [external_id]).fetchone()
    if existing:
        return {"credited": True, "duplicate": True, "sale_id": existing["id"], "affiliate_id": existing["affiliate_id"]}
    ledger_dup = conn.execute(
        "SELECT id FROM purchase_ledger WHERE provider = ? AND transaction_id = ?", [provider, transaction_id]
    ).fetchone()
    if ledger_dup:
        return {"credited": False, "duplicate": True, "reason": "already_recorded_uncredited"}

    email = (email or "").strip().lower()
    sale_date = (occurred_at or "")[:10] or datetime.date.today().isoformat()
    note = (description or product or "").strip()[:200]
    if member_label:
        note = f"{note} — {member_label.strip()[:80]}" if note else member_label.strip()[:80]

    user = conn.execute("SELECT * FROM users WHERE email = ?", [email]).fetchone() if email else None
    if user and user["referred_by_affiliate_id"]:
        res = affiliate_tracking.record_verified_purchase(
            conn, provider=provider, transaction_id=transaction_id, user_id=user["id"],
            product_id=slugify(product) or "external", product_name=description or product,
            category=product, amount_cents=amount_cents,
        )
        if res and res.get("affiliate_id"):
            conn.execute(
                "UPDATE affiliate_sales SET external_id = ?, sale_date = ? WHERE purchase_id = ?",
                [external_id, sale_date, res["purchase_id"]],
            )
            conn.commit()
            return {"credited": True, "duplicate": False, "affiliate_id": res["affiliate_id"],
                    "via": "client_email", "rate": res["rate"], "commission": res["commission"]}
        conn.commit()
        return {"credited": False, "duplicate": False, "reason": "client_not_attributed"}

    code = normalise_code(hub_affiliate_code)
    aff = conn.execute(
        "SELECT * FROM affiliates WHERE active = 1 AND discord_invite_code IS NOT NULL AND LOWER(discord_invite_code) = ?",
        [code],
    ).fetchone() if code else None
    if not aff:
        return {"credited": False, "duplicate": False, "reason": "no_attribution"}
    if email and (aff["email"] or "").lower() == email:
        return {"credited": False, "duplicate": False, "reason": "self_referral"}
    rate = affiliate_tracking.effective_rate(aff)
    cur = conn.execute(
        """INSERT INTO affiliate_sales
           (affiliate_id, product, amount, note, sale_date, user_id, source, external_id, rate_snapshot)
           VALUES (?,?,?,?,?,NULL,?,?,?)""",
        [aff["id"], product, amount_cents / 100, note, sale_date, provider, external_id, rate],
    )
    conn.commit()
    return {"credited": True, "duplicate": False, "affiliate_id": aff["id"], "via": "discord_invite_code",
            "sale_id": cur.lastrowid, "rate": rate, "commission": round(amount_cents * rate / 10000, 2)}
