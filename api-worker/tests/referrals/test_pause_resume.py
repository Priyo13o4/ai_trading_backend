import importlib
import os
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TEST_FILE = pathlib.Path(__file__).resolve()
WORKER_ROOT = TEST_FILE.parents[2]
REPO_ROOT = TEST_FILE.parents[3]
COMMON_ROOT = REPO_ROOT / "common"

# Ensure worker-local imports (`app.*`) and shared package imports (`trading_common.*`) resolve.
for path in (str(WORKER_ROOT), str(COMMON_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

MODULE_NAME = "app.referrals.pause_resume"


class PauseResumeWorkerTests(unittest.TestCase):
    def _load_module(self):
        with mock.patch.dict(
            os.environ,
            {
                "RAZORPAY_KEY_ID": "rzp_test_x",
                "RAZORPAY_KEY_SECRET": "secret_x",
                "REFERRAL_REWARD_FREE_MONTHS_PER_CLAIM": "2",
                "SUPABASE_URL": "https://fake.supabase.co",
                "SUPABASE_SECRET_KEY": "fake_secret_key",
            },
            clear=False,
        ):
            sys.modules.pop(MODULE_NAME, None)
            module = importlib.import_module(MODULE_NAME)
            return importlib.reload(module)

    def test_derive_cycle_duration_seconds_from_provider_timestamps(self):
        mod = self._load_module()

        last_charge = datetime(2026, 3, 1, tzinfo=timezone.utc)
        next_charge = datetime(2026, 4, 1, tzinfo=timezone.utc)

        derived = mod._derive_cycle_duration_seconds(last_charge, next_charge)

        self.assertEqual(derived, 31 * 24 * 3600)

    def test_pause_pending_cycle_updates_to_paused(self):
        mod = self._load_module()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.return_value = {"status": "active"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)

        with mock.patch.object(mod, "_assert_runtime_preconditions", return_value=None):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=1):
                    with mock.patch.object(
                        mod,
                        "_fetch_pause_candidates",
                        return_value=[
                            {
                                "reward_id": "11111111-1111-1111-1111-111111111111",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_abc",
                                "last_charge_at": now_utc - timedelta(days=30),
                                "next_charge_at": now_utc + timedelta(days=30),
                                "status": "reward_pending",
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_promote_pause_pending_if_needed", return_value=True):
                            with mock.patch.object(mod, "_fetch_resume_candidates", return_value=[]):
                                with mock.patch.object(mod, "_find_open_pause_window", return_value=None):
                                    with mock.patch.object(mod, "_utc_now", return_value=now_utc):
                                        with mock.patch.object(mod, "_mark_pause_success", return_value=True) as mark_pause:
                                            stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.seeded_cycles, 1)
        self.assertEqual(stats.paused_success, 1)
        self.assertEqual(stats.resumed_success, 0)
        self.assertTrue(mark_pause.called)

    def test_defer_when_next_charge_within_48_hours(self):
        mod = self._load_module()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.return_value = {"status": "active"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)
        next_charge = now_utc + timedelta(hours=24)

        with mock.patch.object(mod, "_assert_runtime_preconditions", return_value=None):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_fetch_pause_candidates",
                        return_value=[
                            {
                                "reward_id": "22222222-2222-2222-2222-222222222222",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_xyz",
                                "last_charge_at": now_utc - timedelta(days=30),
                                "next_charge_at": next_charge,
                                "status": "reward_pending",
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_promote_pause_pending_if_needed", return_value=True):
                            with mock.patch.object(mod, "_fetch_resume_candidates", return_value=[]):
                                with mock.patch.object(mod, "_find_open_pause_window", return_value=None):
                                    with mock.patch.object(mod, "_mark_pause_deferred", return_value=True):
                                        stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.deferred_pending, 1)
        fake_client.pause_subscription.assert_not_called()

    def test_stacked_reward_extends_pause_window_without_new_provider_pause(self):
        mod = self._load_module()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)
        extended_until = now_utc + timedelta(days=62)

        with mock.patch.object(mod, "_assert_runtime_preconditions", return_value=None):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_fetch_pause_candidates",
                        return_value=[
                            {
                                "reward_id": "23232323-2323-2323-2323-232323232323",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_shared",
                                "last_charge_at": now_utc - timedelta(days=31),
                                "next_charge_at": now_utc,
                                "status": "reward_pending",
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_promote_pause_pending_if_needed", return_value=True):
                            with mock.patch.object(mod, "_fetch_resume_candidates", return_value=[]):
                                with mock.patch.object(
                                    mod,
                                    "_find_open_pause_window",
                                    return_value={"reward_id": "base_reward", "cycle_number": 1, "resume_time": now_utc},
                                ):
                                    with mock.patch.object(mod, "_extend_pause_window", return_value=extended_until):
                                        with mock.patch.object(mod, "_mark_reward_consumed_by_extension", return_value=True):
                                            stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.extended_rewards, 1)
        fake_client.pause_subscription.assert_not_called()

    def test_pause_failure_marks_pause_failed_retryable(self):
        mod = self._load_module()

        fake_client = mock.Mock()
        fake_client.pause_subscription.side_effect = RuntimeError("provider down")
        fake_client.resume_subscription.return_value = {"status": "active"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)

        with mock.patch.object(mod, "_assert_runtime_preconditions", return_value=None):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_fetch_pause_candidates",
                        return_value=[
                            {
                                "reward_id": "33333333-3333-3333-3333-333333333333",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_retry",
                                "last_charge_at": now_utc - timedelta(days=30),
                                "next_charge_at": now_utc + timedelta(days=30),
                                "status": "reward_pending",
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_promote_pause_pending_if_needed", return_value=True):
                            with mock.patch.object(mod, "_fetch_resume_candidates", return_value=[]):
                                with mock.patch.object(mod, "_find_open_pause_window", return_value=None):
                                    with mock.patch.object(mod, "_utc_now", return_value=now_utc):
                                        with mock.patch.object(mod, "_mark_pause_success", return_value=True) as mark_pause:
                                            with mock.patch.object(mod, "_mark_pause_failed", return_value=True) as mark_pause_failed:
                                                stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.paused_success, 0)
        self.assertFalse(mark_pause.called)
        self.assertTrue(mark_pause_failed.called)

    def test_resume_failure_marks_resume_failed_retryable(self):
        mod = self._load_module()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.side_effect = RuntimeError("resume failed")

        with mock.patch.object(mod, "_assert_runtime_preconditions", return_value=None):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(mod, "_fetch_pause_candidates", return_value=[]):
                        with mock.patch.object(
                            mod,
                            "_fetch_resume_candidates",
                            return_value=[
                                {
                                    "reward_id": "44444444-4444-4444-4444-444444444444",
                                    "cycle_number": 1,
                                    "razorpay_pause_id": "pause_retry",
                                    "razorpay_subscription_id": "sub_retry",
                                    "status": "resume_pending",
                                }
                            ],
                        ):
                            with mock.patch.object(mod, "_promote_resume_pending_if_needed", return_value=True):
                                with mock.patch.object(mod, "_mark_resume_success", return_value=True) as mark_resume:
                                    with mock.patch.object(mod, "_mark_resume_failed", return_value=True) as mark_resume_failed:
                                        stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.resumed_success, 0)
        self.assertFalse(mark_resume.called)
        self.assertTrue(mark_resume_failed.called)

    def test_seed_pending_cycles_query_targets_claimed_and_is_idempotent(self):
        mod = self._load_module()

        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)

        claimed_rewards = [
            {"referral_id": "reward_123", "user_id": "user_abc", "status": "claimed"}
        ]
        user_subscriptions = [
            {
                "user_id": "user_abc",
                "external_subscription_id": "sub_abc",
                "last_payment_date": "2026-03-01T00:00:00Z",
                "next_billing_date": "2026-04-01T00:00:00Z",
            }
        ]
        existing_cycles = []

        def mock_table_get(table, params):
            if table == "referral_rewards":
                return claimed_rewards
            elif table == "user_subscriptions":
                return user_subscriptions
            elif table == "referral_reward_pause_cycles":
                return existing_cycles
            return []

        mock_inserted_rows = []
        def mock_table_insert(table, payload, **kwargs):
            nonlocal mock_inserted_rows
            if table == "referral_reward_pause_cycles":
                mock_inserted_rows = payload
                return payload
            return []

        with mock.patch.object(mod, "_table_get", side_effect=mock_table_get):
            with mock.patch.object(mod, "_table_insert", side_effect=mock_table_insert):
                with mock.patch.object(mod, "_utc_now", return_value=now_utc):
                    inserted = mod._seed_pending_cycles(batch_size=10)

        self.assertEqual(inserted, 1)
        self.assertEqual(len(mock_inserted_rows), 1)
        self.assertEqual(mock_inserted_rows[0]["reward_id"], "reward_123")
        self.assertEqual(mock_inserted_rows[0]["status"], "reward_pending")
        self.assertEqual(mock_inserted_rows[0]["razorpay_subscription_id"], "sub_abc")

    def test_fetch_pause_candidates_filters_deferred(self):
        mod = self._load_module()
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)

        mock_cycles = [
            {
                "reward_id": "reward_active",
                "status": "reward_pending",
                "pause_deferred_until": None,
            },
            {
                "reward_id": "reward_deferred",
                "status": "reward_pending",
                "pause_deferred_until": "2026-03-27T00:00:00Z", # future
            }
        ]

        with mock.patch.object(mod, "_table_get", return_value=mock_cycles) as mock_get:
            with mock.patch.object(mod, "_utc_now", return_value=now_utc):
                candidates = mod._fetch_pause_candidates(batch_size=5)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["reward_id"], "reward_active")
        mock_get.assert_called_once()
        called_params = mock_get.call_args[1]["params"]
        self.assertEqual(called_params["status"], "in.(reward_pending,pause_pending,pause_failed)")

    def test_fetch_resume_candidates_filters_future_end_time(self):
        mod = self._load_module()
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)

        mock_cycles = [
            {
                "reward_id": "reward_due",
                "status": "paused",
                "pause_end_time": "2026-03-25T00:00:00Z", # past
                "pause_confirmed": True,
            },
            {
                "reward_id": "reward_not_due",
                "status": "paused",
                "pause_end_time": "2026-03-27T00:00:00Z", # future
                "pause_confirmed": True,
            }
        ]

        with mock.patch.object(mod, "_table_get", return_value=mock_cycles) as mock_get:
            with mock.patch.object(mod, "_utc_now", return_value=now_utc):
                candidates = mod._fetch_resume_candidates(batch_size=5)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["reward_id"], "reward_due")
        mock_get.assert_called_once()
        called_params = mock_get.call_args[1]["params"]
        self.assertEqual(called_params["status"], "in.(paused,resume_pending,resume_failed)")
        self.assertEqual(called_params["pause_confirmed"], "eq.true")

    def test_idempotent_reprocessing_stale_extension_does_not_regress_state(self):
        mod = self._load_module()
        fake_client = mock.Mock()
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)
        extended_until = now_utc + timedelta(days=62)

        with mock.patch.object(mod, "_assert_runtime_preconditions", return_value=None):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_fetch_pause_candidates",
                        return_value=[
                            {
                                "reward_id": "55555555-5555-5555-5555-555555555555",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_shared",
                                "last_charge_at": now_utc - timedelta(days=31),
                                "next_charge_at": now_utc,
                                "status": "reward_pending",
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_promote_pause_pending_if_needed", return_value=True):
                            with mock.patch.object(mod, "_fetch_resume_candidates", return_value=[]):
                                with mock.patch.object(
                                    mod,
                                    "_find_open_pause_window",
                                    return_value={"reward_id": "base_reward", "cycle_number": 1, "resume_time": now_utc},
                                ):
                                    with mock.patch.object(mod, "_extend_pause_window", return_value=extended_until):
                                        with mock.patch.object(mod, "_mark_reward_consumed_by_extension", return_value=False):
                                            stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.extended_rewards, 0)
        self.assertEqual(stats.paused_success, 0)
        fake_client.pause_subscription.assert_not_called()

    def test_resume_provider_http_error_marks_resume_failed_retryable(self):
        mod = self._load_module()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.side_effect = RuntimeError(
            "resume_failed status=500 body=internal error"
        )

        with mock.patch.object(mod, "_assert_runtime_preconditions", return_value=None):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(mod, "_fetch_pause_candidates", return_value=[]):
                        with mock.patch.object(
                            mod,
                            "_fetch_resume_candidates",
                            return_value=[
                                {
                                    "reward_id": "66666666-6666-6666-6666-666666666666",
                                    "cycle_number": 1,
                                    "razorpay_pause_id": "pause_http_500",
                                    "razorpay_subscription_id": "sub_http_500",
                                    "status": "resume_pending",
                                }
                            ],
                        ):
                            with mock.patch.object(mod, "_promote_resume_pending_if_needed", return_value=True):
                                with mock.patch.object(mod, "_mark_resume_success", return_value=True) as mark_resume:
                                    with mock.patch.object(mod, "_mark_resume_failed", return_value=True) as mark_resume_failed:
                                        stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.resumed_success, 0)
        self.assertFalse(mark_resume.called)
        self.assertTrue(mark_resume_failed.called)


if __name__ == "__main__":
    unittest.main()
