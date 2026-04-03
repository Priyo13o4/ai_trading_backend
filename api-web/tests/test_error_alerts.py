import importlib
import os
import sys
import unittest
from unittest import mock


MODULE_NAME = "app.notifications.error_alerts"


def _reload_module():
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    return importlib.reload(module)


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class ErrorAlertsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.module = _reload_module()
        self.module._ALERT_DEDUP_CACHE.clear()
        self.module._ALERT_RATE_LIMIT_WINDOW.clear()
        self.module._ALERT_CIRCUIT_OPEN_UNTIL = 0.0
        self.module._ALERT_CIRCUIT_CONSECUTIVE_FAILURES = 0

    def test_sanitize_internal_message_redacts_sensitive_tokens(self):
        raw = "Bearer abc123 token=xyz user=test@example.com key=abcd1234abcd1234abcd1234abcd1234"
        sanitized = self.module._sanitize_internal_message(raw)

        self.assertNotIn("test@example.com", sanitized)
        self.assertNotIn("abc123", sanitized)
        self.assertIn("[REDACTED_EMAIL]", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_fingerprint_uses_stable_fields(self):
        payload_a = {
            "event_type": "runtime_error",
            "service": "api-web",
            "environment": "production",
            "path": "/auth/exchange",
            "method": "POST",
            "status_code": 500,
            "severity": "critical",
            "message_safe": "A",
            "request_id": "req-1",
            "error_id": "err-1",
            "context": {"exception_type": "RuntimeError", "script": "x.py", "phase": "auth"},
        }
        payload_b = {
            **payload_a,
            "message_safe": "B",
            "request_id": "req-2",
            "error_id": "err-2",
        }

        self.assertEqual(
            self.module._alert_fingerprint(payload_a),
            self.module._alert_fingerprint(payload_b),
        )

    async def test_post_error_alert_retries_with_backoff(self):
        attempts = []

        class _FakeAsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json, headers):
                attempts.append((url, json, headers))
                if len(attempts) == 1:
                    return _FakeResponse(500, "upstream error")
                return _FakeResponse(200, "ok")

        env = {
            "N8N_ERROR_ALERT_ENABLED": "1",
            "N8N_ERROR_ALERT_BASE_URL": "https://n8n.example.com/webhook/abc/error-alert",
            "N8N_ERROR_ALERT_MAX_RETRIES": "1",
            "N8N_ERROR_ALERT_BACKOFF_BASE_SECONDS": "0.001",
            "N8N_ERROR_ALERT_BACKOFF_MAX_SECONDS": "0.001",
        }

        payload = {
            "event_type": "runtime_error",
            "service": "api-web",
            "environment": "local",
            "path": "/x",
            "method": "POST",
            "status_code": 500,
            "severity": "critical",
            "message_safe": "internal error",
            "message_internal": "Bearer abc",
            "context": {},
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.module.httpx, "AsyncClient", _FakeAsyncClient):
                with mock.patch.object(self.module.asyncio, "sleep", new=mock.AsyncMock()) as sleep_mock:
                    sent = await self.module.post_error_alert("/runtime-error", payload)

        self.assertTrue(sent)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleep_mock.await_count, 1)

    async def test_post_error_alert_drops_when_rate_limited(self):
        env = {
            "N8N_ERROR_ALERT_ENABLED": "1",
            "N8N_ERROR_ALERT_BASE_URL": "https://n8n.example.com/webhook/abc/error-alert",
        }
        payload = {
            "event_type": "runtime_error",
            "service": "api-web",
            "environment": "local",
            "path": "/x",
            "method": "POST",
            "status_code": 500,
            "severity": "critical",
            "message_safe": "internal error",
            "message_internal": "err",
            "context": {},
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.module, "_allow_dispatch_under_rate_limit", return_value=False):
                sent = await self.module.post_error_alert("/runtime-error", payload)

        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()
