import importlib
import os
import sys
import unittest
from unittest import mock


MODULE_NAME = "app.payments.payment_providers.razorpay_provider"


def _reload_module():
    sys.modules.pop(MODULE_NAME, None)
    os.environ["RAZORPAY_KEY_ID"] = "rzp_test_key"
    os.environ["RAZORPAY_KEY_SECRET"] = "rzp_test_secret"
    os.environ["RAZORPAY_WEBHOOK_SECRET"] = "webhook"
    os.environ["RAZORPAY_PLAN_ID_CORE"] = "plan_x"

    fake_razorpay = mock.Mock()
    fake_razorpay.Client.return_value = mock.Mock()
    fake_razorpay.errors.SignatureVerificationError = Exception

    with mock.patch.dict(sys.modules, {"razorpay": fake_razorpay}):
        module = importlib.import_module(MODULE_NAME)
        return importlib.reload(module)


class RazorpayPauseResumeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_subscription_returns_pause_id(self):
        mod = _reload_module()
        provider = mod.RazorpayProvider()

        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.content = b'{"id":"pause_1","status":"paused"}'
        fake_response.json.return_value = {"id": "pause_1", "status": "paused"}

        with mock.patch.object(mod.requests, "post", return_value=fake_response) as post:
            result = await provider.pause_subscription("sub_123", 1700000000, idempotency_key="idem-1")

        self.assertEqual(result["pause_id"], "pause_1")
        self.assertEqual(result["status"], "paused")
        self.assertTrue(post.called)

    async def test_resume_subscription_passes_pause_id(self):
        mod = _reload_module()
        provider = mod.RazorpayProvider()

        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.content = b'{"status":"active"}'
        fake_response.json.return_value = {"status": "active"}

        with mock.patch.object(mod.requests, "post", return_value=fake_response) as post:
            result = await provider.resume_subscription("sub_123", "pause_1", idempotency_key="idem-2")

        self.assertEqual(result["status"], "active")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"].get("pause_id"), "pause_1")

    async def test_pause_subscription_raises_on_http_error(self):
        mod = _reload_module()
        provider = mod.RazorpayProvider()

        fake_response = mock.Mock()
        fake_response.status_code = 500
        fake_response.text = "internal error"

        with mock.patch.object(mod.requests, "post", return_value=fake_response):
            with self.assertRaises(RuntimeError):
                await provider.pause_subscription("sub_123", 1700000000, idempotency_key="idem-3")


if __name__ == "__main__":
    unittest.main()
