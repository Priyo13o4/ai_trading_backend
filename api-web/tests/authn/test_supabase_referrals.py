import importlib
import os
import sys
import time
import hashlib
import unittest
from unittest import mock


def _reload_referrals_module():
    sys.modules.pop("app.authn.supabase_referrals", None)
    module = importlib.import_module("app.authn.supabase_referrals")
    return importlib.reload(module)


class SupabaseReferralsTests(unittest.IsolatedAsyncioTestCase):
    def _load_module(self):
        env = {
            "SUPABASE_PROJECT_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "service-role-key",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return _reload_referrals_module()

    def test_normalize_referral_code_accepts_expected_pattern(self):
        referrals = self._load_module()

        self.assertEqual(referrals.normalize_referral_code(" ab12cd34 "), "AB12CD34")
        self.assertIsNone(referrals.normalize_referral_code("abc"))
        self.assertIsNone(referrals.normalize_referral_code("bad-code"))

    async def test_capture_skips_self_referral(self):
        referrals = self._load_module()

        with mock.patch.object(
            referrals,
            "admin_get_user",
            new=mock.AsyncMock(return_value={"created_at": int(time.time())}),
        ):
            with mock.patch.object(
                referrals,
                "_resolve_active_referral_code",
                new=mock.AsyncMock(
                    return_value={
                        "referral_code_id": "code-id-1",
                        "referrer_id": "33333333-3333-3333-3333-333333333333",
                    }
                ),
            ):
                with mock.patch.object(referrals, "_insert_referral_tracking_row", new=mock.AsyncMock()) as insert_mock:
                    result = await referrals.capture_referral_attribution_from_exchange(
                        referred_user_id="33333333-3333-3333-3333-333333333333",
                        claims={"raw_user_meta_data": {"referral_code": "ab12cd34"}},
                        user_agent="Mozilla/5.0",
                        ip_address="203.0.113.44",
                    )

        self.assertEqual(result, "skip:self_referral")
        insert_mock.assert_not_awaited()

    async def test_capture_fetches_admin_user_metadata_when_claims_missing(self):
        referrals = self._load_module()
        fresh_created_at = int(time.time())

        with mock.patch.object(
            referrals,
            "admin_get_user",
            new=mock.AsyncMock(
                return_value={
                    "created_at": fresh_created_at,
                    "raw_user_meta_data": {"referral_code": "ZXCV1234"},
                }
            ),
        ):
            with mock.patch.object(
                referrals,
                "_resolve_active_referral_code",
                new=mock.AsyncMock(
                    return_value={
                        "referral_code_id": "code-id-2",
                        "referrer_id": "44444444-4444-4444-4444-444444444444",
                    }
                ),
            ):
                with mock.patch.object(
                    referrals,
                    "_insert_referral_tracking_row",
                    new=mock.AsyncMock(return_value=True),
                ) as insert_mock:
                    result = await referrals.capture_referral_attribution_from_exchange(
                        referred_user_id="55555555-5555-5555-5555-555555555555",
                        claims={"created_at": fresh_created_at},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        ip_address="198.51.100.22",
                    )

        self.assertEqual(result, "success:captured")
        insert_mock.assert_awaited_once_with(
            referrer_id="44444444-4444-4444-4444-444444444444",
            referred_id="55555555-5555-5555-5555-555555555555",
            referral_code_id="code-id-2",
            ua_hash=hashlib.sha256("Mozilla/5.0 (Windows NT 10.0; Win64; x64)".encode("utf-8")).hexdigest(),
            ip_prefix="198.51.100",
        )

    async def test_insert_referral_tracking_row_uses_referred_id_conflict_and_payload_keys(self):
        referrals = self._load_module()

        fake_resp = mock.Mock(status_code=201)
        fake_resp.json.return_value = [{"id": "row-1"}]

        fake_client = mock.AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.post.return_value = fake_resp

        with mock.patch.object(referrals.httpx, "AsyncClient", return_value=fake_client):
            inserted = await referrals._insert_referral_tracking_row(
                referrer_id="44444444-4444-4444-4444-444444444444",
                referred_id="55555555-5555-5555-5555-555555555555",
                referral_code_id="code-id-2",
                ua_hash="ua-hash-2",
                ip_prefix="198.51.100",
            )

        self.assertTrue(inserted)
        post_call = fake_client.post.await_args
        url = post_call.args[0]
        payload = post_call.kwargs["json"]

        self.assertIn("on_conflict=referred_id", url)
        self.assertEqual(payload[0]["referrer_id"], "44444444-4444-4444-4444-444444444444")
        self.assertEqual(payload[0]["referred_id"], "55555555-5555-5555-5555-555555555555")
        self.assertEqual(payload[0]["registration_ua_hash"], "ua-hash-2")
        self.assertEqual(payload[0]["registration_ip_prefix"], "198.51.100")
        self.assertEqual(
            payload[0]["audit_metadata"],
            {
                "attribution_security": {
                    "ip_prefix": "198.51.100",
                    "ua_hash": "ua-hash-2",
                }
            },
        )
        self.assertEqual(payload[0]["metadata"], {"attribution_source": "auth_exchange"})

    async def test_capture_ignores_user_metadata_referral_code(self):
        referrals = self._load_module()
        fresh_created_at = int(time.time())

        with mock.patch.object(
            referrals,
            "admin_get_user",
            new=mock.AsyncMock(return_value={"created_at": fresh_created_at, "raw_user_meta_data": {}}),
        ) as admin_fetch:
            with mock.patch.object(referrals, "_resolve_active_referral_code", new=mock.AsyncMock()) as resolve_mock:
                result = await referrals.capture_referral_attribution_from_exchange(
                    referred_user_id="66666666-6666-6666-6666-666666666666",
                    claims={
                        "created_at": fresh_created_at,
                        "user_metadata": {"referral_code": "ABCD1234"},
                    },
                    user_agent="Mozilla/5.0 (X11; Linux x86_64)",
                    ip_address="203.0.113.25",
                )

        self.assertEqual(result, "skip:no_referral_code")
        resolve_mock.assert_not_awaited()
        admin_fetch.assert_awaited_once()

    async def test_capture_skips_stale_signup_even_when_claims_have_referral_code(self):
        referrals = self._load_module()
        fresh_claims_created_at = int(time.time())
        stale_admin_created_at = fresh_claims_created_at - 3600

        with mock.patch.object(
            referrals,
            "admin_get_user",
            new=mock.AsyncMock(return_value={"created_at": stale_admin_created_at}),
        ) as admin_fetch:
            with mock.patch.object(referrals, "_resolve_active_referral_code", new=mock.AsyncMock()) as resolve_mock:
                result = await referrals.capture_referral_attribution_from_exchange(
                    referred_user_id="77777777-7777-7777-7777-777777777777",
                    claims={
                        "created_at": fresh_claims_created_at,
                        "raw_user_meta_data": {"referral_code": "ABCD1234"},
                    },
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    ip_address="203.0.114.11",
                )

        self.assertEqual(result, "skip:stale_signup_for_capture")
        admin_fetch.assert_awaited_once()
        resolve_mock.assert_not_awaited()
