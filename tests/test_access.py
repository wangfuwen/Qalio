import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import app
from copilot.access import validate_access_config


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"QA_DEPLOYMENT": "public", "QA_ACCESS_USER": "admin", "QA_ACCESS_PASSWORD": "test-long-password-123"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # No lifespan/database needed for static/authentication checks.
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_all_sensitive_routes_require_credentials(self):
        for path in ["/", "/api/conversations", "/docs", "/openapi.json", "/static/app.js"]:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_valid_and_invalid_credentials(self):
        self.assertEqual(self.client.get("/", auth=("admin", "wrong")).status_code, 401)
        self.assertEqual(self.client.get("/", headers={"Authorization": "Basic @@@"}).status_code, 401)
        response = self.client.get("/", auth=("admin", "test-long-password-123"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_cross_site_writes_rejected(self):
        for headers in [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            response = self.client.post("/api/upload", auth=("admin", "test-long-password-123"), headers=headers)
            self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/upload", auth=("admin", "test-long-password-123"), headers={"Origin": "http://testserver"})
        self.assertEqual(response.status_code, 422)  # reached input validation

    def test_public_config_fails_closed(self):
        with patch.dict(os.environ, {"QA_ACCESS_PASSWORD": ""}):
            with self.assertRaises(RuntimeError):
                validate_access_config()
            self.assertEqual(self.client.get("/").status_code, 503)

    def test_local_mode_still_works_without_password(self):
        with patch.dict(os.environ, {"QA_DEPLOYMENT": "local", "QA_ACCESS_PASSWORD": ""}):
            validate_access_config()
            self.assertEqual(self.client.get("/").status_code, 200)
