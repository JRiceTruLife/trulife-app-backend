"""Paywall switch + owner bypass: the flag is environment-driven and the
owner account always sees every guide as purchased."""
import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ["TRULIFE_DB_PATH"] = _tmp.name + "/test.db"
os.environ["TRULIFE_ENV"] = "development"
os.environ["TRULIFE_PAYWALL_ENABLED"] = "true"

from fastapi.testclient import TestClient
import api_server


class PaywallConfigTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api_server.app)

    def test_config_reports_env_driven_flag(self):
        res = self.client.get("/api/config")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["paywall_enabled"])
        # No Stripe keys in the test env → payments are not ready.
        self.assertFalse(body["payments_ready"])

    def test_owner_sees_everything_purchased(self):
        res = self.client.post("/api/auth/login", json={"email": api_server.OWNER_EMAIL, "password": api_server.OWNER_PASSWORD})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["user"]["is_owner"])
        token = res.json()["token"]
        guides = self.client.get("/api/guides", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertTrue(guides and all(g["purchased"] for g in guides))

    def test_regular_member_has_nothing_purchased(self):
        res = self.client.post("/api/auth/signup", json={"email": "member@example.com", "password": "Member!2026x", "name": "Member"})
        self.assertEqual(res.status_code, 201, res.text)
        token = res.json()["token"]
        guides = self.client.get("/api/guides", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertTrue(guides and not any(g["purchased"] for g in guides))
        # Checkout refuses cleanly when Stripe is not configured (no fake grant).
        res = self.client.post("/api/guides/checkout", json={"guide_id": "linder"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 503)

    def _login(self, email, password):
        res = self.client.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def test_catalog_includes_brick_by_brick_course(self):
        ids = {g["id"]: g for g in api_server.GUIDE_CATALOG}
        self.assertIn("fix-flip", ids)
        self.assertEqual(ids["fix-flip"]["price"], 499)
        # Affiliate commission mapping exists so a referred buyer credits the affiliate.
        self.assertIn("fix-flip", api_server.GUIDE_TO_AFFILIATE_PRODUCT)

    def test_module_video_requires_entitled_media_token_when_paywalled(self):
        # Posters stay public; videos need a token issued to an entitled account.
        self.assertEqual(self.client.get("/brick/fixflip/module-1-poster.jpg").status_code, 200)
        self.assertEqual(self.client.get("/brick/fixflip/module-1.mp4").status_code, 403)
        self.assertEqual(self.client.get("/brick/fixflip/module-1.mp4?t=garbage").status_code, 403)
        # Path traversal inside the brick route is refused (encoded so the client does not normalize it away).
        self.assertEqual(self.client.get("/brick/fixflip/..%2F..%2Fapi_server.py").status_code, 404)
        self.assertEqual(self.client.get("/brick/%2e%2e/api_server.py").status_code, 404)
        # Non-owner without purchase cannot get a token.
        res = self.client.post("/api/auth/signup", json={"email": "viewer@example.com", "password": "Viewer!2026x", "name": "Viewer"})
        member_token = res.json()["token"]
        self.assertEqual(self.client.get("/api/brick/media-token", headers={"Authorization": f"Bearer {member_token}"}).status_code, 403)
        # A session token is not a media token.
        self.assertEqual(self.client.get(f"/brick/fixflip/module-1.mp4?t={member_token}").status_code, 403)
        # Owner gets a token and the video streams with Range support.
        owner_token = self._login(api_server.OWNER_EMAIL, api_server.OWNER_PASSWORD)
        res = self.client.get("/api/brick/media-token", headers={"Authorization": f"Bearer {owner_token}"})
        self.assertEqual(res.status_code, 200, res.text)
        t = res.json()["token"]
        res = self.client.get(f"/brick/fixflip/module-1.mp4?t={t}", headers={"Range": "bytes=0-99"})
        self.assertEqual(res.status_code, 206)
        self.assertEqual(len(res.content), 100)
        # Purchased member streams too.
        api_server.db.execute("INSERT INTO guide_purchases (user_id, guide_id, unlocked_by) VALUES ((SELECT id FROM users WHERE email='viewer@example.com'), 'fix-flip', 'test')")
        api_server.db.commit()
        self.assertEqual(self.client.get("/api/brick/media-token", headers={"Authorization": f"Bearer {member_token}"}).status_code, 200)

    def test_architecture_reservation_is_stored(self):
        res = self.client.post("/api/auth/signup", json={"email": "reserver@example.com", "password": "Reserve!2026x", "name": "Reserver"})
        token = res.json()["token"]
        self.assertEqual(self.client.post("/api/arc/inquiries", json={"kind": "reservation", "plan": "Ivy Trails"}).status_code, 401)
        res = self.client.post("/api/arc/inquiries", json={"kind": "reservation", "plan": "Ivy Trails — Early Access Reservation", "city": "Tampa, FL"},
                               headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["kind"], "reservation")
        row = api_server.db.execute("SELECT * FROM arc_inquiries WHERE id = ?", [res.json()["id"]]).fetchone()
        self.assertEqual(row["email"], "reserver@example.com")  # prefilled from the account
        self.assertEqual(row["name"], "Reserver")
        self.assertEqual(row["city"], "Tampa, FL")
        # Unknown kinds fall back to a plain inquiry rather than erroring.
        res = self.client.post("/api/arc/inquiries", json={"kind": "weird", "plan": "X"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(res.json()["kind"], "inquiry")


if __name__ == "__main__":
    unittest.main()
