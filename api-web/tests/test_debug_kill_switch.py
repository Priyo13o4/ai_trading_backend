import importlib
import os
import sys
import unittest
from unittest import mock


MODULE_NAME = "app.observability.debug"


def _reload_module():
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    return importlib.reload(module)


class DebugKillSwitchTests(unittest.TestCase):
    def test_debug_disabled_globally(self):
        env = {
            "DEBUG_ENABLED": "0",
            "DEBUG_CHANNELS": "auth,cors",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            mod = _reload_module()
            self.assertFalse(mod.is_debug_enabled("auth"))
            self.assertFalse(mod.is_debug_enabled("cors"))

    def test_debug_enabled_when_global_and_channel_match(self):
        env = {
            "DEBUG_ENABLED": "1",
            "DEBUG_CHANNELS": "auth,cors",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            mod = _reload_module()
            self.assertTrue(mod.is_debug_enabled("auth"))
            self.assertTrue(mod.is_debug_enabled("auth.session"))
            self.assertFalse(mod.is_debug_enabled("payments"))


if __name__ == "__main__":
    unittest.main()
