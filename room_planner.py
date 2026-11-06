"""room_planner.py — Floor Plan Measurement Form (Room Planner) helpers.

Clients who paid for architecture drawings enter exterior wall and room measurements, attach room photos,
watch a simple overview sketch form on screen, and submit the package to the
Tru Life back office. Submissions can only go to the fixed back-office inbox
(ROOM_PLANNER_EMAIL); the client never chooses a recipient and there is no
client-facing export/download.

This module holds the pure pieces (access rule, validation, geometry, email
rendering) so they can be unit tested without the HTTP layer. Routes live in
api_server.py.
"""
import base64
import html as html_mod
import math
import os
import re

# Fixed destination. Overridable by env for staging only; clients cannot
# influence it.
ROOM_PLANNER_EMAIL = (os.environ.get("TRULIFE_ROOM_PLANNER_EMAIL", "admin@trulifefiles.com").strip().lower()
                      or "admin@trulifefiles.com")

DIRECTIONS = {"N": 90, "NE": 45, "E": 0, "SE": 315, "S": 270, "SW": 225, "W": 180, "NW": 135}
POSITIONS = {
    "front-left": "Front left", "front-center": "Front center", "front-right": "Front right",
    "middle-left": "Middle left", "center": "Center", "middle-right": "Middle right",
    "back-left": "Back left", "back-center": "Back center", "back-right": "Back right",
}
PROJECT_TYPES = [
    "Full interior renovation", "Kitchen remodel", "Bathroom remodel", "Layout change / open concept",
    "Addition", "New build", "Other",
]
FLOOR_SCOPES = ["1st floor", "2nd floor", "3rd floor", "Basement", "Multiple floors", "Whole house"]
ROOM_FLOORS = ["1st floor", "2nd floor", "3rd floor", "Basement"]

MAX_WALLS = 40
MAX_ROOMS = 40
MAX_PHOTOS = 20
MAX_PHOTO_BYTES = 4 * 1024 * 1024
MAX_TOTAL_PHOTO_BYTES = 20 * 1024 * 1024
MAX_SKETCHES = 4
MAX_SKETCH_BYTES = 2 * 1024 * 1024
# Request cap for the submit endpoint only (base64 inflates by ~4/3).
MAX_SUBMIT_BODY_BYTES = 32 * 1024 * 1024

_DATA_URL_RE = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=\s]+)$")


# ---------- Access ----------

# Access rule (Justin, Nov 2026): the Room Planner opens only for clients who
# have paid for architecture drawings. Other purchases (design guides,
# courses, 5 Step Method) do NOT unlock it. Catalog/product ids for the
# architecture drawing offers; overridable by env when the real checkout ids
# are added (comma separated).
_ARCH_DEFAULT_IDS = "architecture-plan,architecture-custom,architecture-drawings"
ARCHITECTURE_PRODUCT_IDS = frozenset(
    x.strip().lower() for x in os.environ.get("TRULIFE_ARCHITECTURE_PRODUCT_IDS", _ARCH_DEFAULT_IDS).split(",") if x.strip()
)
# Affiliate commission categories that represent architecture drawing sales.
ARCHITECTURE_CATEGORIES = frozenset({"architecture plan", "architecture custom"})


def has_architecture_purchase(conn, user_id: int) -> bool:
    """True when the account has a verified, not fully refunded payment for
    architecture drawings (in-app ledger or Stripe-fulfilled catalog row)."""
    for r in conn.execute(
        """SELECT product_id, category FROM purchase_ledger
            WHERE user_id=? AND amount_cents>0 AND refunded_cents<amount_cents""",
        [user_id],
    ).fetchall():
        if (r["product_id"] or "").lower() in ARCHITECTURE_PRODUCT_IDS or (r["category"] or "").lower() in ARCHITECTURE_CATEGORIES:
            return True
    for r in conn.execute(
        """SELECT gp.guide_id, gp.amount_paid, pr.refunded_cents
             FROM guide_purchases gp
             LEFT JOIN payment_refunds pr ON pr.payment_reference = gp.stripe_payment_intent
            WHERE gp.user_id=? AND gp.unlocked_by='stripe' AND COALESCE(gp.amount_paid,0) > 0""",
        [user_id],
    ).fetchall():
        if (r["guide_id"] or "").lower() not in ARCHITECTURE_PRODUCT_IDS:
            continue
        if (r["refunded_cents"] or 0) < int(round((r["amount_paid"] or 0) * 100)):
            return True
    return False


