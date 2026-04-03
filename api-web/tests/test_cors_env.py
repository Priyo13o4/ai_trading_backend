import importlib
import os
import sys
import unittest
from unittest import mock


def _reload_cors_env_module():
    sys.modules.pop("app.cors_env", None)
    module = importlib.import_module("app.cors_env")
    return importlib.reload(module)


class CorsEnvTests(unittest.TestCase):
    def test_local_environment_uses_local_defaults(self):
        env = {
            "APP_ENV": "local",
            "ALLOWED_ORIGINS": "",
            "ALLOWED_ORIGIN_REGEX": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            module = _reload_cors_env_module()
            self.assertEqual(module.parse_cors_origins_from_env(), module.LOCAL_ALLOWED_ORIGINS)
            self.assertEqual(module.cors_origin_regex_from_env(), module.LOCAL_ALLOWED_ORIGIN_REGEX)

    def test_non_local_environment_requires_allowed_origins(self):
        env = {
            "APP_ENV": "production",
            "ALLOWED_ORIGINS": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            module = _reload_cors_env_module()
            with self.assertRaises(RuntimeError):
                module.parse_cors_origins_from_env()

    def test_non_local_environment_requires_allowed_origin_regex(self):
        env = {
            "APP_ENV": "production",
            "ALLOWED_ORIGIN_REGEX": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            module = _reload_cors_env_module()
            with self.assertRaises(RuntimeError):
                module.cors_origin_regex_from_env()

    def test_non_local_environment_accepts_explicit_values(self):
        regex = r"^https://([a-zA-Z0-9_-]+\.)*pipfactor\.com$"
        env = {
            "APP_ENV": "production",
            "ALLOWED_ORIGINS": "https://pipfactor.com,https://www.pipfactor.com",
            "ALLOWED_ORIGIN_REGEX": regex,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            module = _reload_cors_env_module()
            self.assertEqual(
                module.parse_cors_origins_from_env(),
                ["https://pipfactor.com", "https://www.pipfactor.com"],
            )
            self.assertEqual(
                module.cors_origin_regex_from_env(),
                regex,
            )


if __name__ == "__main__":
    unittest.main()
