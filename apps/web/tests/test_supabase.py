#!/usr/bin/env python3
"""
Test Supabase Credentials and Connectivity.

Usage:
    # 1. Run live credential check from .env or environment:
    python apps/web/tests/test_supabase.py

    # 2. Test specific credentials via CLI flags:
    python apps/web/tests/test_supabase.py --url https://your-id.supabase.co --key your-key

    # 3. Run unit tests via unittest:
    python -m unittest apps/web/tests/test_supabase.py
    # or
    python apps/web/tests/test_supabase.py --unit
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Base directory paths
BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent.parent

# Add apps/web to sys.path so config can be imported if needed
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def load_env_file(filepath: Path) -> dict:
    """Load key-value pairs from a .env file safely."""
    loaded = {}
    if not filepath.is_file():
        return loaded
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k:
                    loaded[k] = v
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass
    return loaded


# Load environment variables from standard project locations
load_env_file(ROOT_DIR / ".env")
load_env_file(BASE_DIR / ".env")


def get_supabase_credentials() -> tuple[str, str]:
    """Retrieve Supabase URL and Key from environment variables."""
    url = (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or ""
    ).strip()

    key = (
        os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
        or ""
    ).strip()

    return url.rstrip("/"), key


def mask_key(key: str) -> str:
    """Mask key for safe logging (e.g. sb_publi...4x8z or eyJhbGci...4x8z)."""
    if not key:
        return "<empty>"
    if len(key) <= 16:
        return f"{key[:4]}...{key[-3:]}"
    return f"{key[:10]}...{key[-4:]}"


def inspect_key(key: str) -> dict:
    """Analyze key format and detect accidental paste concatenation."""
    info = {
        "type": "unknown",
        "is_jwt": False,
        "is_publishable": False,
        "is_secret": False,
        "concatenation_detected": False,
        "warning": None,
    }

    if not key:
        return info

    # Check for accidental concatenation of publishable key + JWT key
    jwt_idx = key.find("eyJ")
    if (key.startswith("sb_publishable_") or key.startswith("sb_secret_")) and jwt_idx > 0:
        info["concatenation_detected"] = True
        first_part = key[:jwt_idx]
        second_part = key[jwt_idx:]
        info["warning"] = (
            f"Detected two keys pasted together in SUPABASE_KEY!\n"
            f"  Part 1 (Publishable key) : {first_part}\n"
            f"  Part 2 (Legacy JWT key)  : {second_part[:15]}...{second_part[-4:]}\n"
            f"  -> Please choose one of them and remove the other in your .env file."
        )
        return info

    if key.startswith("sb_publishable_"):
        info["type"] = "Supabase Publishable Key"
        info["is_publishable"] = True
    elif key.startswith("sb_secret_"):
        info["type"] = "Supabase Secret Key"
        info["is_secret"] = True
    elif key.startswith("eyJ"):
        info["is_jwt"] = True
        parts = key.split(".")
        if len(parts) >= 2:
            try:
                padded = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
                role = payload.get("role", "unknown")
                info["type"] = f"Supabase JWT ({role})"
                info["jwt_role"] = role
            except Exception:
                info["type"] = "Supabase JWT"
        else:
            info["type"] = "Supabase JWT"

    return info


def validate_supabase_url(url: str) -> tuple[bool, str]:
    """Validate Supabase URL structure."""
    if not url:
        return False, "URL is empty"
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "URL must start with http:// or https://"
    return True, "Valid URL format"


def verify_supabase_credentials(url: str, key: str, timeout: int = 10) -> dict:
    """
    Verify Supabase credentials against Auth and REST API endpoints.
    Uses urllib standard library.
    """
    import urllib.error
    import urllib.request

    url = url.rstrip("/")
    key_info = inspect_key(key)

    result = {
        "url": url,
        "masked_key": mask_key(key),
        "key_info": key_info,
        "valid_format": False,
        "health_check": {"ok": False, "status": None, "message": ""},
        "auth_check": {"ok": False, "status": None, "message": ""},
        "rest_check": {"ok": False, "status": None, "message": ""},
        "overall_success": False,
        "error": None,
    }

    is_valid, msg = validate_supabase_url(url)
    result["valid_format"] = is_valid
    if not is_valid:
        result["error"] = f"Invalid URL: {msg}"
        return result

    if not key:
        result["error"] = "Supabase API key is empty or missing"
        return result

    if key_info.get("concatenation_detected"):
        result["error"] = key_info["warning"]
        return result

    # Prepare headers:
    # 'apikey' is the project key for Kong/Supabase.
    # 'Authorization: Bearer' is ONLY for JWT tokens (like legacy anon/service_role keys or user tokens).
    # Opaque publishable/secret keys (sb_publishable_*, sb_secret_*) should NOT have Bearer set to the key.
    headers = {
        "apikey": key,
        "User-Agent": "LegalCopilotBD-SupabaseVerifier/1.0",
    }
    if key_info["is_jwt"]:
        headers["Authorization"] = f"Bearer {key}"

    # 1. Health check: /auth/v1/health (requires apikey on Supabase gateway)
    try:
        health_url = f"{url}/auth/v1/health"
        req = urllib.request.Request(health_url, headers=headers)
        start_t = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = int((time.time() - start_t) * 1000)
            status = resp.status
            data = resp.read().decode("utf-8", errors="replace")
            result["health_check"] = {
                "ok": status == 200,
                "status": status,
                "latency_ms": elapsed_ms,
                "message": f"Auth service healthy ({elapsed_ms}ms)",
            }
    except urllib.error.HTTPError as e:
        result["health_check"] = {
            "ok": False,
            "status": e.code,
            "message": f"HTTP {e.code}: {e.reason}",
        }
    except Exception as e:
        result["health_check"] = {
            "ok": False,
            "status": None,
            "message": f"Connection failed: {str(e)}",
        }

    # 2. Auth settings check: /auth/v1/settings (verifies key validity)
    try:
        auth_url = f"{url}/auth/v1/settings"
        req = urllib.request.Request(auth_url, headers=headers)
        start_t = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = int((time.time() - start_t) * 1000)
            status = resp.status
            result["auth_check"] = {
                "ok": status == 200,
                "status": status,
                "latency_ms": elapsed_ms,
                "message": f"Authenticated successfully ({elapsed_ms}ms)",
            }
    except urllib.error.HTTPError as e:
        err_msg = f"HTTP {e.code}: {e.reason}"
        try:
            body = e.read().decode("utf-8", errors="replace")
            json_data = json.loads(body)
            if "msg" in json_data:
                err_msg = f"HTTP {e.code}: {json_data['msg']}"
            elif "message" in json_data:
                err_msg = f"HTTP {e.code}: {json_data['message']}"
        except Exception:
            pass
        result["auth_check"] = {
            "ok": False,
            "status": e.code,
            "message": err_msg,
        }
    except Exception as e:
        result["auth_check"] = {
            "ok": False,
            "status": None,
            "message": f"Auth request error: {str(e)}",
        }

    # 3. PostgREST OpenAPI check: /rest/v1/
    try:
        rest_url = f"{url}/rest/v1/"
        req = urllib.request.Request(rest_url, headers=headers)
        start_t = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = int((time.time() - start_t) * 1000)
            status = resp.status
            result["rest_check"] = {
                "ok": status == 200,
                "status": status,
                "latency_ms": elapsed_ms,
                "message": f"PostgREST accessible ({elapsed_ms}ms)",
            }
    except urllib.error.HTTPError as e:
        status = e.code
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
            json_data = json.loads(body_text)
            hint = json_data.get("hint", "")
            msg = json_data.get("message", e.reason)
            # If PostgREST notes secret key / service role is needed for root schema, the key itself was accepted!
            if "service_role" in hint.lower() or "secret" in hint.lower():
                result["rest_check"] = {
                    "ok": True,
                    "status": 200,
                    "message": "Valid public/anon key recognized (schema root restricted to secret key)",
                }
            else:
                result["rest_check"] = {
                    "ok": False,
                    "status": status,
                    "message": f"HTTP {status}: {msg} ({hint})" if hint else f"HTTP {status}: {msg}",
                }
        except Exception:
            result["rest_check"] = {
                "ok": False,
                "status": status,
                "message": f"HTTP {status}: {e.reason}",
            }
    except Exception as e:
        result["rest_check"] = {
            "ok": False,
            "status": None,
            "message": f"REST request error: {str(e)}",
        }

    # Overall success if auth_check succeeds
    result["overall_success"] = result["auth_check"]["ok"] and (
        result["rest_check"]["ok"] or result["health_check"]["ok"]
    )

    return result


def print_verification_report(res: dict) -> None:
    """Print a clean, structured diagnostic report."""
    print("=" * 60)
    print("        SUPABASE CREDENTIALS VERIFICATION REPORT")
    print("=" * 60)
    print(f"Endpoint URL : {res.get('url') or '<none>'}")
    print(f"API Key      : {res.get('masked_key') or '<none>'}")
    key_type = res.get("key_info", {}).get("type", "unknown")
    print(f"Key Type     : {key_type}")
    print("-" * 60)

    if res.get("error"):
        print(f"❌ Error:\n{res['error']}")
        print("-" * 60)
        return

    # Health
    h = res["health_check"]
    h_symbol = "✅" if h["ok"] else "⚠️"
    print(f"{h_symbol} Service Health : {h['message']} (Status: {h.get('status')})")

    # Auth
    a = res["auth_check"]
    a_symbol = "✅" if a["ok"] else "❌"
    print(f"{a_symbol} Auth Settings  : {a['message']} (Status: {a.get('status')})")

    # REST
    r = res["rest_check"]
    r_symbol = "✅" if r["ok"] else "❌"
    print(f"{r_symbol} PostgREST API  : {r['message']} (Status: {r.get('status')})")

    print("-" * 60)
    if res["overall_success"]:
        print("🎉 SUCCESS: Supabase credentials are valid and operational!")
    else:
        print("❌ FAILURE: Credentials could not be verified successfully.")
        if not a["ok"]:
            print("   - Please check whether SUPABASE_KEY is correct.")
        if not h["ok"]:
            print("   - Please check whether SUPABASE_URL is reachable.")
    print("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Unit Test Suite
# ─────────────────────────────────────────────────────────────────────────────
class TestSupabaseCredentials(unittest.TestCase):
    """Unit tests for Supabase credential validation and connectivity."""

    def test_mask_key(self):
        """Test key masking logic."""
        self.assertEqual(mask_key(""), "<empty>")
        self.assertEqual(mask_key("short"), "shor...ort")
        long_key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef1234"
        masked = mask_key(long_key)
        self.assertTrue(masked.startswith("eyJhbGciOi"))
        self.assertTrue(masked.endswith("1234"))
        self.assertNotIn("abcdef", masked)

    def test_inspect_key(self):
        """Test key inspection and concatenation detection."""
        # Publishable key
        info = inspect_key("sb_publishable_12345")
        self.assertEqual(info["type"], "Supabase Publishable Key")
        self.assertFalse(info["concatenation_detected"])

        # Secret key
        info = inspect_key("sb_secret_12345")
        self.assertEqual(info["type"], "Supabase Secret Key")
        self.assertFalse(info["concatenation_detected"])

        # Accidental concatenation
        glued_key = "sb_publishable_12345eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        info = inspect_key(glued_key)
        self.assertTrue(info["concatenation_detected"])
        self.assertIsNotNone(info["warning"])

    def test_validate_url(self):
        """Test URL validation logic."""
        ok, _ = validate_supabase_url("https://abcxyz.supabase.co")
        self.assertTrue(ok)

        ok, _ = validate_supabase_url("http://localhost:54321")
        self.assertTrue(ok)

        bad, msg = validate_supabase_url("")
        self.assertFalse(bad)
        self.assertIn("empty", msg.lower())

        bad, msg = validate_supabase_url("ftp://example.com")
        self.assertFalse(bad)
        self.assertIn("http", msg.lower())

    @patch("urllib.request.urlopen")
    def test_mock_verify_success(self, mock_urlopen):
        """Test verification flow with simulated successful responses."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"version": "1.0", "definitions": {"users": {}}}'
        mock_response.__enter__.return_value = mock_response

        mock_urlopen.return_value = mock_response

        res = verify_supabase_credentials("https://test.supabase.co", "sb_publishable_testkey123")
        self.assertTrue(res["overall_success"])
        self.assertTrue(res["health_check"]["ok"])
        self.assertTrue(res["auth_check"]["ok"])
        self.assertTrue(res["rest_check"]["ok"])

    @patch("urllib.request.urlopen")
    def test_mock_verify_invalid_key(self, mock_urlopen):
        """Test verification flow with simulated 401 Unauthorized."""
        import urllib.error

        health_resp = MagicMock()
        health_resp.status = 200
        health_resp.read.return_value = b'{"version": "1.0"}'
        health_resp.__enter__.return_value = health_resp

        def side_effect(req, *args, **kwargs):
            if "/auth/v1/health" in req.full_url:
                return health_resp
            err = urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None
            )
            err.read = MagicMock(return_value=b'{"message": "Invalid API key"}')
            raise err

        mock_urlopen.side_effect = side_effect

        res = verify_supabase_credentials("https://test.supabase.co", "sb_publishable_bad")
        self.assertFalse(res["overall_success"])
        self.assertTrue(res["health_check"]["ok"])
        self.assertFalse(res["auth_check"]["ok"])
        self.assertEqual(res["auth_check"]["status"], 401)
        self.assertIn("Invalid API key", res["auth_check"]["message"])


