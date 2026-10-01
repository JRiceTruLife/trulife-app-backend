"""Provider-neutral purchase attribution. Only call after server-side payment verification.

No public endpoint accepts an amount, affiliate ID or receipt from a browser.
The caller owns the transaction, allowing entitlements and commissions to commit together.
Amounts in the purchase ledger are integer minor units (USD cents).
"""
import datetime
import json


def migrate(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS purchase_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            transaction_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            affiliate_id INTEGER,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
            refunded_cents INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'usd',
            payment_reference TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider, transaction_id)
        );
        CREATE INDEX IF NOT EXISTS purchase_user_idx ON purchase_ledger(user_id);
        CREATE INDEX IF NOT EXISTS purchase_payment_idx ON purchase_ledger(payment_reference);
        CREATE INDEX IF NOT EXISTS user_referral_idx ON users(referred_by_affiliate_id);
        CREATE TABLE IF NOT EXISTS payment_refunds (
            payment_reference TEXT PRIMARY KEY,
            refunded_cents INTEGER NOT NULL
        );
    """)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(affiliate_sales)")}
    for name, definition in (
        ("purchase_id", "INTEGER"),
        ("rate_snapshot", "REAL"),
        ("refunded_amount", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE affiliate_sales ADD COLUMN {name} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS sale_purchase_idx ON affiliate_sales(purchase_id) WHERE purchase_id IS NOT NULL")
    conn.commit()


def record_verified_purchase(conn, *, provider, transaction_id, user_id,
                             product_id, product_name, category, amount_cents,
                             currency="usd", payment_reference=None):
    """Internal integration point for verified Stripe / future store receipts.

    Provider + transaction is unique. Products are not restricted to design guides.
    Never invoke this until the payment adapter has verified ownership and payment.
    """
    if not transaction_id or not provider or not product_id:
        raise ValueError("Payment and product identifiers are required")
    if type(amount_cents) is not int or amount_cents < 0 or currency.lower() != "usd":
        raise ValueError("Only nonnegative USD minor-unit amounts are supported")
    old = conn.execute(
        "SELECT * FROM purchase_ledger WHERE provider=? AND transaction_id=?",
        (provider, transaction_id),
    ).fetchone()
    if old:
        if (old["user_id"], old["product_id"], old["amount_cents"]) != (user_id, product_id, amount_cents):
            raise ValueError("Transaction already belongs to another purchase")
        return None
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise ValueError("Purchaser account not found")
    aff = conn.execute(
        "SELECT * FROM affiliates WHERE id=? AND active=1", (user["referred_by_affiliate_id"],)
    ).fetchone()
    # A same-email affiliate purchase is not a qualified referral.
    if aff and (aff["email"] or "").lower() == user["email"].lower():
        aff = None
    affiliate_id = aff["id"] if aff else None
    cur = conn.execute(
        """INSERT INTO purchase_ledger
           (provider,transaction_id,user_id,affiliate_id,product_id,product_name,
            category,amount_cents,currency,payment_reference)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (provider, transaction_id, user_id, affiliate_id, product_id, product_name,
         category, amount_cents, currency.lower(), payment_reference),
    )
    purchase_id = cur.lastrowid
    refund = conn.execute("SELECT refunded_cents FROM payment_refunds WHERE payment_reference=?", (payment_reference,)).fetchone() if provider == "stripe" and payment_reference else None
    refunded_cents = min(amount_cents, refund["refunded_cents"]) if refund else 0
    if refunded_cents:
        conn.execute("UPDATE purchase_ledger SET refunded_cents=? WHERE id=?", (refunded_cents, purchase_id))
    if not aff or amount_cents == 0:
        return {"purchase_id": purchase_id, "affiliate_id": None}
    rate = float(json.loads(aff["product_rates"] or "{}").get(category, aff["default_rate"]))
    if not 0 <= rate <= 100:
        raise ValueError("Invalid commission rate")
    # Retain compatibility with historical Stripe rows when backfilling a receipt.
    prior = conn.execute(
        "SELECT id FROM affiliate_sales WHERE stripe_session_id=? ORDER BY id LIMIT 1",
        (transaction_id,),
    ).fetchone() if provider == "stripe" else None
    if prior:
        conn.execute("UPDATE affiliate_sales SET purchase_id=? WHERE id=?", (purchase_id, prior["id"]))
    else:
        conn.execute(
            """INSERT INTO affiliate_sales
               (affiliate_id,product,amount,note,sale_date,user_id,source,
                stripe_session_id,purchase_id,rate_snapshot,refunded_amount)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (aff["id"], category, amount_cents / 100, product_name,
             datetime.date.today().isoformat(), user_id, provider,
             transaction_id if provider == "stripe" else None, purchase_id, rate, refunded_cents / 100),
        )
    return {"purchase_id": purchase_id, "affiliate_id": aff["id"], "rate": rate,
            "commission": round((amount_cents - refunded_cents) * rate / 10000, 2)}


def apply_verified_refund(conn, *, payment_reference, refunded_cents):
    """Stripe charge.refunded supplies a cumulative refunded amount.

    MAX makes duplicate/out-of-order notifications idempotent. A partial refund
    reduces earned commission proportionally; a full refund reduces it to zero.
    """
    if type(refunded_cents) is not int or refunded_cents < 0:
        raise ValueError("Invalid refund")
    conn.execute(
        """INSERT INTO payment_refunds(payment_reference,refunded_cents) VALUES (?,?)
           ON CONFLICT(payment_reference) DO UPDATE SET refunded_cents=MAX(refunded_cents,excluded.refunded_cents)""",
        (payment_reference, refunded_cents),
    )
    rows = conn.execute(
        "SELECT * FROM purchase_ledger WHERE provider='stripe' AND payment_reference=?",
        (payment_reference,),
    ).fetchall()
    for row in rows:
        total = min(row["amount_cents"], max(row["refunded_cents"], refunded_cents))
        conn.execute("UPDATE purchase_ledger SET refunded_cents=? WHERE id=?", (total, row["id"]))
        conn.execute("UPDATE affiliate_sales SET refunded_amount=? WHERE purchase_id=?", (total / 100, row["id"]))


def net_sale(row):
    return max(0, row["amount"] - (row["refunded_amount"] or 0))


def sale_rate(row, affiliate):
    if row["rate_snapshot"] is not None:
        return row["rate_snapshot"]
    return json.loads(affiliate["product_rates"] or "{}").get(row["product"], affiliate["default_rate"])