def active_grant(conn, user_id: int):
    """Owner/staff grant for a client who paid for architecture drawings
    outside the app (invoice, in person). Returns the row or None."""
    return conn.execute(
        "SELECT * FROM room_planner_grants WHERE user_id=? AND revoked_at IS NULL", [user_id]
    ).fetchone()


def access_reason(conn, user_id: int):
    """'purchase', 'granted', or None (locked)."""
    if has_architecture_purchase(conn, user_id):
        return "purchase"
    if active_grant(conn, user_id):
        return "granted"
    return None


# ---------- Validation ----------

def _clean(s, limit=500) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(s or "")).strip()[:limit]


def _num(v, lo, hi, default=0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return max(lo, min(hi, f))


def to_feet(ft, inch) -> float:
    return _num(ft, 0, 1000) + _num(inch, 0, 11.99) / 12.0


def fmt_ft_in(total_ft: float) -> str:
    total_in = int(round(total_ft * 12))
    return f"{total_in // 12}' {total_in % 12}\""


def decode_image(data_url: str, max_bytes: int):
    """Return (mime, bytes) for a base64 image data URL, or raise ValueError."""
    m = _DATA_URL_RE.match(data_url or "")
    if not m:
        raise ValueError("Unsupported image format")
    mime = m.group(1)
    try:
        raw = base64.b64decode(re.sub(r"\s", "", m.group(2)), validate=True)
    except Exception:
        raise ValueError("Image data is corrupted")
    if len(raw) > max_bytes:
        raise ValueError("Image is too large")
    sig_ok = (
        (mime == "image/jpeg" and raw[:3] == b"\xff\xd8\xff")
        or (mime == "image/png" and raw[:8] == b"\x89PNG\r\n\x1a\n")
        or (mime == "image/webp" and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
    )
    if not sig_ok:
        raise ValueError("Image content does not match its type")
    return mime, raw


def normalize_submission(body: dict) -> dict:
    """Clamp and clean a client submission into the stored shape. Raises
    ValueError with a user-facing message for missing required fields."""
    c = body.get("client") or {}
    client = {
        "name": _clean(c.get("name"), 120),
        "phone": _clean(c.get("phone"), 40),
        "email": _clean(c.get("email"), 200),
        "project_type": _clean(c.get("project_type"), 60),
        "address": _clean(c.get("address"), 240),
        "floors": _clean(c.get("floors"), 40),
        "year_built": _clean(c.get("year_built"), 12),
    }
    for key, label in (("name", "Full name"), ("phone", "Phone number"),
                       ("project_type", "Project type"), ("address", "Property address")):
        if not client[key]:
            raise ValueError(f"{label} is required")
    if client["project_type"] not in PROJECT_TYPES:
        client["project_type"] = "Other"
    if client["floors"] and client["floors"] not in FLOOR_SCOPES:
        client["floors"] = ""

    front = _clean(body.get("front_faces"), 4).upper()
    front = front if front in DIRECTIONS else ""

    walls = []
    for i, w in enumerate((body.get("walls") or [])[:MAX_WALLS]):
        facing = _clean(w.get("facing"), 4).upper()
        walls.append({
            "label": _clean(w.get("label"), 6) or chr(65 + i % 26),
            "feet": int(_num(w.get("feet"), 0, 1000)),
            "inches": round(_num(w.get("inches"), 0, 11.99), 2),
            "facing": facing if facing in DIRECTIONS else "",
        })

    rooms = []
    for i, r in enumerate((body.get("rooms") or [])[:MAX_ROOMS]):
        pos = _clean(r.get("position"), 20)
        floor = _clean(r.get("floor"), 20)
        rooms.append({
            "name": _clean(r.get("name"), 60) or f"Room {i + 1}",
            "length_ft": int(_num(r.get("length_ft"), 0, 500)),
            "length_in": round(_num(r.get("length_in"), 0, 11.99), 2),
            "width_ft": int(_num(r.get("width_ft"), 0, 500)),
            "width_in": round(_num(r.get("width_in"), 0, 11.99), 2),
            "floor": floor if floor in ROOM_FLOORS else "",
            "position": pos if pos in POSITIONS else "",
            "note": _clean(r.get("note"), 200),
        })

    measured_walls = [w for w in walls if to_feet(w["feet"], w["inches"]) > 0]
    measured_rooms = [r for r in rooms if to_feet(r["length_ft"], r["length_in"]) > 0
                      and to_feet(r["width_ft"], r["width_in"]) > 0]
    if len(measured_walls) < 3 and not measured_rooms:
        raise ValueError("Add at least three exterior wall lengths or one room with length and width")

    return {
        "client": client,
        "front_faces": front,
        "walls": walls,
        "rooms": rooms,
        "fixed_elements": _clean(body.get("fixed_elements"), 3000),
        "ceiling_heights": _clean(body.get("ceiling_heights"), 500),
        "comments": _clean(body.get("comments"), 3000),
    }


# ---------- Geometry (mirrors the on-screen sketch) ----------

def outline(walls):
    """Walk the exterior clockwise. A wall facing direction D is walked at D
    minus 90 degrees (a north-facing wall runs west to east). Returns points
    in feet (y up), skipping walls with no length or no facing."""
    x = y = 0.0
    pts = [(0.0, 0.0)]
    for w in walls:
        length = to_feet(w["feet"], w["inches"])
        if length <= 0 or w["facing"] not in DIRECTIONS:
            continue
        ang = math.radians(DIRECTIONS[w["facing"]] - 90)
        x += length * math.cos(ang)
        y += length * math.sin(ang)
        pts.append((x, y))
    return pts


def measurements_summary(sub: dict) -> dict:
    walls = sub["walls"]
    perimeter = sum(to_feet(w["feet"], w["inches"]) for w in walls)
    pts = outline(walls)
    gap = math.hypot(pts[-1][0], pts[-1][1]) if len(pts) > 1 else None
    missing_dir = [w["label"] for w in walls if to_feet(w["feet"], w["inches"]) > 0 and not w["facing"]]
    area = None
    if len(pts) >= 4 and gap is not None and gap <= 0.5:
        s = 0.0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            s += x1 * y2 - x2 * y1
        area = abs(s) / 2.0
    room_rows = []
    for r in sub["rooms"]:
        L = to_feet(r["length_ft"], r["length_in"])
        W = to_feet(r["width_ft"], r["width_in"])
        room_rows.append({**r, "length": L, "width": W, "area": L * W})
    return {
        "perimeter": perimeter,
        "closure_gap": gap,
        "closed": gap is not None and gap <= 0.5 and len(pts) >= 4,
        "footprint_area": area,
        "walls_missing_direction": missing_dir,
        "rooms": room_rows,
        "rooms_area": sum(r["area"] for r in room_rows),
    }


# ---------- Email ----------

def _e(s) -> str:
    return html_mod.escape(str(s or ""))


def render_submission_email(*, submission_id: int, reference: str, sub: dict, summary: dict,
                            account_email: str, photo_labels: list[str], sketch_count: int) -> str:
    c = sub["client"]
    navy, gold = "#031158", "#daa61f"
    td = "padding:6px 10px;border-bottom:1px solid #e5e7eb;font-size:14px;"
    th = td + f"text-align:left;color:{navy};font-weight:700;background:#f3f5fc;"

    def kv(label, value):
        return f'<tr><td style="{td}color:#6b7280;width:38%">{_e(label)}</td><td style="{td}">{_e(value) or "&mdash;"}</td></tr>'

    client_rows = "".join([
        kv("Full name", c["name"]), kv("Phone", c["phone"]), kv("Email (entered)", c["email"]),
        kv("Account email", account_email), kv("Project type", c["project_type"]),
        kv("Property address", c["address"]), kv("Floor(s) being renovated", c["floors"]),
        kv("Approx. year built", c["year_built"]), kv("Front of building faces", sub["front_faces"]),
    ])
    wall_rows = "".join(
        f'<tr><td style="{td}font-weight:700">{_e(w["label"])}</td>'
        f'<td style="{td}">{_e(fmt_ft_in(to_feet(w["feet"], w["inches"])))}</td>'
        f'<td style="{td}">{_e(w["facing"]) or "&mdash;"}</td></tr>'
        for w in sub["walls"]
    ) or f'<tr><td colspan="3" style="{td}">No exterior walls entered</td></tr>'
    room_rows = "".join(
        f'<tr><td style="{td}font-weight:700">{_e(r["name"])}</td>'
        f'<td style="{td}">{_e(fmt_ft_in(r["length"]))} &times; {_e(fmt_ft_in(r["width"]))}</td>'
        f'<td style="{td}">{r["area"]:,.0f} sq ft</td>'
        f'<td style="{td}">{_e(r["floor"]) or "&mdash;"}</td>'
        f'<td style="{td}">{_e(POSITIONS.get(r["position"], "")) or "&mdash;"}</td>'
        f'<td style="{td}">{_e(r["note"])}</td></tr>'
        for r in summary["rooms"]
    ) or f'<tr><td colspan="6" style="{td}">No rooms entered</td></tr>'

    if summary["closure_gap"] is None:
        closure = "No exterior outline (walls need lengths and directions)."
    elif summary["closed"]:
        closure = "Outline closes within 6 inches."
    else:
        closure = f"Outline does NOT close: ends {fmt_ft_in(summary['closure_gap'])} from the starting corner. Verify wall lengths/directions with the client."
    if summary["walls_missing_direction"]:
        closure += " Walls missing a direction: " + ", ".join(summary["walls_missing_direction"]) + "."
    area = f'{summary["footprint_area"]:,.0f} sq ft' if summary["footprint_area"] is not None else "Not available"

    photos = ", ".join(_e(p) for p in photo_labels) or "None attached"

    def block(title, text):
        return (f'<h3 style="color:{navy};font-family:Georgia,serif;margin:22px 0 6px">{_e(title)}</h3>'
                f'<p style="white-space:pre-wrap;margin:0;font-size:14px;color:#374151">{_e(text) or "&mdash;"}</p>')

    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:720px;margin:0 auto;color:#212326">
  <div style="background:{navy};color:#fff;padding:18px 22px;border-radius:10px 10px 0 0">
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:{gold};font-weight:700">Room Planner Submission</div>
    <div style="font-size:20px;font-weight:700;margin-top:4px">{_e(c["name"])} &mdash; {_e(c["address"])}</div>
    <div style="font-size:13px;opacity:.8;margin-top:4px">Reference {_e(reference)} &middot; #{submission_id}</div>
  </div>
  <div style="border:1px solid #e5e7eb;border-top:0;padding:18px 22px;border-radius:0 0 10px 10px">
    <p style="font-size:13px;color:#6b7280;margin:0 0 12px">Client-entered measurements. The attached sketch is a simple overview generated from these numbers; verify before producing drawings.</p>
    <table style="width:100%;border-collapse:collapse">{client_rows}</table>
    <h3 style="color:{navy};font-family:Georgia,serif;margin:22px 0 6px">Exterior walls</h3>
    <table style="width:100%;border-collapse:collapse"><tr><th style="{th}">Wall</th><th style="{th}">Length</th><th style="{th}">Facing</th></tr>{wall_rows}</table>
    <p style="font-size:13px;margin:8px 0 0"><b>Perimeter:</b> {_e(fmt_ft_in(summary["perimeter"]))} &middot; <b>Footprint:</b> {_e(area)}<br>{_e(closure)}</p>
    <h3 style="color:{navy};font-family:Georgia,serif;margin:22px 0 6px">Rooms</h3>
    <table style="width:100%;border-collapse:collapse"><tr><th style="{th}">Room</th><th style="{th}">Size</th><th style="{th}">Area</th><th style="{th}">Floor</th><th style="{th}">Position</th><th style="{th}">Note</th></tr>{room_rows}</table>
    <p style="font-size:13px;margin:8px 0 0"><b>Total room area:</b> {summary["rooms_area"]:,.0f} sq ft</p>
    {block("Fixed elements & special notes", sub["fixed_elements"])}
    {block("Ceiling heights", sub["ceiling_heights"])}
    {block("Additional comments", sub["comments"])}
    <h3 style="color:{navy};font-family:Georgia,serif;margin:22px 0 6px">Attachments</h3>
    <p style="font-size:14px;margin:0">Overview sketch{"es" if sketch_count != 1 else ""}: {sketch_count} &middot; Photos: {photos}</p>
  </div>
</div>"""


def render_client_confirmation_email(*, name: str, reference: str, address: str) -> str:
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#212326">
  <h2 style="color:#031158;font-family:Georgia,serif">We received your measurements</h2>
  <p>Hi {_e(name) or "there"},</p>
  <p>Your floor plan measurements for <b>{_e(address)}</b> were submitted to the Tru Life Properties back office. Our team will review them and reach out if anything needs a second look.</p>
  <p style="color:#6b7280;font-size:13px">Reference: {_e(reference)}</p>
  <p>&mdash; Tru Life Properties</p>
</div>"""
