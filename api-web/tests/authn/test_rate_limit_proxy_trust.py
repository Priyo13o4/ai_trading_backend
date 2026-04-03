import importlib
import os
import sys
import unittest
from unittest import mock

from starlette.requests import Request


def _reload_rate_limit_module():
    sys.modules.pop("app.authn.rate_limit_auth", None)
    module = importlib.import_module("app.authn.rate_limit_auth")
    return importlib.reload(module)


def _make_request(*, headers: dict[str, str] | None = None, client_host: str = "10.0.0.9") -> Request:
    merged_headers = headers or {}
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/auth/exchange",
        "headers": [
            (k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in merged_headers.items()
        ],
        "client": (client_host, 50000),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class RateLimitProxyTrustTests(unittest.TestCase):
    def test_client_ip_ignores_forwarded_header_when_proxy_trust_disabled(self):
        env = {
            "TRUST_PROXY_HEADERS": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            module = _reload_rate_limit_module()

        request = _make_request(
            headers={"x-forwarded-for": "198.51.100.7, 203.0.113.2"},
            client_host="10.1.2.3",
        )

        self.assertFalse(module.TRUST_PROXY_HEADERS)
        self.assertEqual(module._client_ip(request), "10.1.2.3")

    def test_client_ip_uses_forwarded_header_when_proxy_trust_enabled(self):
        env = {
            "TRUST_PROXY_HEADERS": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            module = _reload_rate_limit_module()

        request = _make_request(
            headers={"x-forwarded-for": "198.51.100.7, 203.0.113.2"},
            client_host="10.1.2.3",
        )

        self.assertTrue(module.TRUST_PROXY_HEADERS)
        self.assertEqual(module._client_ip(request), "198.51.100.7")

    def test_client_ip_prefers_cf_connecting_ip_when_proxy_trust_enabled(self):
        env = {
            "TRUST_PROXY_HEADERS": "true",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            module = _reload_rate_limit_module()

        request = _make_request(
            headers={
                "cf-connecting-ip": "203.0.113.55",
                "x-forwarded-for": "198.51.100.7, 203.0.113.2",
            },
            client_host="10.1.2.3",
        )

        self.assertTrue(module.TRUST_PROXY_HEADERS)
        self.assertEqual(module._client_ip(request), "203.0.113.55")