import importlib
import os
import sys
import unittest
from unittest import mock


def _reload_routes_module():
    sys.modules.pop("app.authn.routes", None)
    module = importlib.import_module("app.authn.routes")
    return importlib.reload(module)


class RoutesEnvStartupTests(unittest.TestCase):
    def test_invalid_cookie_and_auth_env_values_fall_back_to_defaults(self):
        env = {
            "SESSION_REDIS_URL": "redis://localhost:6379/0",
            "REDIS_PASSWORD": "test-password",
            "APP_ENV": "development",
            "AUTH_ENV": "development",
            "COOKIE_SECURE": "not-a-bool",
            "COOKIE_SAMESITE": "sideways",
            "TRUST_PROXY_HEADERS": "2",
            "AUTH_INVALIDATION_USE_SIGNED": "true",
            "AUTH_INVALIDATION_TOLERANCE_SECONDS": "invalid",
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertLogs("app.authn.routes", level="WARNING") as logs:
                routes = _reload_routes_module()

        self.assertTrue(routes.COOKIE_SECURE)
        self.assertEqual(routes.COOKIE_SAMESITE, "lax")
        self.assertFalse(routes.TRUST_PROXY_HEADERS)
        self.assertEqual(routes.AUTH_INVALIDATION_TOLERANCE_SECONDS, 300)

        joined = "\n".join(logs.output)
        self.assertIn("COOKIE_SECURE", joined)
        self.assertIn("COOKIE_SAMESITE", joined)
        self.assertIn("TRUST_PROXY_HEADERS", joined)
        self.assertIn("AUTH_INVALIDATION_TOLERANCE_SECONDS", joined)

    def test_unsigned_invalidation_is_blocked_in_non_dev_environment(self):
        env = {
            "SESSION_REDIS_URL": "redis://localhost:6379/0",
            "REDIS_PASSWORD": "test-password",
            "APP_ENV": "production",
            "AUTH_ENV": "production",
            "AUTH_INVALIDATION_USE_SIGNED": "0",
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                _reload_routes_module()

    def test_unsigned_invalidation_is_allowed_in_development_environment(self):
        env = {
            "SESSION_REDIS_URL": "redis://localhost:6379/0",
            "REDIS_PASSWORD": "test-password",
            "APP_ENV": "development",
            "AUTH_ENV": "development",
            "AUTH_INVALIDATION_USE_SIGNED": "0",
        }

        with mock.patch.dict(os.environ, env, clear=False):
            routes = _reload_routes_module()

        self.assertFalse(routes.AUTH_INVALIDATION_USE_SIGNED)

    def test_app_env_is_canonical_over_auth_env(self):
        env = {
            "SESSION_REDIS_URL": "redis://localhost:6379/0",
            "REDIS_PASSWORD": "test-password",
            "APP_ENV": "production",
            "AUTH_ENV": "development",
            "AUTH_INVALIDATION_USE_SIGNED": "0",
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                _reload_routes_module()
