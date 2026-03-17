import importlib
import unittest


class SupabaseAdminParserTests(unittest.TestCase):
    def _load_module(self):
        module = importlib.import_module("app.authn.supabase_admin")
        return importlib.reload(module)

    def test_extracts_nested_user_envelope(self):
        supabase_admin = self._load_module()
        payload = {"user": {"id": "u-1", "email": "user@example.com"}}

        user = supabase_admin._extract_user_from_admin_response(payload)

        self.assertEqual(user["id"], "u-1")

    def test_extracts_top_level_user_envelope(self):
        supabase_admin = self._load_module()
        payload = {"id": "u-2", "created_at": "2026-03-16T00:00:00Z"}

        user = supabase_admin._extract_user_from_admin_response(payload)

        self.assertEqual(user["id"], "u-2")

    def test_rejects_payload_without_user_id(self):
        supabase_admin = self._load_module()
        payload = {"user": {"email": "missing-id@example.com"}}

        user = supabase_admin._extract_user_from_admin_response(payload)

        self.assertIsNone(user)
