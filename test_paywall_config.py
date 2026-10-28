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


if __name__ == "__main__":
    unittest.main()
