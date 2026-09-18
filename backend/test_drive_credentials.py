"""
Which environment variable the Drive service account is read from.

Regression cover for a silent failure: main.py read only
GOOGLE_SERVICE_ACCOUNT_JSON while the Railway deployment had always set
GOOGLE_CREDENTIALS_JSON. The key was therefore ignored, every run fell back to
OAuth without saying so, and Drive broke outright once that refresh token
expired. Nothing surfaced until a run failed with invalid_grant.
"""

import os
import sys
import unittest
from unittest.mock import patch

# main.py imports script.py flat ("import script"), so backend/ has to be on the
# path even when this module is loaded as backend.test_drive_credentials.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# script.py raises at import time without these.
os.environ.setdefault("TOKEN", "test-token")
os.environ.setdefault("MERCHANT", "test-merchant")

import main  # noqa: E402

SERVICE_ACCOUNT_VARS = ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_CREDENTIALS_JSON")


class ServiceAccountEnvVarTests(unittest.TestCase):
    def setUp(self):
        # Neither a stray real key nor a repo-root credentials.json may decide
        # the outcome of these tests.
        for name in SERVICE_ACCOUNT_VARS:
            self.enterContext(patch.dict(os.environ, {}, clear=False))
            os.environ.pop(name, None)
        self.enterContext(patch.object(os.path, "exists", return_value=False))

    def _credentials_from(self, env):
        """Return the info dict main.py would build credentials from."""
        seen = {}

        def from_info(info, scopes=None):
            seen["info"] = info
            return "credentials"

        with patch.dict(os.environ, env, clear=False), \
                patch.object(main.service_account.Credentials,
                             "from_service_account_info", from_info):
            result = main._service_account_credentials()
        return result, seen.get("info")

    def test_documented_name_is_read(self):
        result, info = self._credentials_from(
            {"GOOGLE_SERVICE_ACCOUNT_JSON": '{"type": "service_account", "a": 1}'})
        self.assertEqual(result, "credentials")
        self.assertEqual(info["a"], 1)

    def test_railways_name_is_also_read(self):
        """The whole point: GOOGLE_CREDENTIALS_JSON must not be ignored."""
        result, info = self._credentials_from(
            {"GOOGLE_CREDENTIALS_JSON": '{"type": "service_account", "b": 2}'})
        self.assertEqual(result, "credentials")
        self.assertEqual(info["b"], 2)

    def test_documented_name_wins_when_both_are_set(self):
        _, info = self._credentials_from({
            "GOOGLE_SERVICE_ACCOUNT_JSON": '{"which": "documented"}',
            "GOOGLE_CREDENTIALS_JSON": '{"which": "railway"}',
        })
        self.assertEqual(info["which"], "documented")

    def test_unresolved_railway_placeholder_is_not_a_key(self):
        """env.md carries this verbatim; a copied .env must still reach OAuth."""
        result, info = self._credentials_from(
            {"GOOGLE_CREDENTIALS_JSON": "${{shared.GOOGLE_CREDENTIALS_JSON}}"})
        self.assertIsNone(result)
        self.assertIsNone(info)

    def test_blank_value_falls_through(self):
        result, _ = self._credentials_from({"GOOGLE_SERVICE_ACCOUNT_JSON": "   "})
        self.assertIsNone(result)

    def test_malformed_json_still_fails_loudly(self):
        """A real key that is broken must not be swallowed into an OAuth fallback."""
        with patch.dict(os.environ, {"GOOGLE_CREDENTIALS_JSON": "{not json"}, clear=False):
            with self.assertRaises(RuntimeError) as caught:
                main._service_account_credentials()
        self.assertIn("GOOGLE_CREDENTIALS_JSON", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
