import importlib
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock


MODULE_NAME = "app.payments.webhook_handler"


def _reload_module():
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    return importlib.reload(module)


class _ProviderSuccess:
    def map_event_to_state(self, event_type):
        return self.status

    def __init__(self, status):
        self.status = status


class WebhookReferralWireInTests(unittest.IsolatedAsyncioTestCase):
    def _load_module(self):
        env = {
            "SUPABASE_PROJECT_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "service-role-key",
        }
        fake_plisio = types.ModuleType("plisio")
        fake_plisio.PlisioAioClient = mock.Mock()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.dict(sys.modules, {"razorpay": mock.Mock(), "plisio": fake_plisio}):
                return _reload_module()

    async def test_evaluator_called_on_success_transition(self):
        mod = self._load_module()

        tx_id = "22222222-2222-2222-2222-222222222222"
        user_id = "11111111-1111-1111-1111-111111111111"
        tx_row = {
            "id": tx_id,
            "user_id": user_id,
            "subscription_id": "33333333-3333-3333-3333-333333333333",
            "status": "pending",
            "metadata": {
                "renewal_intent": True,
            },
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[{"id": tx_id, "status": "succeeded"}]),
                SimpleNamespace(data=[{"id": tx_id, "status": "succeeded"}]),
                SimpleNamespace(data=[{"id": "audit-1"}]),
                SimpleNamespace(data=[{"id": tx_row["subscription_id"], "status": "active"}]),
            ]
        )
        eval_mock = mock.AsyncMock(
            return_value=SimpleNamespace(
                outcome="success_reward_created",
                referral_id="44444444-4444-4444-4444-444444444444",
            )
        )

        event_row = {
            "provider": "plisio",
            "event_id": "evt-success-1",
            "event_type": "completed",
            "payload": {
                "order_number": "provider-payment-1",
                "currency": "USDT_BSC",
                "source_currency": "USD",
            },
        }

        with mock.patch.object(mod, "get_provider", return_value=_ProviderSuccess(mod.PaymentTransactionStatus.SUCCEEDED)):
            with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
                with mock.patch.object(mod, "async_db", new=async_db_mock):
                    with mock.patch.object(mod, "evaluate_referral_reward", new=eval_mock):
                        with mock.patch.object(mod, "_mark_webhook_processed", new=mock.AsyncMock()) as mark_mock:
                            with mock.patch.object(mod, "_set_webhook_cache_hint", new=mock.AsyncMock()):
                                with self.assertLogs(mod.logger, level="INFO") as logs:
                                    await mod.process_claimed_webhook_event(event_row)

        eval_mock.assert_awaited_once_with(
            referred_user_id=user_id,
            trigger_payment_id=tx_id,
        )
        mark_mock.assert_awaited_once()
        self.assertTrue(any("event=referral_reward_evaluation_result" in line for line in logs.output))

    async def test_evaluator_not_called_for_non_success_status(self):
        mod = self._load_module()

        tx_row = {
            "id": "55555555-5555-5555-5555-555555555555",
            "user_id": "66666666-6666-6666-6666-666666666666",
            "subscription_id": None,
            "status": "succeeded",
            "metadata": {},
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
            ]
        )
        eval_mock = mock.AsyncMock()

        event_row = {
            "provider": "plisio",
            "event_id": "evt-failed-1",
            "event_type": "failed",
            "payload": {
                "order_number": "provider-payment-2",
                "currency": "USDT_BSC",
                "source_currency": "USD",
            },
        }

        with mock.patch.object(mod, "get_provider", return_value=_ProviderSuccess(mod.PaymentTransactionStatus.FAILED)):
            with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
                with mock.patch.object(mod, "async_db", new=async_db_mock):
                    with mock.patch.object(mod, "evaluate_referral_reward", new=eval_mock):
                        with mock.patch.object(mod, "_mark_webhook_processed", new=mock.AsyncMock()) as mark_mock:
                            with mock.patch.object(mod, "_set_webhook_cache_hint", new=mock.AsyncMock()):
                                await mod.process_claimed_webhook_event(event_row)

        eval_mock.assert_not_awaited()
        mark_mock.assert_awaited_once()

    async def test_evaluator_exception_is_swallowed(self):
        mod = self._load_module()

        tx_id = "77777777-7777-7777-7777-777777777777"
        user_id = "88888888-8888-8888-8888-888888888888"
        tx_row = {
            "id": tx_id,
            "user_id": user_id,
            "subscription_id": "99999999-9999-9999-9999-999999999999",
            "status": "pending",
            "metadata": {
                "renewal_intent": True,
            },
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[{"id": tx_id, "status": "succeeded"}]),
                SimpleNamespace(data=[{"id": tx_id, "status": "succeeded"}]),
                SimpleNamespace(data=[{"id": "audit-2"}]),
                SimpleNamespace(data=[{"id": tx_row["subscription_id"], "status": "active"}]),
            ]
        )
        eval_mock = mock.AsyncMock(side_effect=RuntimeError("ref-eval-boom"))

        event_row = {
            "provider": "plisio",
            "event_id": "evt-success-2",
            "event_type": "completed",
            "payload": {
                "order_number": "provider-payment-3",
                "currency": "USDT_BSC",
                "source_currency": "USD",
            },
        }

        with mock.patch.object(mod, "get_provider", return_value=_ProviderSuccess(mod.PaymentTransactionStatus.SUCCEEDED)):
            with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
                with mock.patch.object(mod, "async_db", new=async_db_mock):
                    with mock.patch.object(mod, "evaluate_referral_reward", new=eval_mock):
                        with mock.patch.object(mod, "_mark_webhook_processed", new=mock.AsyncMock()) as mark_mock:
                            with mock.patch.object(mod, "_set_webhook_cache_hint", new=mock.AsyncMock()):
                                with self.assertLogs(mod.logger, level="WARNING") as logs:
                                    await mod.process_claimed_webhook_event(event_row)

        eval_mock.assert_awaited_once_with(
            referred_user_id=user_id,
            trigger_payment_id=tx_id,
        )
        mark_mock.assert_awaited_once()
        self.assertTrue(any("event=referral_reward_evaluation_failed" in line for line in logs.output))

    async def test_real_evaluator_feature_flag_disabled_via_webhook_path(self):
        mod = self._load_module()

        tx_id = "12121212-1212-1212-1212-121212121212"
        user_id = "13131313-1313-1313-1313-131313131313"
        tx_row = {
            "id": tx_id,
            "user_id": user_id,
            "subscription_id": "14141414-1414-1414-1414-141414141414",
            "status": "pending",
            "metadata": {
                "renewal_intent": True,
            },
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[{"id": tx_id, "status": "succeeded"}]),
                SimpleNamespace(data=[{"id": tx_id, "status": "succeeded"}]),
                SimpleNamespace(data=[{"id": "audit-feature-flag"}]),
                SimpleNamespace(data=[{"id": tx_row["subscription_id"], "status": "active"}]),
            ]
        )

        event_row = {
            "provider": "plisio",
            "event_id": "evt-feature-flag-disabled-1",
            "event_type": "completed",
            "payload": {
                "order_number": "provider-payment-feature-disabled",
                "currency": "USDT_BSC",
                "source_currency": "USD",
            },
        }

        reward_mod = importlib.import_module("app.referrals.reward_evaluator")

        with mock.patch.dict(os.environ, {"REFERRAL_REWARD_EVALUATION_ENABLED": "0"}, clear=False):
            with mock.patch.object(mod, "get_provider", return_value=_ProviderSuccess(mod.PaymentTransactionStatus.SUCCEEDED)):
                with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
                    with mock.patch.object(mod, "async_db", new=async_db_mock):
                        with mock.patch.object(reward_mod, "async_db", new=mock.AsyncMock()) as reward_async_db_mock:
                            with mock.patch.object(reward_mod, "get_supabase_client", new=mock.Mock()) as reward_client_mock:
                                with mock.patch.object(mod, "_mark_webhook_processed", new=mock.AsyncMock()) as mark_mock:
                                    with mock.patch.object(mod, "_set_webhook_cache_hint", new=mock.AsyncMock()):
                                        with self.assertLogs(mod.logger, level="INFO") as logs:
                                            await mod.process_claimed_webhook_event(event_row)

        mark_mock.assert_awaited_once()
        reward_async_db_mock.assert_not_awaited()
        reward_client_mock.assert_not_called()
        self.assertTrue(
            any(
                "event=referral_reward_evaluation_result" in line and "outcome=feature_disabled" in line
                for line in logs.output
            )
        )

    async def test_evaluator_called_when_cas_update_is_not_applied_but_latest_is_succeeded(self):
        mod = self._load_module()

        tx_id = "15151515-1515-1515-1515-151515151515"
        user_id = "16161616-1616-1616-1616-161616161616"
        tx_row = {
            "id": tx_id,
            "user_id": user_id,
            "subscription_id": "17171717-1717-1717-1717-171717171717",
            "status": "pending",
            "metadata": {
                "renewal_intent": True,
            },
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[]),
                SimpleNamespace(data=[{"id": tx_id, "status": "succeeded"}]),
            ]
        )
        eval_mock = mock.AsyncMock()

        event_row = {
            "provider": "plisio",
            "event_id": "evt-cas-no-update-1",
            "event_type": "completed",
            "payload": {
                "order_number": "provider-payment-cas-no-update",
                "currency": "USDT_BSC",
                "source_currency": "USD",
            },
        }

        with mock.patch.object(mod, "get_provider", return_value=_ProviderSuccess(mod.PaymentTransactionStatus.SUCCEEDED)):
            with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
                with mock.patch.object(mod, "async_db", new=async_db_mock):
                    with mock.patch.object(mod, "evaluate_referral_reward", new=eval_mock):
                        with mock.patch.object(mod, "_mark_webhook_processed", new=mock.AsyncMock()) as mark_mock:
                            with mock.patch.object(mod, "_set_webhook_cache_hint", new=mock.AsyncMock()):
                                await mod.process_claimed_webhook_event(event_row)

        eval_mock.assert_awaited_once_with(
            referred_user_id=user_id,
            trigger_payment_id=tx_id,
        )
        mark_mock.assert_awaited_once()

    async def test_refund_revocation_called_when_cas_update_is_not_applied_but_latest_is_refunded(self):
        mod = self._load_module()

        tx_id = "18181818-1818-1818-1818-181818181818"
        user_id = "19191919-1919-1919-1919-191919191919"
        tx_row = {
            "id": tx_id,
            "user_id": user_id,
            "subscription_id": None,
            "status": "succeeded",
            "metadata": {},
        }

        async_db_mock = mock.AsyncMock(
            side_effect=[
                SimpleNamespace(data=[tx_row]),
                SimpleNamespace(data=[]),
                SimpleNamespace(data=[{"id": tx_id, "status": "refunded"}]),
            ]
        )
        revoke_mock = mock.AsyncMock(
            return_value=SimpleNamespace(
                outcome="already_revoked",
                reward_id="20202020-2020-2020-2020-202020202020",
                previous_status="revoked",
            )
        )

        event_row = {
            "provider": "plisio",
            "event_id": "evt-cas-no-update-refund-1",
            "event_type": "refunded",
            "payload": {
                "order_number": "provider-payment-cas-no-update-refund",
                "currency": "USDT_BSC",
                "source_currency": "USD",
            },
        }

        with mock.patch.object(mod, "get_provider", return_value=_ProviderSuccess(mod.PaymentTransactionStatus.REFUNDED)):
            with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
                with mock.patch.object(mod, "async_db", new=async_db_mock):
                    with mock.patch.object(mod, "revoke_referral_reward_on_refund", new=revoke_mock):
                        with mock.patch.object(mod, "_mark_webhook_processed", new=mock.AsyncMock()) as mark_mock:
                            with mock.patch.object(mod, "_set_webhook_cache_hint", new=mock.AsyncMock()):
                                await mod.process_claimed_webhook_event(event_row)

        revoke_mock.assert_awaited_once_with(
            trigger_payment_id=tx_id,
            refund_trigger_event_id="evt-cas-no-update-refund-1",
        )
        mark_mock.assert_awaited_once()
