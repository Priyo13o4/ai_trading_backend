import hmac
import importlib
import os
import sys
import unittest
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request


def _reload_routes_module():
    sys.modules.pop("app.authn.routes", None)
    module = importlib.import_module("app.authn.routes")
    return importlib.reload(module)


def _make_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/auth/invalidate",
        "headers": [(k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in headers.items()],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _compute_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    signed = str(timestamp).encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), signed, digestmod="sha256").hexdigest()


class SignedWebhookVerificationTests(unittest.IsolatedAsyncioTestCase):
    def _load_routes(self):
        env = {
            "SESSION_REDIS_URL": "redis://localhost:6379/0",
            "REDIS_PASSWORD": "test-password",
            "AUTH_ENV": "test",
            "AUTH_INVALIDATION_USE_SIGNED": "1",
            "AUTH_INVALIDATION_TOLERANCE_SECONDS": "300",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return _reload_routes_module()

    async def test_valid_signature_is_accepted(self):
        routes = self._load_routes()
        secret = "signing-secret"
        timestamp = 1_700_000_000
        raw_body = b'{"user_id":"u-1"}'

        request = _make_request(
            {
                "x-webhook-timestamp": str(timestamp),
                "x-webhook-id": "evt-1",
                "x-webhook-signature": _compute_signature(secret, timestamp, raw_body),
            }
        )

        with mock.patch.object(routes.time, "time", return_value=timestamp):
            with mock.patch.object(
                routes,
                "put_replay_guard_once",
                new=mock.AsyncMock(return_value=True),
            ) as guard_mock:
                await routes._verify_signed_invalidation(request, secret, raw_body)

        guard_mock.assert_awaited_once_with(
            "replay:auth_invalidate:evt-1",
            600,
        )

    async def test_invalid_signature_is_rejected(self):
        routes = self._load_routes()
        secret = "signing-secret"
        timestamp = 1_700_000_000
        raw_body = b'{"user_id":"u-1"}'

        request = _make_request(
            {
                "x-webhook-timestamp": str(timestamp),
                "x-webhook-id": "evt-2",
                "x-webhook-signature": "deadbeef",
            }
        )

        with mock.patch.object(routes.time, "time", return_value=timestamp):
            with mock.patch.object(
                routes,
                "put_replay_guard_once",
                new=mock.AsyncMock(return_value=True),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await routes._verify_signed_invalidation(request, secret, raw_body)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Invalid webhook signature")

    async def test_stale_timestamp_is_rejected(self):
        routes = self._load_routes()
        secret = "signing-secret"
        now = 1_700_000_000
        stale_timestamp = now - (routes.AUTH_INVALIDATION_TOLERANCE_SECONDS + 1)
        raw_body = b'{"user_id":"u-1"}'

        request = _make_request(
            {
                "x-webhook-timestamp": str(stale_timestamp),
                "x-webhook-id": "evt-3",
                "x-webhook-signature": _compute_signature(secret, stale_timestamp, raw_body),
            }
        )

        with mock.patch.object(routes.time, "time", return_value=now):
            with mock.patch.object(
                routes,
                "put_replay_guard_once",
                new=mock.AsyncMock(return_value=True),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await routes._verify_signed_invalidation(request, secret, raw_body)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Stale webhook timestamp")

    async def test_replayed_webhook_id_is_rejected(self):
        routes = self._load_routes()
        secret = "signing-secret"
        timestamp = 1_700_000_000
        raw_body = b'{"user_id":"u-1"}'

        request = _make_request(
            {
                "x-webhook-timestamp": str(timestamp),
                "x-webhook-id": "evt-4",
                "x-webhook-signature": _compute_signature(secret, timestamp, raw_body),
            }
        )

        with mock.patch.object(routes.time, "time", return_value=timestamp):
            with mock.patch.object(
                routes,
                "put_replay_guard_once",
                new=mock.AsyncMock(return_value=False),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await routes._verify_signed_invalidation(request, secret, raw_body)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Replay detected")

    async def test_missing_headers_is_rejected(self):
        routes = self._load_routes()
        secret = "signing-secret"
        raw_body = b'{"user_id":"u-1"}'

        request = _make_request(
            {
                "x-webhook-id": "evt-5",
            }
        )

        with mock.patch.object(routes.time, "time", return_value=1_700_000_000):
            with mock.patch.object(
                routes,
                "put_replay_guard_once",
                new=mock.AsyncMock(return_value=True),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await routes._verify_signed_invalidation(request, secret, raw_body)

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Missing webhook signature headers")
