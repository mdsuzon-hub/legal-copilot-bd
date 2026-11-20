#!/usr/bin/env python3
"""
Unit tests for Google Authentication (Issue #8).
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directory to path so apps/web modules are importable
current_dir = Path(__file__).resolve().parent
web_dir = current_dir.parent
sys.path.insert(0, str(web_dir))

import app
import config


class TestGoogleAuth(unittest.TestCase):
    def setUp(self):
        config.GOOGLE_CLIENT_ID = "511757892910-rpf5p7tu7d551anuek8l82f70a3e7tv6.apps.googleusercontent.com"
        config.GOOGLE_CLIENT_SECRET = "test_client_secret"

        app.app.config["TESTING"] = True
        app.app.config["SECRET_KEY"] = "test_secret_key"
        self.client = app.app.test_client()

    def test_auth_config_endpoint(self):
        """Test public /auth/config endpoint returns GOOGLE_CLIENT_ID."""
        res = self.client.get("/auth/config")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("google_client_id"), config.GOOGLE_CLIENT_ID)
        self.assertTrue(data.get("configured"))

    def test_google_login_redirect(self):
        """Test /auth/google/login constructs and redirects to Google OAuth 2.0 URL."""
        res = self.client.get("/auth/google/login")
        self.assertEqual(res.status_code, 302)
        location = res.headers.get("Location", "")
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", location)
        self.assertIn(config.GOOGLE_CLIENT_ID, location)
        self.assertIn("response_type=code", location)
        self.assertIn("scope=openid", location)

    def test_auth_me_unauthenticated(self):
        """Test /auth/me when user is not logged in."""
        res = self.client.get("/auth/me")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data.get("authenticated"))
        self.assertIsNone(data.get("user"))

    @patch("requests.get")
    def test_google_verify_success(self, mock_get):
        """Test /auth/google/verify endpoint with mocked Google token validation."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "sub": "google_99999",
            "email": "test.barrister@diu.edu.bd",
            "name": "Barrister Kamal",
            "picture": "https://lh3.googleusercontent.com/a/test",
            "aud": config.GOOGLE_CLIENT_ID,
        }
        mock_get.return_value = mock_response

        res = self.client.post(
            "/auth/google/verify",
            data=json.dumps({"credential": "mock_valid_id_token"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data["user"]["email"], "test.barrister@diu.edu.bd")
        self.assertEqual(data["user"]["name"], "Barrister Kamal")

        # Verify /auth/me recognizes the active session
        res_me = self.client.get("/auth/me")
        self.assertEqual(res_me.status_code, 200)
        data_me = res_me.get_json()
        self.assertTrue(data_me.get("authenticated"))
        self.assertEqual(data_me["user"]["email"], "test.barrister@diu.edu.bd")

    @patch("requests.get")
    def test_google_verify_invalid_token(self, mock_get):
        """Test /auth/google/verify rejects invalid Google token."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_get.return_value = mock_response

        res = self.client.post(
            "/auth/google/verify",
            data=json.dumps({"credential": "invalid_token_123"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertIn("error", data)

    def test_auth_logout(self):
        """Acceptance Criteria: Handle logout properly and clear user session."""
        # Manually seed session with logged in user
        with self.client.session_transaction() as sess:
            sess["user"] = {"google_id": "1", "email": "logout.test@example.com", "name": "User One"}

        # Verify session active
        res1 = self.client.get("/auth/me")
        self.assertTrue(res1.get_json().get("authenticated"))

        # Call logout
        res_logout = self.client.post("/auth/logout")
        self.assertEqual(res_logout.status_code, 200)
        self.assertTrue(res_logout.get_json().get("success"))

        # Verify session cleared
        res2 = self.client.get("/auth/me")
        self.assertFalse(res2.get_json().get("authenticated"))

    def test_google_callback_error_handling(self):
        """Acceptance Criteria: Authentication errors are handled properly."""
        # When user cancels Google login
        res = self.client.get("/auth/google/callback?error=access_denied&error_description=User+cancelled")
        self.assertEqual(res.status_code, 302)
        location = res.headers.get("Location", "")
        self.assertIn("auth_error=", location)


if __name__ == "__main__":
    unittest.main()
