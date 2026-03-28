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


class _FakeConnection:
    def __init__(self) -> None:
        self.autocommit = False
        self.commit_calls = 0
        self.rollback_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeCursor:
    def __init__(self, *, rowcount: int = 0, rows: list[dict] | None = None) -> None:
        self.rowcount = rowcount
        self.rows = rows or []
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, query: str, params: tuple | None = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[dict]:
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _CursorConnection:
    def __init__(self, cursors: list[_FakeCursor]) -> None:
        self._cursors = cursors
        self._index = 0

    def cursor(self, *args, **kwargs) -> _FakeCursor:
        cursor = self._cursors[self._index]
        self._index += 1
        return cursor


class PauseResumeWorkerTests(unittest.TestCase):
    def _load_module(self):
        with mock.patch.dict(
            os.environ,
            {
                "RAZORPAY_KEY_ID": "rzp_test_x",
                "RAZORPAY_KEY_SECRET": "secret_x",
                "REFERRAL_REWARD_FREE_MONTHS_PER_CLAIM": "2",
            },
            clear=False,
        ):
            import sys

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
        fake_conn = _FakeConnection()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.return_value = {"status": "active"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)

        with mock.patch.object(mod.psycopg, "connect", return_value=fake_conn):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=1):
                    with mock.patch.object(
                        mod,
                        "_claim_pending_cycles",
                        return_value=[
                            {
                                "reward_id": "11111111-1111-1111-1111-111111111111",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_abc",
                                "last_charge_at": now_utc - timedelta(days=30),
                                "next_charge_at": now_utc + timedelta(days=30),
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_claim_due_resumes", return_value=[]):
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
        fake_conn = _FakeConnection()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.return_value = {"status": "active"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)
        next_charge = now_utc + timedelta(hours=24)

        with mock.patch.object(mod.psycopg, "connect", return_value=fake_conn):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_claim_pending_cycles",
                        return_value=[
                            {
                                "reward_id": "22222222-2222-2222-2222-222222222222",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_xyz",
                                "last_charge_at": now_utc - timedelta(days=30),
                                "next_charge_at": next_charge,
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_claim_due_resumes", return_value=[]):
                            with mock.patch.object(mod, "_find_open_pause_window", return_value=None):
                                with mock.patch.object(mod, "_mark_pause_deferred", return_value=True):
                                    stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.deferred_pending, 1)
        fake_client.pause_subscription.assert_not_called()

    def test_stacked_reward_extends_pause_window_without_new_provider_pause(self):
        mod = self._load_module()
        fake_conn = _FakeConnection()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)
        extended_until = now_utc + timedelta(days=62)

        with mock.patch.object(mod.psycopg, "connect", return_value=fake_conn):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_claim_pending_cycles",
                        return_value=[
                            {
                                "reward_id": "23232323-2323-2323-2323-232323232323",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_shared",
                                "last_charge_at": now_utc - timedelta(days=31),
                                "next_charge_at": now_utc,
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_claim_due_resumes", return_value=[]):
                            with mock.patch.object(
                                mod,
                                "_find_open_pause_window",
                                return_value={"reward_id": "base_reward", "cycle_number": 1},
                            ):
                                with mock.patch.object(mod, "_extend_pause_window", return_value=extended_until):
                                    with mock.patch.object(mod, "_mark_reward_consumed_by_extension", return_value=True):
                                        stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.extended_rewards, 1)
        fake_client.pause_subscription.assert_not_called()

    def test_pause_failure_marks_pause_failed_retryable(self):
        mod = self._load_module()
        fake_conn = _FakeConnection()

        fake_client = mock.Mock()
        fake_client.pause_subscription.side_effect = RuntimeError("provider down")
        fake_client.resume_subscription.return_value = {"status": "active"}
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)

        with mock.patch.object(mod.psycopg, "connect", return_value=fake_conn):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_claim_pending_cycles",
                        return_value=[
                            {
                                "reward_id": "33333333-3333-3333-3333-333333333333",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_retry",
                                "last_charge_at": now_utc - timedelta(days=30),
                                "next_charge_at": now_utc + timedelta(days=30),
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_claim_due_resumes", return_value=[]):
                            with mock.patch.object(mod, "_find_open_pause_window", return_value=None):
                                with mock.patch.object(mod, "_utc_now", return_value=now_utc):
                                    with mock.patch.object(mod, "_mark_pause_success", return_value=True) as mark_pause:
                                        with mock.patch.object(mod, "_mark_pause_failed", return_value=True) as mark_pause_failed:
                                            stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.paused_success, 0)
        self.assertFalse(mark_pause.called)
        self.assertTrue(mark_pause_failed.called)
        self.assertGreaterEqual(fake_conn.rollback_calls, 1)

    def test_resume_failure_marks_resume_failed_retryable(self):
        mod = self._load_module()
        fake_conn = _FakeConnection()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.side_effect = RuntimeError("resume failed")

        with mock.patch.object(mod.psycopg, "connect", return_value=fake_conn):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(mod, "_claim_pending_cycles", return_value=[]):
                        with mock.patch.object(
                            mod,
                            "_claim_due_resumes",
                            return_value=[
                                {
                                    "reward_id": "44444444-4444-4444-4444-444444444444",
                                    "cycle_number": 1,
                                    "razorpay_pause_id": "pause_retry",
                                    "razorpay_subscription_id": "sub_retry",
                                }
                            ],
                        ):
                            with mock.patch.object(mod, "_mark_resume_success", return_value=True) as mark_resume:
                                with mock.patch.object(mod, "_mark_resume_failed", return_value=True) as mark_resume_failed:
                                    stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.resumed_success, 0)
        self.assertFalse(mark_resume.called)
        self.assertTrue(mark_resume_failed.called)
        self.assertGreaterEqual(fake_conn.rollback_calls, 1)

    def test_seed_pending_cycles_query_targets_claimed_and_is_idempotent(self):
        mod = self._load_module()
        seed_cursor = _FakeCursor(rowcount=1)
        conn = _CursorConnection([seed_cursor])

        inserted = mod._seed_pending_cycles(conn)

        self.assertEqual(inserted, 1)
        self.assertEqual(len(seed_cursor.executed), 1)
        query, params = seed_cursor.executed[0]
        self.assertIsNone(params)
        self.assertIn("WHERE rr.status = 'claimed'", query)
        self.assertIn("ON CONFLICT (reward_id, cycle_number) DO NOTHING", query)
        self.assertIn("'reward_pending'::public.referral_pause_cycle_status", query)

    def test_claim_pending_cycles_promotes_to_pause_pending(self):
        mod = self._load_module()
        claim_cursor = _FakeCursor(rows=[])
        conn = _CursorConnection([claim_cursor])

        _ = mod._claim_pending_cycles(conn, batch_size=5)

        self.assertEqual(len(claim_cursor.executed), 1)
        query, params = claim_cursor.executed[0]
        self.assertEqual(params, (5,))
        self.assertIn("status IN ('reward_pending', 'pause_pending', 'pause_failed')", query)
        self.assertIn("SET status = 'pause_pending'", query)

    def test_claim_due_resumes_promotes_to_resume_pending(self):
        mod = self._load_module()
        claim_cursor = _FakeCursor(rows=[])
        conn = _CursorConnection([claim_cursor])

        _ = mod._claim_due_resumes(conn, batch_size=7)

        self.assertEqual(len(claim_cursor.executed), 1)
        query, params = claim_cursor.executed[0]
        self.assertEqual(params, (7,))
        self.assertIn("status IN ('paused', 'resume_pending', 'resume_failed')", query)
        self.assertIn("SET status = 'resume_pending'", query)

    def test_idempotent_reprocessing_stale_extension_does_not_regress_state(self):
        mod = self._load_module()
        fake_conn = _FakeConnection()
        fake_client = mock.Mock()
        now_utc = datetime(2026, 3, 26, tzinfo=timezone.utc)
        extended_until = now_utc + timedelta(days=62)

        with mock.patch.object(mod.psycopg, "connect", return_value=fake_conn):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(
                        mod,
                        "_claim_pending_cycles",
                        return_value=[
                            {
                                "reward_id": "55555555-5555-5555-5555-555555555555",
                                "cycle_number": 1,
                                "razorpay_subscription_id": "sub_shared",
                                "last_charge_at": now_utc - timedelta(days=31),
                                "next_charge_at": now_utc,
                            }
                        ],
                    ):
                        with mock.patch.object(mod, "_claim_due_resumes", return_value=[]):
                            with mock.patch.object(
                                mod,
                                "_find_open_pause_window",
                                return_value={"reward_id": "base_reward", "cycle_number": 1},
                            ):
                                with mock.patch.object(mod, "_extend_pause_window", return_value=extended_until):
                                    with mock.patch.object(mod, "_mark_reward_consumed_by_extension", return_value=False):
                                        stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.extended_rewards, 0)
        self.assertEqual(stats.paused_success, 0)
        fake_client.pause_subscription.assert_not_called()

    def test_resume_provider_http_error_marks_resume_failed_retryable(self):
        mod = self._load_module()
        fake_conn = _FakeConnection()

        fake_client = mock.Mock()
        fake_client.pause_subscription.return_value = {"pause_id": "pause_123", "status": "paused"}
        fake_client.resume_subscription.side_effect = RuntimeError(
            "resume_failed status=500 body=internal error"
        )

        with mock.patch.object(mod.psycopg, "connect", return_value=fake_conn):
            with mock.patch.object(mod, "RazorpayPauseResumeClient", return_value=fake_client):
                with mock.patch.object(mod, "_seed_pending_cycles", return_value=0):
                    with mock.patch.object(mod, "_claim_pending_cycles", return_value=[]):
                        with mock.patch.object(
                            mod,
                            "_claim_due_resumes",
                            return_value=[
                                {
                                    "reward_id": "66666666-6666-6666-6666-666666666666",
                                    "cycle_number": 1,
                                    "razorpay_pause_id": "pause_http_500",
                                    "razorpay_subscription_id": "sub_http_500",
                                }
                            ],
                        ):
                            with mock.patch.object(mod, "_mark_resume_success", return_value=True) as mark_resume:
                                with mock.patch.object(mod, "_mark_resume_failed", return_value=True) as mark_resume_failed:
                                    stats = mod.run_referral_pause_resume_cycle()

        self.assertEqual(stats.resumed_success, 0)
        self.assertFalse(mark_resume.called)
        self.assertTrue(mark_resume_failed.called)
        self.assertGreaterEqual(fake_conn.rollback_calls, 1)


if __name__ == "__main__":
    unittest.main()