def main():
    parser = argparse.ArgumentParser(description="Test Supabase credentials and API connectivity.")
    parser.add_argument("--url", default=None, help="Supabase project URL (overrides .env)")
    parser.add_argument("--key", default=None, help="Supabase API key (overrides .env)")
    parser.add_argument("--unit", action="store_true", help="Run unittest test cases")
    args, unknown = parser.parse_known_args()

    if args.unit or (len(sys.argv) > 1 and any(arg in sys.argv for arg in ["-v", "--verbose"])):
        sys.argv = [sys.argv[0]] + [a for a in unknown if a != "--unit"]
        unittest.main()
        return

    url = args.url
    key = args.key

    if not url or not key:
        env_url, env_key = get_supabase_credentials()
        url = url or env_url
        key = key or env_key

    if not url or not key:
        print("=" * 60)
        print("          SUPABASE CREDENTIAL CHECKER")
        print("=" * 60)
        print("No Supabase credentials detected in environment or .env!")
        print("\nTo test credentials, please either:")
        print("1. Set them in .env:")
        print("   SUPABASE_URL=https://your-project.supabase.co")
        print("   SUPABASE_KEY=your-supabase-anon-or-service-role-key")
        print("\n2. Or provide them via command line:")
        print("   python apps/web/tests/test_supabase.py --url <URL> --key <KEY>")
        print("\n3. Or run unit test suite:")
        print("   python apps/web/tests/test_supabase.py --unit")
        print("=" * 60)
        sys.exit(1)

    print(f"Testing Supabase credentials for: {url}")
    result = verify_supabase_credentials(url, key)
    print_verification_report(result)
    sys.exit(0 if result["overall_success"] else 1)


if __name__ == "__main__":
    main()
