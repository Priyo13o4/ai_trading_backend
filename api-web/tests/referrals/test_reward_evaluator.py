import importlib
import os
import sys
import unittest
from unittest import mock


MODULE_NAME = "app.referrals.reward_evaluator"


def _reload_module():
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    return importlib.reload(module)


class RewardEvaluatorTests(unittest.IsolatedAsyncioTestCase):
    def _load_module(self, env: dict[str, str] | None = None):
        base_env = {
            "SUPABASE_PROJECT_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "service-role-key",
            "REFERRAL_REWARD_EVALUATION_ENABLED": "1",
        }
        if env:
            base_env.update(env)
        with mock.patch.dict(os.environ, base_env, clear=False):
            return _reload_module()

    def test_feature_flag_default_off(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": ""})
        # H2 fix: env is now read per-call, so we must patch at assertion time too.
        with mock.patch.dict(os.environ, {"REFERRAL_REWARD_EVALUATION_ENABLED": ""}, clear=False):
            self.assertFalse(mod.is_reward_evaluation_enabled())

    async def test_evaluator_is_noop_when_flag_disabled(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "0"})

        # H2 fix: env is now read per-call, so we must patch at assertion time too.
        with mock.patch.dict(os.environ, {"REFERRAL_REWARD_EVALUATION_ENABLED": "0"}, clear=False):
            result = await mod.evaluate_referral_reward(
                referred_user_id="11111111-1111-1111-1111-111111111111",
                trigger_payment_id="22222222-2222-2222-2222-222222222222",
            )

        self.assertEqual(result.outcome, "feature_disabled")
        self.assertIsNone(result.referral_id)

    async def test_invalid_uuid_input_is_skipped_without_rpc_call(self):
        mod = self._load_module()
        mock_supabase = mock.Mock()
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                result = await mod.evaluate_referral_reward(
                    referred_user_id="not-a-uuid",
                    trigger_payment_id="also-not-a-uuid",
                )

        self.assertEqual(result.outcome, "skip_invalid_input")
        self.assertFalse(mock_supabase.rpc.called)

    async def test_rpc_outcome_mapping_skip_not_first_success(self):
        mod = self._load_module(
            env={
                "REFERRAL_REWARD_EVALUATION_ENABLED": "1",
                "REFERRAL_REWARD_HOLD_DAYS": "30",
            }
        )

        mock_response = mock.Mock(data=[{
            "result_code": "skip_not_first_success",
            "referral_id": "33333333-3333-3333-3333-333333333333",
            "reward_created": False,
            "qualified_updated": False,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response

        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                    result = await mod.evaluate_referral_reward(
                        referred_user_id="11111111-1111-1111-1111-111111111111",
                        trigger_payment_id="22222222-2222-2222-2222-222222222222",
                    )

        self.assertEqual(result.outcome, "skip_not_first_success")
        self.assertEqual(result.referral_id, "33333333-3333-3333-3333-333333333333")
        rpc_name, rpc_payload = mock_supabase.rpc.call_args.args
        self.assertEqual(rpc_name, "qualify_referral_reward")
        self.assertEqual(rpc_payload["referred_user_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(rpc_payload["trigger_payment_id"], "22222222-2222-2222-2222-222222222222")
        self.assertEqual(rpc_payload["hold_days"], 7)

    async def test_rpc_idempotent_duplicate_maps_to_reconciled_outcome(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        mock_response = mock.Mock(data=[{
            "result_code": "success_already_rewarded_reconciled",
            "referral_id": "44444444-4444-4444-4444-444444444444",
            "reward_created": False,
            "qualified_updated": False,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                    result = await mod.evaluate_referral_reward(
                        referred_user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        trigger_payment_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    )

        self.assertEqual(result.outcome, "success_already_rewarded_reconciled")
        self.assertEqual(result.referral_id, "44444444-4444-4444-4444-444444444444")

    async def test_rpc_reconciliation_outcome_mapping(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        mock_response = mock.Mock(data=[{
            "result_code": "success_already_rewarded_reconciled",
            "referral_id": "55555555-5555-5555-5555-555555555555",
            "reward_created": False,
            "qualified_updated": True,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                    result = await mod.evaluate_referral_reward(
                        referred_user_id="11111111-2222-3333-4444-555555555555",
                        trigger_payment_id="66666666-7777-8888-9999-000000000000",
                    )

        self.assertEqual(result.outcome, "success_already_rewarded_reconciled")
        self.assertEqual(result.referral_id, "55555555-5555-5555-5555-555555555555")

    async def test_rpc_failure_returns_controlled_error(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        mock_supabase = mock.Mock()
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=RuntimeError("db-down"))):
                with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                    result = await mod.evaluate_referral_reward(
                        referred_user_id="11111111-1111-1111-1111-111111111111",
                        trigger_payment_id="22222222-2222-2222-2222-222222222222",
                    )

        self.assertEqual(result.outcome, "error_controlled")

    async def test_same_network_soft_signal_allows_rpc(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        fraud_result = mod.FraudDetectionResult(
            blocked=False,
            outcome=None,
            reason="same_network",
            referral_id="99999999-9999-9999-9999-999999999999",
        )

        mock_response = mock.Mock(data=[{
            "result_code": "success_reward_created",
            "referral_id": "99999999-9999-9999-9999-999999999999",
            "reward_created": True,
            "qualified_updated": True,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                    with mock.patch.object(mod, "fraud_detect_referral_pattern", new=mock.AsyncMock(return_value=fraud_result)):
                        result = await mod.evaluate_referral_reward(
                            referred_user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                            trigger_payment_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        )

        self.assertEqual(result.outcome, "success_reward_created")
        self.assertEqual(result.referral_id, "99999999-9999-9999-9999-999999999999")
        self.assertTrue(mock_supabase.rpc.called)

    async def test_fraud_duplicate_identity_blocks_before_rpc(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        fraud_result = mod.FraudDetectionResult(
            blocked=True,
            outcome="fraud_blocked_duplicate_identity",
            reason="duplicate_payment_identity",
            referral_id="88888888-8888-8888-8888-888888888888",
        )
        mock_supabase = mock.Mock()

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                with mock.patch.object(mod, "fraud_detect_referral_pattern", new=mock.AsyncMock(return_value=fraud_result)):
                    result = await mod.evaluate_referral_reward(
                        referred_user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        trigger_payment_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    )

        self.assertEqual(result.outcome, "fraud_blocked_duplicate_identity")
        self.assertEqual(result.referral_id, "88888888-8888-8888-8888-888888888888")
        self.assertFalse(mock_supabase.rpc.called)

    async def test_fraud_detector_failure_is_fail_safe_and_allows_rpc(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        mock_response = mock.Mock(data=[{
            "result_code": "success_reward_created",
            "referral_id": "77777777-7777-7777-7777-777777777777",
            "reward_created": True,
            "qualified_updated": True,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                with mock.patch.object(mod, "is_reward_evaluation_enabled", return_value=True):
                    with mock.patch.object(
                        mod,
                        "fraud_detect_referral_pattern",
                        new=mock.AsyncMock(side_effect=RuntimeError("fraud-check-unavailable")),
                    ):
                        result = await mod.evaluate_referral_reward(
                            referred_user_id="11111111-1111-1111-1111-111111111111",
                            trigger_payment_id="22222222-2222-2222-2222-222222222222",
                        )

        self.assertEqual(result.outcome, "success_reward_created")
        self.assertTrue(mock_supabase.rpc.called)

    async def test_fraud_detect_same_network_is_soft_signal_only(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        pending_row = {
            "id": "f1111111-1111-1111-1111-111111111111",
            "referrer_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "registration_ip_prefix": "203.0.113",
            "registration_ua_hash": "ua-hash-same",
            "audit_metadata": {},
        }

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "_get_pending_referral_row", new=mock.AsyncMock(return_value=pending_row)):
                with mock.patch.object(mod, "_get_referrer_signup_security", new=mock.AsyncMock(return_value=("203.0.113", "ua-hash-same"))):
                    with mock.patch.object(mod, "_get_payment_identity_hash", new=mock.AsyncMock(return_value="")):
                        result = await mod.fraud_detect_referral_pattern(
                            referred_user_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                            trigger_payment_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                        )

        self.assertFalse(result.blocked)
        self.assertIsNone(result.outcome)

    async def test_duplicate_identity_is_blocking_policy(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        pending_row = {
            "id": "f2222222-2222-2222-2222-222222222222",
            "referrer_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "registration_ip_prefix": "203.0.113",
            "registration_ua_hash": "ua-hash-a",
            "audit_metadata": {},
        }

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "_get_pending_referral_row", new=mock.AsyncMock(return_value=pending_row)):
                with mock.patch.object(mod, "_get_referrer_signup_security", new=mock.AsyncMock(return_value=("198.51.100", "ua-hash-b"))):
                    with mock.patch.object(mod, "_get_payment_identity_hash", new=mock.AsyncMock(return_value="identity-123")):
                        with mock.patch.object(mod, "_has_duplicate_identity_under_referrer", new=mock.AsyncMock(return_value=True)):
                            result = await mod.fraud_detect_referral_pattern(
                                referred_user_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                                trigger_payment_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                            )

        self.assertTrue(result.blocked)
        self.assertEqual(result.outcome, "fraud_blocked_duplicate_identity")

    async def test_fraud_detect_legitimate_pass_through(self):
        mod = self._load_module(env={"REFERRAL_REWARD_EVALUATION_ENABLED": "1"})

        pending_row = {
            "id": "f3333333-3333-3333-3333-333333333333",
            "referrer_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "registration_ip_prefix": "203.0.113",
            "registration_ua_hash": "ua-hash-a",
            "audit_metadata": {},
        }

        with mock.patch.object(mod, "get_supabase_client", return_value=mock.Mock()):
            with mock.patch.object(mod, "_get_pending_referral_row", new=mock.AsyncMock(return_value=pending_row)):
                with mock.patch.object(mod, "_get_referrer_signup_security", new=mock.AsyncMock(return_value=("198.51.100", "ua-hash-b"))):
                    with mock.patch.object(mod, "_get_payment_identity_hash", new=mock.AsyncMock(return_value="identity-456")):
                        with mock.patch.object(mod, "_has_duplicate_identity_under_referrer", new=mock.AsyncMock(return_value=False)):
                            result = await mod.fraud_detect_referral_pattern(
                                referred_user_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                                trigger_payment_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                            )

        self.assertFalse(result.blocked)
        self.assertIsNone(result.outcome)
