import importlib
import os
import sys
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.referrals.manual_activation import ManualActivationResult


MODULE_NAME = "app.referrals.manual_activation"
ROUTES_MODULE_NAME = "app.routes.referrals"


def _reload_module(env: dict[str, str] | None = None):
    base_env = {
        "SUPABASE_PROJECT_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": "service-role-key",
    }
    if env:
        base_env.update(env)

    sys.modules.pop(MODULE_NAME, None)
    with mock.patch.dict(os.environ, base_env, clear=False):
        module = importlib.import_module(MODULE_NAME)
        return importlib.reload(module)


def _reload_routes_module(env: dict[str, str] | None = None):
    base_env = {
        "SUPABASE_PROJECT_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": "service-role-key",
        "SESSION_REDIS_URL": "redis://localhost:6379/0",
        "REDIS_PASSWORD": "test-password",
        "AUTH_ENV": "test",
    }
    if env:
        base_env.update(env)

    sys.modules.pop(ROUTES_MODULE_NAME, None)
    with mock.patch.dict(os.environ, base_env, clear=False):
        module = importlib.import_module(ROUTES_MODULE_NAME)
        return importlib.reload(module)


class ManualActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_maps_result_and_claimed_rewards(self):
        mod = _reload_module()

        mock_response = mock.Mock(data=[{
            "result_code": "success",
            "activated_months": 2,
            "qualified_count": 10,
            "next_threshold": 15,
            "remaining_referrals_for_next": 5,
            "claimed_reward_ids": [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                result = await mod.activate_referral_reward_manual(
                    current_user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    referral_code="ABCD1234",
                )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.activated_months, 2)
        self.assertEqual(result.qualified_count, 10)
        self.assertEqual(result.next_threshold, 15)
        self.assertEqual(result.remaining_referrals_for_next, 5)
        self.assertEqual(len(result.claimed_reward_ids), 2)

        rpc_name, payload = mock_supabase.rpc.call_args.args
        self.assertEqual(rpc_name, "activate_referral_reward_manual")
        self.assertEqual(payload["p_referral_code"], "ABCD1234")

    async def test_insufficient_referrals_returns_error_code(self):
        mod = _reload_module()

        mock_response = mock.Mock(data=[{
            "result_code": "insufficient_referrals",
            "activated_months": 0,
            "qualified_count": 3,
            "next_threshold": 5,
            "remaining_referrals_for_next": 2,
            "claimed_reward_ids": [],
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                result = await mod.activate_referral_reward_manual(
                    current_user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                )

        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.error_code, "insufficient_referrals")
        self.assertEqual(result.remaining_referrals_for_next, 2)

    async def test_already_claimed_all_represents_idempotent_repeat(self):
        mod = _reload_module()

        mock_response = mock.Mock(data=[{
            "result_code": "already_claimed_all",
            "activated_months": 0,
            "qualified_count": 0,
            "next_threshold": 15,
            "remaining_referrals_for_next": 5,
            "claimed_reward_ids": [],
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder

        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                result = await mod.activate_referral_reward_manual(
                    current_user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                )

        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.error_code, "already_claimed_all")

    async def test_internal_error_when_rpc_fails(self):
        mod = _reload_module()

        mock_supabase = mock.Mock()
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=RuntimeError("db down"))):
                result = await mod.activate_referral_reward_manual(
                    current_user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                )

        self.assertEqual(result.outcome, "error")
        self.assertEqual(result.error_code, "internal_error")


class ManualActivationEndpointTests(unittest.TestCase):
    @staticmethod
    def _make_client(routes_module):
        app = FastAPI()
        app.include_router(routes_module.referrals_router)
        app.dependency_overrides[routes_module.require_session] = lambda: {
            "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        }
        return TestClient(app)

    def test_activate_rewards_success_returns_200_with_typed_payload(self):
        routes = _reload_routes_module()

        with mock.patch.object(
            routes,
            "activate_referral_reward_manual",
            new=mock.AsyncMock(
                return_value=ManualActivationResult(
                    outcome="success",
                    activated_months=2,
                    qualified_count=10,
                    next_threshold=15,
                    remaining_referrals_for_next=5,
                )
            ),
        ):
            with self._make_client(routes) as client:
                response = client.post("/api/referrals/activate-rewards", json={"referral_code": "ABCD1234"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["activated_months"], 2)
        self.assertEqual(body["next_threshold"], 15)
        self.assertEqual(body["remaining_referrals_for_next"], 5)
        self.assertEqual(body["qualified_referrals"], 10)
        self.assertNotIn("error_code", body)

    def test_activate_rewards_insufficient_referrals_returns_400_with_typed_error(self):
        routes = _reload_routes_module()

        with mock.patch.object(
            routes,
            "activate_referral_reward_manual",
            new=mock.AsyncMock(
                return_value=ManualActivationResult(
                    outcome="error",
                    error_code="insufficient_referrals",
                    error_message="Not enough qualified referrals to activate a free month yet.",
                    qualified_count=3,
                    next_threshold=5,
                    remaining_referrals_for_next=2,
                )
            ),
        ):
            with self._make_client(routes) as client:
                response = client.post("/api/referrals/activate-rewards", json={})

        self.assertEqual(response.status_code, 400)
        detail = response.json()["detail"]
        self.assertEqual(detail["error_code"], "insufficient_referrals")
        self.assertIn("message", detail)
        self.assertEqual(detail["threshold"], 5)
        self.assertEqual(detail["next_threshold"], 5)

    def test_activate_rewards_retry_after_success_returns_409_already_claimed_all(self):
        routes = _reload_routes_module()
        call_count = {"value": 0}

        async def _stateful_activation(*, current_user_id: str, referral_code: str | None = None):
            del current_user_id, referral_code
            call_count["value"] += 1
            if call_count["value"] == 1:
                return ManualActivationResult(
                    outcome="success",
                    activated_months=1,
                    qualified_count=5,
                    next_threshold=10,
                    remaining_referrals_for_next=5,
                )

            return ManualActivationResult(
                outcome="error",
                error_code="already_claimed_all",
                error_message="All currently earned referral months are already claimed.",
                activated_months=0,
                qualified_count=5,
                next_threshold=10,
                remaining_referrals_for_next=5,
            )

        with mock.patch.object(routes, "activate_referral_reward_manual", new=_stateful_activation):
            with self._make_client(routes) as client:
                first_response = client.post("/api/referrals/activate-rewards", json={})
                second_response = client.post("/api/referrals/activate-rewards", json={})

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["activated_months"], 1)
        self.assertEqual(second_response.status_code, 409)
        second_detail = second_response.json()["detail"]
        self.assertEqual(second_detail["error_code"], "already_claimed_all")
        self.assertIn("message", second_detail)
        self.assertEqual(second_detail["threshold"], 10)
        self.assertEqual(call_count["value"], 2)


if __name__ == "__main__":
    unittest.main()
