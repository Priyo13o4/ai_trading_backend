import importlib
import json
import os
import sys
import unittest
from unittest import mock

from fastapi import HTTPException, Response
from starlette.requests import Request


def _reload_routes_module():
    sys.modules.pop("app.authn.routes", None)
    module = importlib.import_module("app.authn.routes")
    return importlib.reload(module)


def _make_request(
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
        "client": (client_host, 50000),
    }

    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(scope, receive)


class RoutesTurnstileAndIpTests(unittest.IsolatedAsyncioTestCase):
    def _load_routes(self, *, turnstile_secret: str | None = None, trust_proxy_headers: str = "false"):
        env = {
            "SESSION_REDIS_URL": "redis://localhost:6379/0",
            "REDIS_PASSWORD": "test-password",
            "AUTH_ENV": "test",
            "TRUST_PROXY_HEADERS": trust_proxy_headers,
        }
        if turnstile_secret is not None:
            env["TURNSTILE_SECRET_KEY"] = turnstile_secret
            env["AUTH_EXCHANGE_TURNSTILE_ENFORCE"] = "1"

        with mock.patch.dict(os.environ, env, clear=False):
            return _reload_routes_module()

    async def test_missing_turnstile_token_is_rejected_when_secret_set(self):
        routes = self._load_routes(turnstile_secret="secret-value")
        request = _make_request({"access_token": "fake-access-token"})
        response = Response()

        with mock.patch.object(routes, "rate_limit", new=mock.AsyncMock()):
            with mock.patch.object(
                routes,
                "verify_supabase_access_token",
                new=mock.AsyncMock(return_value={"sub": "u-1", "exp": 1_900_000_000}),
            ) as verify_mock:
                with self.assertRaises(HTTPException) as ctx:
                    await routes.auth_exchange(request, response)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "turnstile_token is required")
        verify_mock.assert_not_awaited()

    def test_request_client_ip_ignores_forwarded_header_when_proxy_trust_disabled(self):
        routes = self._load_routes(trust_proxy_headers="false")
        request = _make_request(
            {"access_token": "fake-access-token"},
            headers={"x-forwarded-for": "198.51.100.7, 203.0.113.2"},
            client_host="10.1.2.3",
        )

        self.assertFalse(routes.TRUST_PROXY_HEADERS)
        self.assertEqual(routes._request_client_ip(request), "10.1.2.3")

    def test_turnstile_payload_omits_remoteip_when_proxy_trust_disabled(self):
        routes = self._load_routes(turnstile_secret="secret-value", trust_proxy_headers="false")
        request = _make_request(
            {"access_token": "fake-access-token", "turnstile_token": "tt"},
            headers={"x-forwarded-for": "198.51.100.7"},
            client_host="10.1.2.3",
        )

        payload = routes._turnstile_verify_form_payload("tt", request)

        self.assertIn("secret", payload)
        self.assertEqual(payload["response"], "tt")
        self.assertNotIn("remoteip", payload)

    def test_turnstile_payload_includes_remoteip_when_proxy_trust_enabled(self):
        routes = self._load_routes(turnstile_secret="secret-value", trust_proxy_headers="true")
        request = _make_request(
            {"access_token": "fake-access-token", "turnstile_token": "tt"},
            headers={"x-forwarded-for": "198.51.100.7, 203.0.113.2"},
            client_host="10.1.2.3",
        )

        payload = routes._turnstile_verify_form_payload("tt", request)

        self.assertIn("remoteip", payload)
        self.assertEqual(payload["remoteip"], "198.51.100.7")

    async def test_auth_exchange_attempts_referral_capture_with_session_fingerprint_values(self):
        routes = self._load_routes()
        request = _make_request({"access_token": "fake-access-token", "remember_me": False})
        response = Response()

        with mock.patch.object(routes, "rate_limit", new=mock.AsyncMock()):
            with mock.patch.object(
                routes,
                "verify_supabase_access_token",
                new=mock.AsyncMock(
                    return_value={
                        "sub": "11111111-1111-1111-1111-111111111111",
                        "exp": 1_900_000_000,
                    }
                ),
            ):
                with mock.patch.object(
                    routes,
                    "get_cached_perms",
                    new=mock.AsyncMock(return_value={"plan": "free", "permissions": []}),
                ):
                    with mock.patch.object(routes, "create_session", new=mock.AsyncMock(return_value={"sid": "sid-1", "ttl": 3600, "evicted_count": 0})):
                        with mock.patch.object(routes, "_session_binding_components", return_value=("ua-hash-1", "203.0.113")):
                            with mock.patch.object(
                                routes,
                                "capture_referral_attribution_from_exchange",
                                new=mock.AsyncMock(return_value="skip:no_referral_code"),
                            ) as capture_mock:
                                result = await routes.auth_exchange(request, response)

        self.assertTrue(result["ok"])
        capture_mock.assert_awaited_once()
        _, kwargs = capture_mock.await_args
        self.assertEqual(kwargs["referred_user_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(kwargs["user_agent"], "")
        self.assertEqual(kwargs["ip_address"], "127.0.0.1")

    async def test_auth_exchange_referral_capture_failure_does_not_break_login(self):
        routes = self._load_routes()
        request = _make_request({"access_token": "fake-access-token", "remember_me": True})
        response = Response()

        with mock.patch.object(routes, "rate_limit", new=mock.AsyncMock()):
            with mock.patch.object(
                routes,
                "verify_supabase_access_token",
                new=mock.AsyncMock(
                    return_value={
                        "sub": "22222222-2222-2222-2222-222222222222",
                        "exp": 1_900_000_000,
                    }
                ),
            ):
                with mock.patch.object(
                    routes,
                    "get_cached_perms",
                    new=mock.AsyncMock(return_value={"plan": "free", "permissions": []}),
                ):
                    with mock.patch.object(routes, "create_session", new=mock.AsyncMock(return_value={"sid": "sid-2", "ttl": 7200, "evicted_count": 0})):
                        with mock.patch.object(routes, "_session_binding_components", return_value=("ua-hash-2", "198.51.100")):
                            with mock.patch.object(
                                routes,
                                "capture_referral_attribution_from_exchange",
                                new=mock.AsyncMock(side_effect=RuntimeError("db down")),
                            ) as capture_mock:
                                result = await routes.auth_exchange(request, response)

        self.assertTrue(result["ok"])
        self.assertEqual(result["user_id"], "22222222-2222-2222-2222-222222222222")
        capture_mock.assert_awaited_once()
