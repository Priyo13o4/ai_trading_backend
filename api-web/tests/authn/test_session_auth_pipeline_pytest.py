import importlib
import os
import sys


def _reload(module_name: str):
    sys.modules.pop(module_name, None)
    module = importlib.import_module(module_name)
    return importlib.reload(module)


def test_supabase_admin_parser_accepts_nested_and_top_level_user():
    supabase_admin = _reload("app.authn.supabase_admin")

    nested = {"user": {"id": "u-nested", "email": "n@example.com"}}
    top_level = {"id": "u-top", "created_at": "2026-03-16T00:00:00Z"}

    assert supabase_admin._extract_user_from_admin_response(nested)["id"] == "u-nested"
    assert supabase_admin._extract_user_from_admin_response(top_level)["id"] == "u-top"


def test_runtime_env_secure_default_is_production(monkeypatch):
    monkeypatch.delenv("AUTH_ENV", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("FASTAPI_ENV", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("SESSION_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "test-password")

    routes = _reload("app.authn.routes")

    assert routes._runtime_environment_name() == "production"
