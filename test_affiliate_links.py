"""Campaign links, per-affiliate rate overrides and Discord hub residuals.
Run: python -m unittest -v test_affiliate_links. Never uses production data."""
import os
import tempfile
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ["TRULIFE_DB_PATH"] = _tmp.name + "/test.db"
os.environ["TRULIFE_ENV"] = "development"
os.environ["TRULIFE_HUB_SHARED_SECRET"] = "hub-test-secret"

from fastapi.testclient import TestClient
import api_server as api
import affiliate_links as links
import affiliate_tracking as tracking

HUB = {"X-TruLife-Hub-Key": "hub-test-secret"}


class CampaignLinkTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        for table in ("affiliate_sales", "purchase_ledger", "payment_refunds", "guide_purchases",
                      "checkout_sessions", "affiliate_clicks", "affiliate_links", "affiliate_payouts",
                      "password_reset_codes", "users", "affiliates"):
            api.db.execute("DELETE FROM " + table)
        for slug, email, admin in (("alice", "alice@example.test", 0), ("bob", "bob@example.test", 0),
                                   ("admin", "admin@example.test", 1)):
            api.db.execute("INSERT INTO affiliates(slug,name,email,is_admin) VALUES (?,?,?,?)",
                           (slug, slug.title(), email, admin))
        api.db.commit()
        api._rate_buckets.clear()
        self.a, self.b, self.admin = [api.db.execute("SELECT * FROM affiliates WHERE slug=?", (s,)).fetchone()
                                      for s in ("alice", "bob", "admin")]
        self.ah = {"Authorization": "Bearer " + api.make_affiliate_token(self.a["id"])}
        self.bh = {"Authorization": "Bearer " + api.make_affiliate_token(self.b["id"])}
        self.mh = {"Authorization": "Bearer " + api.make_affiliate_token(self.admin["id"])}

    # ----------------------------------------------------------- helpers
    def signup(self, email, ref):
        with patch.object(api, "enforce_rate_limit"):
            return self.client.post("/api/auth/signup", json={
                "name": "Buyer Example", "email": email, "password": "OnlyATest123!", "ref": ref})

    def user(self, email):
        return api.db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    def pay(self, user, sid, gid="linder", cents=49700):
        api.db.execute("INSERT INTO checkout_sessions(stripe_session_id,user_id,guide_id) VALUES (?,?,?)",
                       (sid, user["id"], gid))
        api.db.commit()
        with patch.object(api.email_service, "send_email"):
            return api.fulfill_checkout_session({"id": sid, "payment_status": "paid", "amount_total": cents,
                                                 "currency": "usd", "payment_intent": "pi_" + sid})

    def make_link(self, headers=None, **body):
        payload = {"label": "Fix & Flip reel 1", "product": "Fix & Flip Module"}
        payload.update(body)
        return self.client.post("/api/affiliate/links", json=payload, headers=headers or self.ah)

    # ------------------------------------------------------------- tests
    def test_create_link_namespaced_and_opens_product(self):
        r = self.make_link()
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["code"], "alice-fix-flip-reel-1")
        self.assertIn("/?ref=alice-fix-flip-reel-1&open=brick", r.json()["url"])
        # Same label again -> still unique.
        r2 = self.make_link()
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r2.json()["code"], r.json()["code"])
        self.assertTrue(r2.json()["code"].startswith("alice-fix-flip-reel-1-"))
        # Custom code is forced under the affiliate's slug and can't collide with a slug.
        r3 = self.make_link(label="Budget sheet video", product="5 Step Method Module", code="bob")
        self.assertEqual(r3.status_code, 201)
        self.assertEqual(r3.json()["code"], "alice-bob")
        self.assertIn("&open=five-step", r3.json()["url"])
        self.assertEqual(self.make_link(label="   ").status_code, 400)
        self.assertEqual(self.make_link(product="Not a product").status_code, 400)
        self.assertEqual(self.make_link(code="bad code!").status_code, 400)

    def test_link_click_signup_and_sale_roll_up_to_affiliate(self):
        code = self.make_link().json()["code"]
        code2 = self.make_link(label="Design guide short", product="Design Guide").json()["code"]
        for c in (code, code, code2, "alice"):
            self.assertTrue(self.client.post("/api/affiliate/click", json={"ref": c}).json()["logged"])
        self.assertFalse(self.client.post("/api/affiliate/click", json={"ref": "alice-nope"}).json()["logged"])

        self.assertEqual(self.signup("one@example.test", code).status_code, 201)
        self.assertEqual(self.signup("two@example.test", code2).status_code, 201)
        self.assertEqual(self.signup("three@example.test", "alice").status_code, 201)
        one = self.user("one@example.test")
        self.assertEqual(one["referred_by_affiliate_id"], self.a["id"])
        self.assertIsNotNone(one["referred_link_id"])
        self.assertIsNone(self.user("three@example.test")["referred_link_id"])

        # Client from the Fix & Flip link later buys a design guide AND the course:
        # both still pay Alice and both show under the link that brought them in.
        self.assertTrue(self.pay(one, "cs_1", "linder")["fulfilled"])
        self.assertTrue(self.pay(one, "cs_2", "fix-flip", 49900)["fulfilled"])
        self.pay(self.user("three@example.test"), "cs_3", "five-step-method", 4900)

        data = self.client.get("/api/affiliate/links", headers=self.ah).json()
        self.assertFalse(data["is_admin"])
        self.assertEqual(len(data["affiliates"]), 1)
        card = data["affiliates"][0]
        by_code = {l["code"]: l for l in card["links"]}
        self.assertEqual(by_code[code]["stats"], {"clicks": 2, "signups": 1, "sale_count": 2,
                                                   "revenue": 996.0, "commission": 199.2})
        self.assertEqual(by_code[code2]["stats"]["clicks"], 1)
        self.assertEqual(by_code[code2]["stats"]["signups"], 1)
        self.assertEqual(by_code[code2]["stats"]["sale_count"], 0)
        self.assertEqual(card["main_stats"], {"clicks": 1, "signups": 1, "sale_count": 1,
                                              "revenue": 49.0, "commission": 9.8})
        overview = self.client.get("/api/affiliate/overview", headers=self.ah).json()["affiliates"][0]
        self.assertEqual(overview["click_count"], 4)
        self.assertEqual(overview["client_count"], 3)
        self.assertAlmostEqual(overview["total_sales"], 1045.0)
        # Clients list shows which link brought each client in.
        clients = {c["email"]: c for c in self.client.get("/api/affiliate/clients", headers=self.mh).json()}
        self.assertEqual(clients["one@example.test"]["via_link"], "Fix & Flip reel 1")
        self.assertEqual(clients["three@example.test"]["via_link"], "")

    def test_links_are_private_and_archivable(self):
        link = self.make_link().json()
        self.assertEqual(len(self.client.get("/api/affiliate/links", headers=self.bh).json()["affiliates"][0]["links"]), 0)
        self.assertEqual(self.client.patch(f"/api/affiliate/links/{link['id']}", json={"active": False}, headers=self.bh).status_code, 404)
        # Bob can't create on Alice's behalf; admin can.
        self.assertEqual(self.make_link(headers=self.bh, affiliate_id=self.a["id"]).status_code, 403)
        r = self.make_link(headers=self.mh, affiliate_id=self.a["id"], label="Admin made")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["affiliate_id"], self.a["id"])
        self.assertEqual(len(self.client.get("/api/affiliate/links", headers=self.mh).json()["affiliates"]), 3)
        # Archive: new visitors no longer resolve, history and attribution stay.
        self.signup("kept@example.test", link["code"])
        r = self.client.patch(f"/api/affiliate/links/{link['id']}", json={"active": False, "label": "Old reel"}, headers=self.ah)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["active"])
        self.assertEqual(r.json()["label"], "Old reel")
        self.assertFalse(self.client.post("/api/affiliate/click", json={"ref": link["code"]}).json()["logged"])
        self.assertEqual(self.signup("late@example.test", link["code"]).status_code, 400)
        self.assertEqual(self.user("kept@example.test")["referred_link_id"], link["id"])
        self.assertEqual(r.json()["stats"]["signups"], 1)

    def test_rate_override_applies_to_future_sales_only(self):
        self.signup("c@example.test", "alice")
        c = self.user("c@example.test")
        self.pay(c, "cs_a")  # 497 @ 20%
        r = self.client.put(f"/api/affiliate/list/{self.a['id']}/rates", json={"default_rate": 30}, headers=self.mh)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["default_rate"], 30)
        self.assertEqual(r.json()["rate_override"], 30)
        self.pay(c, "cs_b", "fix-flip", 49900)  # 499 @ 30%
        with patch.object(api, "enforce_rate_limit"):
            manual = self.client.post("/api/affiliate/sales", json={"product": "Coaching", "amount": 100, "affiliate_id": self.a["id"]}, headers=self.mh)
        self.assertEqual(manual.status_code, 201, manual.text)
        self.assertEqual(manual.json()["rate"], 30)
        sales = {s["product"]: s for s in self.client.get("/api/affiliate/sales", headers=self.ah).json()}
        self.assertEqual(sales["Design Guide"]["rate"], 20)
        self.assertEqual(sales["Fix & Flip Module"]["rate"], 30)
        card = self.client.get("/api/affiliate/overview", headers=self.ah).json()["affiliates"][0]
        self.assertAlmostEqual(card["total_commission"], 497 * 0.2 + 499 * 0.3 + 30)
        # Non-admin can't change rates; clearing goes back to the program rate.
        self.assertEqual(self.client.put(f"/api/affiliate/list/{self.a['id']}/rates", json={"default_rate": 50}, headers=self.ah).status_code, 403)
        r = self.client.put(f"/api/affiliate/list/{self.a['id']}/rates", json={"default_rate": None}, headers=self.mh)
        self.assertIsNone(r.json()["rate_override"])
        self.assertEqual(r.json()["default_rate"], tracking.PROGRAM_RATE)
        self.assertEqual(self.client.put(f"/api/affiliate/list/{self.a['id']}/rates", json={"default_rate": 120}, headers=self.mh).status_code, 422)

    def test_discord_residual_via_client_email(self):
        self.signup("member@example.test", "alice")
        body = {"provider": "discord-hub", "transaction_id": "in_001", "email": "Member@Example.test",
                "amount_cents": 4900, "product": "Discord Membership", "description": "BWBB Investor tier — Nov 2026",
                "occurred_at": "2026-11-14T10:00:00Z"}
        self.assertEqual(self.client.post("/api/affiliate/external-sale", json=body).status_code, 401)
        self.assertEqual(self.client.post("/api/affiliate/external-sale", json=body, headers={"X-TruLife-Hub-Key": "wrong"}).status_code, 401)
        r = self.client.post("/api/affiliate/external-sale", json=body, headers=HUB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["via"], "client_email")
        self.assertEqual(r.json()["commission"], 9.8)
        # Stripe retries the same invoice -> no double credit.
        again = self.client.post("/api/affiliate/external-sale", json=body, headers=HUB).json()
        self.assertTrue(again["duplicate"])
        # Next month is a new invoice -> new residual.
        self.client.post("/api/affiliate/external-sale", json={**body, "transaction_id": "in_002"}, headers=HUB)
        sales = self.client.get("/api/affiliate/sales", headers=self.ah).json()
        self.assertEqual(len(sales), 2)
        self.assertTrue(all(s["product"] == "Discord Membership" and s["source"] == "discord-hub" for s in sales))
        self.assertEqual(sales[0]["sale_date"], "2026-11-14")
        card = self.client.get("/api/affiliate/overview", headers=self.ah).json()["affiliates"][0]
        self.assertAlmostEqual(card["total_commission"], 19.6)
        client = self.client.get("/api/affiliate/clients", headers=self.mh).json()[0]
        self.assertEqual(client["purchase_count"], 2)
        self.assertEqual(client["purchases"][0]["provider"], "discord-hub")

    def test_discord_residual_via_claimed_invite_code(self):
        body = {"provider": "discord-hub", "transaction_id": "in_100", "email": "stranger@example.test",
                "amount_cents": 4900, "hub_affiliate_code": "AbCd1234", "member_label": "stranger#1"}
        r = self.client.post("/api/affiliate/external-sale", json=body, headers=HUB).json()
        self.assertEqual(r, {"credited": False, "duplicate": False, "reason": "no_attribution"})
        self.assertEqual(self.client.put(f"/api/affiliate/list/{self.b['id']}/discord", json={"invite_code": "https://discord.gg/AbCd1234"}, headers=self.bh).status_code, 403)
        r = self.client.put(f"/api/affiliate/list/{self.b['id']}/discord", json={"invite_code": "https://discord.gg/AbCd1234"}, headers=self.mh)
        self.assertEqual(r.json()["discord_invite_code"], "AbCd1234")
        self.assertEqual(self.client.put(f"/api/affiliate/list/{self.a['id']}/discord", json={"invite_code": "abcd1234"}, headers=self.mh).status_code, 409)
        r = self.client.post("/api/affiliate/external-sale", json=body, headers=HUB).json()
        self.assertEqual(r["via"], "discord_invite_code")
        self.assertEqual(r["affiliate_id"], self.b["id"])
        sales = self.client.get("/api/affiliate/sales", headers=self.bh).json()
        self.assertEqual(len(sales), 1)
        self.assertIn("stranger#1", sales[0]["note"])
        self.assertEqual(sales[0]["client"], "")
        # Alice sees nothing of Bob's residual.
        self.assertEqual(self.client.get("/api/affiliate/sales", headers=self.ah).json(), [])
        # Bob's own email as the payer is not a qualified referral.
        r = self.client.post("/api/affiliate/external-sale", json={**body, "transaction_id": "in_101", "email": "bob@example.test"}, headers=HUB).json()
        self.assertEqual(r["reason"], "self_referral")
        self.assertEqual(self.client.post("/api/affiliate/external-sale", json={**body, "provider": "whop"}, headers=HUB).status_code, 400)

    def test_disabled_without_secret(self):
        with patch.object(api, "HUB_SHARED_SECRET", ""):
            r = self.client.post("/api/affiliate/external-sale", json={"provider": "discord-hub", "transaction_id": "x", "amount_cents": 1}, headers=HUB)
        self.assertEqual(r.status_code, 503)

    def test_migration_repeatable(self):
        links.migrate(api.db)
        links.migrate(api.db)
        self.assertIn("referred_link_id", {r["name"] for r in api.db.execute("PRAGMA table_info(users)")})


if __name__ == "__main__":
    unittest.main()
