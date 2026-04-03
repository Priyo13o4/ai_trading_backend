import importlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Response
from starlette.requests import Request



def _reload_routes_module():
    sys.modules.pop("app.authn.routes", None)
    module = importlib.import_module("app.authn.routes")
    return importlib.reload(module)



def _reload_trial_policy_module():
    sys.modules.pop("app.authn.trial_policy", None)
    module = importlib.import_module("app.authn.trial_policy")
    return importlib.reload(module)



def _make_exchange_request(
    body: dict[str, object],
    *,
    headers: dict[str, str] | None = None,
    client_host: str = "127.0.0.1",
) -> Request:
    merged_headers = {"content-type": "application/json"}
    if headers:
        merged_headers.update(headers)

    raw_body = json.dumps(body).encode("utf-8")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/auth/exchange",
        "headers": [
            (k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in merged_headers.items()
        ],
        "client": (client_host, 50100),
    }

    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(scope, receive)


class Scope2RoutesTests(unittest.IsolatedAsyncioTestCase):
    def _load_routes(self):
        env = {
            "SESSION_REDIS_URL": "redis://localhost:6379/0",
            "REDIS_PASSWORD": "test-password",
            "AUTH_ENV": "test",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return _reload_routes_module()

    async def test_disposable_domain_rejected_with_exact_message(self):
        routes = self._load_routes()
        request = _make_exchange_request({"access_token": "fake-access-token", "device_id": "dev-1"})
        response = Response()

        with mock.patch.object(routes, "rate_limit", new=mock.AsyncMock()):
            with mock.patch.object(
                routes,
                "verify_supabase_access_token",
                new=mock.AsyncMock(
                    return_value={
                        "sub": "11111111-1111-1111-1111-111111111111",
                        "exp": 1_900_000_000,
                        "email": "test@mailinator.com",
                    }
                ),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await routes.auth_exchange(request, response)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Please use a supported email provider (e.g., Gmail, Outlook).")

    async def test_upstream_disposable_domain_fun4k_is_rejected(self):
        routes = self._load_routes()
        request = _make_exchange_request({"access_token": "fake-access-token", "device_id": "dev-1"})
        response = Response()

        with mock.patch.object(routes, "rate_limit", new=mock.AsyncMock()):
            with mock.patch.object(
                routes,
                "verify_supabase_access_token",
                new=mock.AsyncMock(
                    return_value={
                        "sub": "11111111-1111-1111-1111-111111111111",
                        "exp": 1_900_000_000,
                        "email": "pixepab636@fun4k.com",
                    }
                ),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await routes.auth_exchange(request, response)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Please use a supported email provider (e.g., Gmail, Outlook).")

    async def test_second_signup_same_device_disables_trial_but_exchange_succeeds(self):
        routes = self._load_routes()
        request = _make_exchange_request({"access_token": "fake-access-token", "device_id": "dev-repeat"})
        response = Response()

        deny_outcome = SimpleNamespace(
            trial_allowed=False,
            reason="deny_same_device",
            device_id_hash="abc",
            had_active_trial=True,
        )

        with mock.patch.object(routes, "rate_limit", new=mock.AsyncMock()):
            with mock.patch.object(
                routes,
                "verify_supabase_access_token",
                new=mock.AsyncMock(
                    return_value={
                        "sub": "22222222-2222-2222-2222-222222222222",
                        "exp": 1_900_000_000,
                        "email": "real.user@gmail.com",
                    }
                ),
            ):
                with mock.patch.object(
                    routes,
                    "apply_trial_policy_for_exchange",
                    new=mock.AsyncMock(return_value=deny_outcome),
                ):
                    with mock.patch.object(routes, "invalidate_perms", new=mock.AsyncMock()):
                        with mock.patch.object(routes, "get_cached_perms", new=mock.AsyncMock(return_value=None)):
                            with mock.patch.object(routes, "rpc_get_active_subscription", new=mock.AsyncMock(return_value=None)):
                                with mock.patch.object(routes, "set_cached_perms", new=mock.AsyncMock()):
                                    with mock.patch.object(
                                        routes,
                                        "create_session",
                                        new=mock.AsyncMock(return_value={"sid": "sid-x", "ttl": 3600, "evicted_count": 0}),
                                    ):
                                        with mock.patch.object(
                                            routes,
                                            "capture_referral_attribution_from_exchange",
                                            new=mock.AsyncMock(return_value="skip:no_referral_code"),
                                        ):
                                            result = await routes.auth_exchange(request, response)

        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"], "free")

    async def test_referral_access_requires_pause_confirmed(self):
        routes = self._load_routes()
        free_until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

        no_confirm_plan, no_confirm_permissions = routes._derive_plan_and_permissions_from_subscription(
            {
                "is_current": False,
                "status": "cancelled",
                "plan_name": "pro",
                "free_access_until": free_until,
                "pause_confirmed": False,
            }
        )
        confirmed_plan, confirmed_permissions = routes._derive_plan_and_permissions_from_subscription(
            {
                "is_current": False,
                "status": "cancelled",
                "plan_name": "pro",
                "free_access_until": free_until,
                "pause_confirmed": True,
            }
        )

        self.assertEqual(no_confirm_plan, "free")
        self.assertEqual(no_confirm_permissions, ["dashboard"])
        self.assertEqual(confirmed_plan, "pro")
        self.assertEqual(confirmed_permissions, ["dashboard", "signals"])


class Scope2TrialPolicyTests(unittest.IsolatedAsyncioTestCase):
    def _load_trial_policy(self):
        env = {
            "SUPABASE_PROJECT_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "service-role-key",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return _reload_trial_policy_module()

    async def test_first_device_gets_trial(self):
        trial_policy = self._load_trial_policy()
        active_trial = {
            "id": "sub-1",
            "metadata": {"trial_source": "signup"},
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

        with mock.patch.object(
            trial_policy,
            "_get_active_trial_subscription",
            new=mock.AsyncMock(return_value=active_trial),
        ):
            with mock.patch.object(
                trial_policy,
                "_mark_device_trial_first_use",
                new=mock.AsyncMock(return_value=True),
            ):
                with mock.patch.object(trial_policy, "_disable_trial_entitlement", new=mock.AsyncMock()) as disable_mock:
                    result = await trial_policy.apply_trial_policy_for_exchange(
                        user_id="33333333-3333-3333-3333-333333333333",
                        device_id="fresh-device",
                        user_agent="Mozilla/5.0",
                        ip_address="203.0.113.5",
                    )

        self.assertTrue(result.trial_allowed)
        self.assertEqual(result.reason, "allow_first_device_trial")
        disable_mock.assert_not_awaited()

    async def test_ip_and_ua_do_not_deny_trial_when_device_differs(self):
        trial_policy = self._load_trial_policy()
        active_trial = {
            "id": "sub-2",
            "metadata": {"trial_source": "signup"},
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

        with mock.patch.object(
            trial_policy,
            "_get_active_trial_subscription",
            new=mock.AsyncMock(return_value=active_trial),
        ):
            with mock.patch.object(
                trial_policy,
                "_mark_device_trial_first_use",
                new=mock.AsyncMock(return_value=True),
            ) as first_use_mock:
                result = await trial_policy.apply_trial_policy_for_exchange(
                    user_id="44444444-4444-4444-4444-444444444444",
                    device_id="different-device",
                    user_agent="CompletelyDifferentUA/1.0",
                    ip_address="198.51.100.99",
                )

        self.assertTrue(result.trial_allowed)
        self.assertEqual(result.reason, "allow_first_device_trial")
        first_use_mock.assert_awaited_once()

    async def test_same_email_reclaim_does_not_disable_trial_on_repeat_device(self):
        trial_policy = self._load_trial_policy()
        reclaim_trial = {
            "id": "sub-3",
            "metadata": {"trial_source": "same_email_reclaim_resume"},
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

        with mock.patch.object(
            trial_policy,
            "_get_active_trial_subscription",
            new=mock.AsyncMock(return_value=reclaim_trial),
        ):
            with mock.patch.object(
                trial_policy,
                "_mark_device_trial_first_use",
                new=mock.AsyncMock(return_value=False),
            ):
                with mock.patch.object(trial_policy, "_disable_trial_entitlement", new=mock.AsyncMock()) as disable_mock:
                    result = await trial_policy.apply_trial_policy_for_exchange(
                        user_id="55555555-5555-5555-5555-555555555555",
                        device_id="reclaim-device",
                        user_agent="Mozilla/5.0",
                        ip_address="203.0.113.10",
                    )

        self.assertTrue(result.trial_allowed)
        self.assertEqual(result.reason, "allow_same_email_reclaim")
        disable_mock.assert_not_awaited()

    async def test_repeat_device_non_reclaim_still_disables_trial(self):
        trial_policy = self._load_trial_policy()
        normal_trial = {
            "id": "sub-4",
            "metadata": {"trial_source": "signup"},
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

        with mock.patch.object(
            trial_policy,
            "_get_active_trial_subscription",
            new=mock.AsyncMock(return_value=normal_trial),
        ):
            with mock.patch.object(
                trial_policy,
                "_mark_device_trial_first_use",
                new=mock.AsyncMock(return_value=False),
            ):
                with mock.patch.object(trial_policy, "_disable_trial_entitlement", new=mock.AsyncMock()) as disable_mock:
                    result = await trial_policy.apply_trial_policy_for_exchange(
                        user_id="66666666-6666-6666-6666-666666666666",
                        device_id="repeat-device",
                        user_agent="Mozilla/5.0",
                        ip_address="203.0.113.11",
                    )

        self.assertFalse(result.trial_allowed)
        self.assertEqual(result.reason, "deny_same_device")
        disable_mock.assert_awaited_once()
