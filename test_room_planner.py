"""Room Planner: architecture-drawing-gated access, fixed back-office recipient,
validation, photo checks, idempotency, geometry and account deletion."""
import base64
import os
import tempfile
import unittest
import uuid

_tmp = tempfile.TemporaryDirectory()
os.environ["TRULIFE_DB_PATH"] = _tmp.name + "/test.db"
os.environ["TRULIFE_ENV"] = "development"
os.environ["TRULIFE_STAFF_EMAILS"] = "media@trulifeproperties.com"

from fastapi.testclient import TestClient
import api_server
import email_service
import room_planner

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 200).decode()
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 100).decode()

SENT = []


def fake_send(to, subject, html, attachments=None):
    SENT.append({"to": to, "subject": subject, "html": html, "attachments": attachments or []})
    return True


def payload(**over):
    body = {
        "client_ref": uuid.uuid4().hex,
        "client": {"name": "Maria Johnson", "phone": "555-000-0000", "email": "maria@example.com",
                   "project_type": "Kitchen remodel", "address": "123 Main St, Orlando FL",
                   "floors": "1st floor", "year_built": "1985"},
        "front_faces": "S",
        "walls": [
            {"label": "A", "feet": 40, "inches": 0, "facing": "W"},
            {"label": "B", "feet": 30, "inches": 0, "facing": "N"},
            {"label": "C", "feet": 40, "inches": 0, "facing": "E"},
            {"label": "D", "feet": 30, "inches": 0, "facing": "S"},
        ],
        "rooms": [{"name": "Kitchen", "length_ft": 12, "length_in": 6, "width_ft": 10, "width_in": 0,
                   "floor": "1st floor", "position": "back-left", "note": "open to living"}],
        "fixed_elements": "Load-bearing wall between kitchen and living",
        "ceiling_heights": "9 ft", "comments": "",
        "photos": [{"label": "Kitchen", "data_url": JPEG}],
        "sketches": [{"floor": "1st floor", "png": PNG}],
    }
    body.update(over)
    return body


class RoomPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        email_service.send_email = fake_send
        cls.client = TestClient(api_server.app)

    def setUp(self):
        SENT.clear()
        api_server._rate_buckets.clear()

    def signup(self, email):
        res = self.client.post("/api/auth/signup", json={"email": email, "password": "RoomPlan!2026x", "name": "Test"})
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["user"]["id"], {"Authorization": "Bearer " + res.json()["token"]}

    def buy(self, user_id, cents=150000, refunded=0, provider_ref=None,
            product=("architecture-plan", "Architecture Plan", "Architecture Plan")):
        api_server.db.execute(
            """INSERT INTO purchase_ledger (provider, transaction_id, user_id, product_id, product_name,
                   category, amount_cents, refunded_cents, payment_reference)
               VALUES ('stripe', ?, ?, ?, ?, ?, ?, ?, ?)""",
            [uuid.uuid4().hex, user_id, *product, cents, refunded, provider_ref])
        api_server.db.commit()

    _owner_token = None

    def owner_headers(self):
        cls = type(self)
        if not cls._owner_token:
            res = self.client.post("/api/auth/login", json={"email": api_server.OWNER_EMAIL, "password": api_server.OWNER_PASSWORD})
            self.assertEqual(res.status_code, 200, res.text)
            cls._owner_token = res.json()["token"]
        return {"Authorization": "Bearer " + cls._owner_token}

    def test_other_purchases_do_not_unlock(self):
        uid, h = self.signup("guidebuyer@example.com")
        self.buy(uid, cents=4900, product=("five-step-method", "5 Step Method", "5 Step Method Module"))
        self.buy(uid, cents=49700, product=("linder", "Modern Farmhouse", "Design Guide"))
        api_server.db.execute(
            "INSERT INTO guide_purchases (user_id, guide_id, unlocked_by, stripe_payment_intent, amount_paid) VALUES (?, 'fix-flip', 'stripe', 'pi_course', 499)", [uid])
        api_server.db.commit()
        res = self.client.get("/api/room-planner/access", headers=h).json()
        self.assertFalse(res["allowed"])
        self.assertEqual(res["reason"], "architecture_required")
        self.assertEqual(self.client.post("/api/room-planner/submit", json=payload(), headers=h).status_code, 403)

    def test_architecture_purchase_unlocks(self):
        uid, h = self.signup("archbuyer@example.com")
        self.buy(uid, product=("architecture-custom", "Architecture Custom", "Architecture Custom"))
        res = self.client.get("/api/room-planner/access", headers=h).json()
        self.assertTrue(res["allowed"])
        self.assertEqual(res["reason"], "purchase")
        self.assertFalse(res["can_grant"])

    def test_owner_grant_for_offline_payment(self):
        uid, h = self.signup("invoiced@example.com")
        oh = self.owner_headers()
        self.assertFalse(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])
        # members cannot grant
        self.assertEqual(self.client.post("/api/room-planner/grants", json={"email": "invoiced@example.com"}, headers=h).status_code, 403)
        self.assertEqual(self.client.post("/api/room-planner/grants", json={"email": "nobody@example.com"}, headers=oh).status_code, 404)
        res = self.client.post("/api/room-planner/grants", json={"email": "Invoiced@Example.com", "note": "Paid invoice #12"}, headers=oh)
        self.assertEqual(res.status_code, 201, res.text)
        acc = self.client.get("/api/room-planner/access", headers=h).json()
        self.assertTrue(acc["allowed"]); self.assertEqual(acc["reason"], "granted")
        self.assertIn("invoiced@example.com", [g["email"] for g in self.client.get("/api/room-planner/grants", headers=oh).json()])
        self.assertEqual(self.client.delete(f"/api/room-planner/grants/{uid}", headers=oh).status_code, 200)
        self.assertFalse(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])
        self.assertEqual(self.client.delete(f"/api/room-planner/grants/{uid}", headers=oh).status_code, 404)

    def test_no_purchase_is_locked(self):
        _, h = self.signup("nobuy@example.com")
        self.assertFalse(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])
        res = self.client.post("/api/room-planner/submit", json=payload(), headers=h)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(SENT, [])

    def test_unauthenticated_rejected(self):
        self.assertEqual(self.client.get("/api/room-planner/access").status_code, 401)

    def test_admin_comp_is_not_a_purchase(self):
        uid, h = self.signup("comped@example.com")
        api_server.db.execute("INSERT INTO guide_purchases (user_id, guide_id, unlocked_by) VALUES (?, 'architecture-plan', 'admin')", [uid])
        api_server.db.commit()
        self.assertFalse(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])

    def test_full_refund_revokes_partial_keeps(self):
        uid, h = self.signup("refund@example.com")
        self.buy(uid, cents=150000, refunded=150000)
        self.assertFalse(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])
        self.buy(uid, cents=150000, refunded=10000)
        self.assertTrue(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])

    def test_stripe_catalog_architecture_purchase_counts(self):
        uid, h = self.signup("legacy@example.com")
        api_server.db.execute(
            "INSERT INTO guide_purchases (user_id, guide_id, unlocked_by, stripe_payment_intent, amount_paid) VALUES (?, 'architecture-plan', 'stripe', 'pi_legacy', 1500)", [uid])
        api_server.db.commit()
        self.assertTrue(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])
        api_server.db.execute("INSERT INTO payment_refunds (payment_reference, refunded_cents) VALUES ('pi_legacy', 150000)")
        api_server.db.commit()
        self.assertFalse(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])

    def test_owner_and_staff_allowed(self):
        self.assertTrue(self.client.get("/api/room-planner/access", headers=self.owner_headers()).json()["allowed"])
        _, h = self.signup("media@trulifeproperties.com")
        self.assertTrue(self.client.get("/api/room-planner/access", headers=h).json()["allowed"])

    def test_submit_goes_only_to_backoffice(self):
        uid, h = self.signup("buyer@example.com")
        self.buy(uid)
        body = payload()
        body["recipient"] = "attacker@example.com"  # ignored: not part of the schema
        res = self.client.post("/api/room-planner/submit", json=body, headers=h)
        self.assertEqual(res.status_code, 201, res.text)
        self.assertTrue(res.json()["email_sent"])
        back = [m for m in SENT if m["attachments"]]
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["to"], "admin@trulifefiles.com")
        self.assertNotIn("attacker@example.com", [m["to"] for m in SENT])
        names = [a["filename"] for a in back[0]["attachments"]]
        self.assertTrue(any(n.endswith(".png") for n in names) and any(n.endswith(".jpg") for n in names))
        self.assertIn("Kitchen", back[0]["html"])
        self.assertIn("1,200 sq ft", back[0]["html"])  # 40 x 30 footprint computed server-side
        # client gets a confirmation without the measurements attached
        conf = [m for m in SENT if m["to"] == "buyer@example.com"]
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0]["attachments"], [])

    def test_duplicate_submit_is_idempotent(self):
        uid, h = self.signup("dupe@example.com")
        self.buy(uid)
        body = payload()
        a = self.client.post("/api/room-planner/submit", json=body, headers=h).json()
        SENT.clear()
        b = self.client.post("/api/room-planner/submit", json=body, headers=h).json()
        self.assertEqual(a["reference"], b["reference"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(SENT, [])

    def test_validation(self):
        uid, h = self.signup("valid@example.com")
        self.buy(uid)
        bad = payload(client={"name": "", "phone": "1", "project_type": "Other", "address": "x"})
        self.assertEqual(self.client.post("/api/room-planner/submit", json=bad, headers=h).status_code, 400)
        empty = payload(walls=[], rooms=[])
        self.assertEqual(self.client.post("/api/room-planner/submit", json=empty, headers=h).status_code, 400)
        fake = payload(photos=[{"label": "x", "data_url": "data:image/jpeg;base64," + base64.b64encode(b"<html>").decode()}])
        self.assertEqual(self.client.post("/api/room-planner/submit", json=fake, headers=h).status_code, 400)
        svg = payload(photos=[{"label": "x", "data_url": "data:image/svg+xml;base64,PHN2Zz4="}])
        self.assertEqual(self.client.post("/api/room-planner/submit", json=svg, headers=h).status_code, 400)

    def test_geometry(self):
        sub = room_planner.normalize_submission(payload())
        s = room_planner.measurements_summary(sub)
        self.assertTrue(s["closed"])
        self.assertAlmostEqual(s["footprint_area"], 1200, places=3)
        self.assertAlmostEqual(s["perimeter"], 140, places=3)
        self.assertAlmostEqual(s["rooms"][0]["area"], 125, places=3)
        open_walls = payload(walls=[{"label": "A", "feet": 40, "facing": "W"}, {"label": "B", "feet": 30, "facing": "N"},
                                    {"label": "C", "feet": 35, "facing": "E"}])
        s2 = room_planner.measurements_summary(room_planner.normalize_submission(open_walls))
        self.assertFalse(s2["closed"])
        self.assertIsNone(s2["footprint_area"])

    def test_backoffice_list_owner_only(self):
        _, h = self.signup("member-list@example.com")
        self.assertEqual(self.client.get("/api/room-planner/submissions", headers=h).status_code, 403)
        res = self.client.get("/api/room-planner/submissions", headers=self.owner_headers())
        self.assertEqual(res.status_code, 200)

    def test_account_deletion_removes_stored_copies(self):
        uid, h = self.signup("deleteme@example.com")
        self.buy(uid)
        sid = self.client.post("/api/room-planner/submit", json=payload(), headers=h).json()["id"]
        folder = os.path.join(api_server._RP_UPLOAD_DIR, str(sid))
        self.assertTrue(os.path.isdir(folder))
        res = self.client.post("/api/auth/delete-account", json={"current_password": "RoomPlan!2026x", "confirmation": "DELETE"}, headers=h)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse(os.path.isdir(folder))
        self.assertIsNone(api_server.db.execute("SELECT 1 FROM room_plan_submissions WHERE id=?", [sid]).fetchone())


if __name__ == "__main__":
    unittest.main()
