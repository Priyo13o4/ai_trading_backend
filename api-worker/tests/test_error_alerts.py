import importlib
import os
import pathlib
import sys
import unittest
from unittest import mock


TEST_FILE = pathlib.Path(__file__).resolve()
WORKER_ROOT = TEST_FILE.parents[1]
REPO_ROOT = TEST_FILE.parents[2]
COMMON_ROOT = REPO_ROOT / "common"

for path in (str(WORKER_ROOT), str(COMMON_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


MODULE_NAME = "app.error_alerts"


def _reload_module():
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    return importlib.reload(module)


class WorkerErrorAlertsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _reload_module()
        self.module._ALERT_DEDUP_CACHE.clear()
        self.module._ALERT_RATE_LIMIT_WINDOW.clear()
        self.module._ALERT_CIRCUIT_OPEN_UNTIL = 0.0
        self.module._ALERT_CIRCUIT_CONSECUTIVE_FAILURES = 0

    def test_sanitize_internal_message_redacts_sensitive_data(self):
        raw = "password=hunter2 bearer abcdef test@example.com"
        sanitized = self.module._sanitize_internal_message(raw)

        self.assertNotIn("hunter2", sanitized)
        self.assertNotIn("test@example.com", sanitized)
        self.assertIn("[REDACTED_EMAIL]", sanitized)

    def test_fingerprint_ignores_volatile_fields(self):
        payload_a = {
            "event_type": "runtime_error",
            "service": "api-worker",
            "environment": "production",
            "path": "/worker/scheduler",
            "method": "PROCESS",
            "status_code": 500,
            "severity": "critical",
            "message_safe": "A",
            "request_id": "req-a",
            "error_id": "err-a",
            "context": {"exception_type": "RuntimeError", "script": "worker.py", "phase": "tick"},
        }
        payload_b = {
            **payload_a,
            "message_safe": "B",
            "request_id": "req-b",
            "error_id": "err-b",
        }

        self.assertEqual(
            self.module._alert_fingerprint(payload_a),
            self.module._alert_fingerprint(payload_b),
        )

    def test_post_error_alert_respects_circuit_open(self):
        env = {
            "N8N_ERROR_ALERT_ENABLED": "1",
            "N8N_ERROR_ALERT_BASE_URL": "https://n8n.example.com/webhook/abc/error-alert",
        }
        payload = {
            "event_type": "runtime_error",
            "service": "api-worker",
            "environment": "local",
            "path": "/worker/x",
            "method": "PROCESS",
            "status_code": 500,
            "severity": "critical",
            "message_safe": "internal error",
            "message_internal": "err",
            "context": {},
        }

        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(self.module, "_is_circuit_open", return_value=True):
                sent = self.module.post_error_alert("/runtime-error", payload)

        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()
