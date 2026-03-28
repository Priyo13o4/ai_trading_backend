import importlib
import importlib.util
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock


MODULE_NAME = "app.referrals.reward_revocation"


def _reload_module():
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    return importlib.reload(module)


class RefundRevocationTests(unittest.IsolatedAsyncioTestCase):
    def _load_module(self):
        env = {
            "SUPABASE_PROJECT_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "service-role-key",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return _reload_module()

    async def test_revoke_on_hold_reward_success(self):
        """Test successful revocation of on_hold reward."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        reward_id = "33333333-3333-3333-3333-333333333333"
        event_id = "evt-refund-1"

        tx_row = {
            "id": tx_id,
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "refunded",
        }

        reward_row = {
            "referral_id": reward_id,
            "status": "on_hold",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "trigger_payment_id": tx_id,
            "hold_expires_at": "2099-12-31T00:00:00+00:00",
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[reward_row]),
                SimpleNamespace(data=[reward_row]),
                SimpleNamespace(data=[{"reason": "refund_on_hold_revocation"}]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.reward_id, reward_id)
        self.assertEqual(result.previous_status, "on_hold")
        self.assertEqual(result.trigger_payment_id, tx_id)

    async def test_revoke_no_transaction(self):
        """Test when payment transaction does not exist."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        event_id = "evt-refund-2"

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "no_transaction")
        self.assertEqual(result.trigger_payment_id, tx_id)

    async def test_revoke_no_reward(self):
        """Test when no reward exists for the trigger payment."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        event_id = "evt-refund-3"

        tx_row = {
            "id": tx_id,
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "refunded",
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "no_reward")
        self.assertEqual(result.trigger_payment_id, tx_id)

    async def test_revoke_unavailable_status_available(self):
        """Test when reward is already in available status (not on_hold)."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        reward_id = "33333333-3333-3333-3333-333333333333"
        event_id = "evt-refund-4"

        tx_row = {
            "id": tx_id,
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "refunded",
        }

        reward_row = {
            "referral_id": reward_id,
            "status": "available",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "trigger_payment_id": tx_id,
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[reward_row]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "unavailable_status")
        self.assertEqual(result.reward_id, reward_id)
        self.assertEqual(result.previous_status, "available")

    async def test_revoke_on_hold_but_hold_expired_is_ignored(self):
        """Refund after hold expiry must not revoke reward."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        reward_id = "33333333-3333-3333-3333-333333333333"
        event_id = "evt-refund-after-hold"

        tx_row = {
            "id": tx_id,
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "refunded",
        }

        reward_row = {
            "referral_id": reward_id,
            "status": "on_hold",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "trigger_payment_id": tx_id,
            "hold_expires_at": "2000-01-01T00:00:00+00:00",
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[reward_row]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "unavailable_status")
        self.assertEqual(result.reward_id, reward_id)
        self.assertEqual(result.previous_status, "on_hold")
        self.assertEqual(async_db_mock.await_count, 2)

    async def test_revoke_unavailable_status_applied(self):
        """Test when reward is already applied (cannot revoke)."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        reward_id = "33333333-3333-3333-3333-333333333333"
        event_id = "evt-refund-5"

        tx_row = {
            "id": tx_id,
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "refunded",
        }

        reward_row = {
            "referral_id": reward_id,
            "status": "applied",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "trigger_payment_id": tx_id,
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[reward_row]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "unavailable_status")
        self.assertEqual(result.previous_status, "applied")

    async def test_revoke_idempotent_already_revoked(self):
        """Test idempotency: second call finds already-revoked reward."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        reward_id = "33333333-3333-3333-3333-333333333333"
        event_id = "evt-refund-6"

        tx_row = {
            "id": tx_id,
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "refunded",
        }

        reward_row = {
            "referral_id": reward_id,
            "status": "on_hold",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "trigger_payment_id": tx_id,
            "hold_expires_at": "2099-12-31T00:00:00+00:00",
        }

        already_revoked_row = {
            "status": "revoked",
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[reward_row]),
                SimpleNamespace(data=[]),
                SimpleNamespace(data=[already_revoked_row]),
                SimpleNamespace(data=[{"reason": "refund_on_hold_revocation"}]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "already_revoked")
        self.assertEqual(result.reward_id, reward_id)
        self.assertEqual(result.previous_status, "revoked")

    async def test_revoke_invalid_uuid(self):
        """Test with invalid trigger_payment_id UUID."""
        mod = self._load_module()

        event_id = "evt-refund-7"

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            result = await mod.revoke_referral_reward_on_refund(
                trigger_payment_id="not-a-uuid",
                refund_trigger_event_id=event_id,
            )

        self.assertEqual(result.outcome, "skip_invalid_input")

    async def test_revoke_audit_trail_recorded(self):
        """Test that audit trail is recorded after successful revocation."""
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        reward_id = "33333333-3333-3333-3333-333333333333"
        event_id = "evt-refund-8"

        tx_row = {
            "id": tx_id,
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "refunded",
        }

        reward_row = {
            "referral_id": reward_id,
            "status": "on_hold",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "trigger_payment_id": tx_id,
            "hold_expires_at": "2099-12-31T00:00:00+00:00",
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[reward_row]),
                SimpleNamespace(data=[reward_row]),
                SimpleNamespace(data=[{"id": "audit-1"}]),
            ]
        )

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "async_db", new=async_db_mock):
                result = await mod.revoke_referral_reward_on_refund(
                    trigger_payment_id=tx_id,
                    refund_trigger_event_id=event_id,
                )

        self.assertEqual(result.outcome, "success")
        self.assertTrue(len(async_db_mock.call_args_list) >= 4)


class WebhookRefundRevocationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests verify webhook handler integration with revocation module."""

    def _load_webhook_module(self):
        env = {
            "SUPABASE_PROJECT_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "service-role-key",
        }
        fake_plisio = types.ModuleType("plisio")
        fake_plisio.PlisioAioClient = mock.Mock()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.dict(sys.modules, {"razorpay": mock.Mock(), "plisio": fake_plisio}):
                sys.modules.pop("app.payments.webhook_handler", None)
                return importlib.import_module("app.payments.webhook_handler")

    def test_webhook_imports_revocation_function(self):
        """Test that webhook handler imports the revocation function."""
        mod = self._load_webhook_module()
        self.assertTrue(hasattr(mod, "revoke_referral_reward_on_refund"))
        self.assertTrue(callable(mod.revoke_referral_reward_on_refund))

    def test_webhook_has_allow_refund_on_terminal_check(self):
        """Test that webhook handler has logic to allow refund transitions."""
        mod = self._load_webhook_module()
        source = importlib.util.find_spec(mod.__name__).origin
        with open(source, "r") as f:
            content = f.read()
        self.assertIn("allow_refund_on_terminal", content)
        self.assertIn("PaymentTransactionStatus.REFUNDED", content)
        self.assertIn("event=refund_revocation_result", content)


if __name__ == "__main__":
    unittest.main()
