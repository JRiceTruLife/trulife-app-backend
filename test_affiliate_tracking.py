"""Run: python -m unittest -v test_affiliate_tracking. Never uses production data."""
import os
import tempfile
import unittest
import json
import time
import hmac
import hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ["TRULIFE_DB_PATH"] = _tmp.name + "/test.db"
os.environ["TRULIFE_ENV"] = "development"

from fastapi.testclient import TestClient
import api_server as api
import affiliate_tracking as tracking


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        for table in ("affiliate_sales", "purchase_ledger", "payment_refunds", "guide_purchases",
                      "checkout_sessions", "users", "affiliates"):
            api.db.execute("DELETE FROM " + table)
        for slug, email, admin in (("alice", "alice@example.test", 0), ("bob", "bob@example.test", 0),
                                   ("admin", "admin@example.test", 1)):
            api.db.execute(
                "INSERT INTO affiliates(slug,name,email,default_rate,is_admin) VALUES (?,?,?,10,?)",
                (slug, slug.title(), email, admin),
            )
        api.db.commit()
        api._rate_buckets.clear() if hasattr(api, "_rate_buckets") else None
        self.a, self.b, self.admin = [api.db.execute("SELECT * FROM affiliates WHERE slug=?", (s,)).fetchone() for s in ("alice", "bob", "admin")]
        self.ah = {"Authorization": "Bearer " + api.make_affiliate_token(self.a["id"])}
        self.bh = {"Authorization": "Bearer " + api.make_affiliate_token(self.b["id"])}
        self.mh = {"Authorization": "Bearer " + api.make_affiliate_token(self.admin["id"])}

    def signup(self, email="buyer@example.test", ref="alice"):
        with patch.object(api, "enforce_rate_limit"):
            return self.client.post("/api/auth/signup", json={
                "name": "Buyer Example", "email": email, "password": "OnlyATest123!", "ref": ref})

    def buyer(self, email="buyer@example.test", ref="alice"):
        r = self.signup(email, ref)
        self.assertEqual(r.status_code, 201, r.text)
        return api.db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    def checkout(self, user, sid="cs_test_one", gid="linder"):
        api.db.execute("INSERT INTO checkout_sessions(stripe_session_id,user_id,guide_id) VALUES (?,?,?)", (sid, user["id"], gid))
        api.db.commit()
        return {"id": sid, "payment_status": "paid", "amount_total": 49700, "currency": "usd", "payment_intent": "pi_" + sid}

    def fulfill(self, session):
        with patch.object(api.email_service, "send_email") as send:
            result = api.fulfill_checkout_session(session)
        return result, send

    def test_referral_isolation(self):
        self.buyer()
        self.buyer("other@example.test", "bob")
        a = self.client.get("/api/affiliate/clients?affiliate_id=" + str(self.b["id"]), headers=self.ah).json()
        b = self.client.get("/api/affiliate/clients", headers=self.bh).json()
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["affiliate_id"], self.a["id"])
        self.assertNotEqual(a[0]["email"], "buyer@example.test")
        self.assertEqual(b[0]["affiliate_id"], self.b["id"])
        links = self.client.get("/api/affiliate/overview", headers=self.ah).json()["affiliates"]
        self.assertEqual([x["slug"] for x in links], ["alice"])
        self.assertIn("app.trulifeproperties.com/?ref=alice", links[0]["tracking_link"])

    def test_invalid_and_inactive_ref(self):
        self.assertEqual(self.signup(ref="not-real").status_code, 400)
        api.db.execute("UPDATE affiliates SET active=0 WHERE id=?", (self.a["id"],))
        api.db.commit()
        self.assertEqual(self.signup().status_code, 400)
        self.assertEqual(self.signup(ref="").status_code, 201)

    def test_self_referral(self):
        user = self.buyer("alice@example.test")
        self.assertIsNone(user["referred_by_affiliate_id"])

    def test_existing_account_not_reassigned(self):
        user = self.buyer()
        self.assertEqual(self.signup(ref="bob").status_code, 409)
        r = self.client.post("/api/affiliate/clients/assign", headers=self.mh,
                             json={"email": user["email"], "affiliate_id": self.b["id"]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.post("/api/affiliate/clients/assign", headers=self.bh,
                                         json={"email": user["email"], "affiliate_id": self.b["id"]}).status_code, 403)

    def test_later_multiple_products_and_idempotency(self):
        user = self.buyer()
        session = self.checkout(user)
        result, send = self.fulfill(session)
        self.assertTrue(result["fulfilled"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[1].kwargs["to"], api.ACCOUNTING_EMAIL)
        self.assertNotEqual(send.call_args_list[1].kwargs["to"], "info@trulifeproperties.com")
        self.assertFalse(self.fulfill(session)[0]["fulfilled"])
        self.fulfill(self.checkout(user, "cs_test_two", "five-step-method"))
        sales = self.client.get("/api/affiliate/sales", headers=self.ah).json()
        self.assertEqual(len(sales), 2)
        self.assertEqual({s["product"] for s in sales}, {"Design Guide", "5 Step Method Module"})
        self.assertEqual(self.client.get("/api/affiliate/sales", headers=self.bh).json(), [])
        self.assertEqual(self.client.get("/api/affiliate/clients", headers=self.ah).json()[0]["purchase_count"], 2)

    def test_unpaid_unknown_never_credit(self):
        session = self.checkout(self.buyer())
        session["payment_status"] = "unpaid"
        self.assertFalse(self.fulfill(session)[0]["fulfilled"])
        session.update(id="unknown", payment_status="paid")
        self.assertFalse(self.fulfill(session)[0]["fulfilled"])
        self.assertEqual(api.db.execute("SELECT COUNT(*) FROM affiliate_sales").fetchone()[0], 0)

    def test_atomic_rollback_and_retry(self):
        session = self.checkout(self.buyer())
        with patch.object(tracking, "record_verified_purchase", side_effect=RuntimeError("test outage")):
            with self.assertRaises(RuntimeError):
                self.fulfill(session)
        self.assertEqual(api.db.execute("SELECT status FROM checkout_sessions").fetchone()[0], "pending")
        self.assertEqual(api.db.execute("SELECT COUNT(*) FROM guide_purchases").fetchone()[0], 0)
        self.assertTrue(self.fulfill(session)[0]["fulfilled"])

    def test_concurrent_duplicate_delivery(self):
        session = self.checkout(self.buyer())
        with patch.object(api.email_service, "send_email"), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: api.fulfill_checkout_session(session), range(4)))
        self.assertEqual(sum(r["fulfilled"] for r in results), 1)
        self.assertEqual(api.db.execute("SELECT COUNT(*) FROM affiliate_sales").fetchone()[0], 1)

    def test_commission_snapshot_and_refunds(self):
        session = self.checkout(self.buyer())
        self.fulfill(session)
        api.db.execute("UPDATE affiliates SET default_rate=80 WHERE id=?", (self.a["id"],))
        api.db.commit()
        for cents in (10000, 10000, 5000):
            with api.db:
                tracking.apply_verified_refund(api.db, payment_reference=session["payment_intent"], refunded_cents=cents)
        row = self.client.get("/api/affiliate/sales", headers=self.ah).json()[0]
        self.assertEqual(row["rate"], 10)
        self.assertEqual(row["refunded_amount"], 100)
        self.assertAlmostEqual(row["commission"], 39.7)
        with api.db:
            tracking.apply_verified_refund(api.db, payment_reference=session["payment_intent"], refunded_cents=49700)
        self.assertEqual(self.client.get("/api/affiliate/overview", headers=self.ah).json()["totals"]["commission"], 0)

    def test_refund_before_purchase(self):
        session = self.checkout(self.buyer())
        with api.db:
            tracking.apply_verified_refund(api.db, payment_reference=session["payment_intent"], refunded_cents=49700)
        self.fulfill(session)
        self.assertEqual(self.client.get("/api/affiliate/sales", headers=self.ah).json()[0]["commission"], 0)

    def test_generic_product_adapter(self):
        user = self.buyer()
        with api.db:
            tracking.record_verified_purchase(api.db, provider="verified-test-adapter", transaction_id="order-coaching",
                user_id=user["id"], product_id="coaching-session", product_name="Coaching session",
                category="Coaching", amount_cents=25000)
        row = self.client.get("/api/affiliate/clients", headers=self.ah).json()[0]
        self.assertEqual(row["products"], ["Coaching session"])
        self.assertEqual(row["total_spent"], 250)

    def test_no_fake_sales_or_receipt_deletion(self):
        payload = {"affiliate_id": self.a["id"], "product": "Design Guide", "amount": 999}
        self.assertEqual(self.client.post("/api/affiliate/sales", headers=self.ah, json=payload).status_code, 403)
        self.fulfill(self.checkout(self.buyer()))
        sid = api.db.execute("SELECT id FROM affiliate_sales").fetchone()[0]
        self.assertEqual(self.client.delete(f"/api/affiliate/sales/{sid}", headers=self.ah).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/affiliate/sales/{sid}", headers=self.mh).status_code, 409)
        self.assertEqual(self.client.get("/api/affiliate/list", headers=self.ah).status_code, 403)

    def test_auth_namespaces_and_unsigned_webhook(self):
        user = self.buyer()
        uh = {"Authorization": "Bearer " + api.make_token(user["id"])}
        for path in ("/api/affiliate/clients", "/api/affiliate/sales", "/api/affiliate/overview"):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get(path, headers=uh).status_code, 401)
        with patch.object(api, "STRIPE_ENABLED", True), patch.object(api, "STRIPE_WEBHOOK_SECRET", "test-not-live"):
            self.assertEqual(self.client.post("/api/stripe/webhook", content=b'{}', headers={"Stripe-Signature": "invalid"}).status_code, 400)

    def test_migration_repeatable(self):
        self.fulfill(self.checkout(self.buyer()))
        tracking.migrate(api.db)
        tracking.migrate(api.db)
        self.assertEqual(api.db.execute("SELECT COUNT(*) FROM affiliate_sales").fetchone()[0], 1)

    def test_signed_webhook_purchase_and_refund(self):
        session = self.checkout(self.buyer())
        session["object"] = "checkout.session"
        def deliver(event_type, obj):
            payload = json.dumps({"id": "evt_qa", "object": "event", "type": event_type, "data": {"object": obj}})
            now = str(int(time.time()))
            signature = hmac.new(b"test-signing-only", (now + "." + payload).encode(), hashlib.sha256).hexdigest()
            return self.client.post("/api/stripe/webhook", content=payload,
                                    headers={"Stripe-Signature": f"t={now},v1={signature}"})
        with patch.object(api, "STRIPE_ENABLED", True), patch.object(api, "STRIPE_WEBHOOK_SECRET", "test-signing-only"), patch.object(api.email_service, "send_email"):
            self.assertEqual(deliver("checkout.session.completed", session).status_code, 200)
            self.assertEqual(deliver("checkout.session.completed", session).status_code, 200)
            self.assertEqual(deliver("charge.refunded", {"object": "charge", "id": "ch_qa",
                             "payment_intent": session["payment_intent"], "amount_refunded": 49700}).status_code, 200)
        self.assertEqual(api.db.execute("SELECT COUNT(*) FROM affiliate_sales").fetchone()[0], 1)
        self.assertEqual(self.client.get("/api/affiliate/sales", headers=self.ah).json()[0]["commission"], 0)


if __name__ == "__main__":
    unittest.main()
