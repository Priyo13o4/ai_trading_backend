import importlib
import os
import sys
import unittest
from unittest import mock


MODULE_NAME = "app.referrals.reward_transitions"


def _reload_module(env: dict[str, str] | None = None):
    base_env = {
        "SUPABASE_PROJECT_URL": "https://example.supabase.co",
        "SUPABASE_SECRET_KEY": "service-role-key",
    }
    if env:
        base_env.update(env)
    
    sys.modules.pop(MODULE_NAME, None)
    with mock.patch.dict(os.environ, base_env, clear=False):
        module = importlib.import_module(MODULE_NAME)
        return importlib.reload(module)


class RewardTransitionsTests(unittest.IsolatedAsyncioTestCase):
    """Tests for referral reward state transitions (Scope C)."""
    
    async def test_transition_on_hold_to_available_success(self):
        """Test successful transition of expired on_hold rewards to available."""
        mod = _reload_module()
        
        mock_response = mock.Mock(data=[{
            "result_code": "success",
            "transitioned_count": 3,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder
        
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                result = await mod.transition_rewards_on_hold_to_available()
        
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.transitioned_count, 3)
        
        # Verify RPC was called correctly
        rpc_name = mock_supabase.rpc.call_args.args[0]
        self.assertEqual(rpc_name, "transition_rewards_on_hold_to_available")
    
    async def test_transition_on_hold_to_available_no_candidates(self):
        """Test transition when no rewards are eligible (no-op)."""
        mod = _reload_module()
        
        mock_response = mock.Mock(data=[{
            "result_code": "success",
            "transitioned_count": 0,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder
        
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                result = await mod.transition_rewards_on_hold_to_available()
        
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.transitioned_count, 0)
    
    async def test_transition_on_hold_to_available_idempotent(self):
        """Test that calling transition multiple times is safe (idempotent)."""
        mod = _reload_module()
        
        # First call transitions 3 rewards
        mock_response1 = mock.Mock(data=[{
            "result_code": "success",
            "transitioned_count": 3,
        }])
        # Second call returns 0 (no more candidates)
        mock_response2 = mock.Mock(data=[{
            "result_code": "success",
            "transitioned_count": 0,
        }])
        
        mock_supabase = mock.Mock()
        call_count = [0]
        
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                rpc_builder = mock.Mock()
                rpc_builder.execute.return_value = mock_response1
                return rpc_builder
            else:
                rpc_builder = mock.Mock()
                rpc_builder.execute.return_value = mock_response2
                return rpc_builder
        
        mock_supabase.rpc.side_effect = side_effect
        
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                # First call
                result1 = await mod.transition_rewards_on_hold_to_available()
                self.assertEqual(result1.outcome, "success")
                self.assertEqual(result1.transitioned_count, 3)
                
                # Second call should also succeed but transition 0 (idempotent)
                result2 = await mod.transition_rewards_on_hold_to_available()
                self.assertEqual(result2.outcome, "success")
                self.assertEqual(result2.transitioned_count, 0)
    
    async def test_transition_on_hold_to_available_error_handling(self):
        """Test error handling when RPC fails."""
        mod = _reload_module()
        
        mock_supabase = mock.Mock()
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=RuntimeError("db-error"))):
                result = await mod.transition_rewards_on_hold_to_available()
        
        self.assertEqual(result.outcome, "error_controlled")
        self.assertEqual(result.transitioned_count, 0)
        self.assertIsNotNone(result.error_message)
    
    async def test_apply_available_rewards_success(self):
        """Test successful application of available rewards."""
        mod = _reload_module()
        
        mock_response = mock.Mock(data=[{
            "result_code": "success",
            "applied_count": 5,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder
        
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                result = await mod.apply_available_rewards()
        
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.transitioned_count, 5)
        
        # Verify RPC was called correctly
        rpc_name = mock_supabase.rpc.call_args.args[0]
        self.assertEqual(rpc_name, "apply_available_rewards")
    
    async def test_apply_available_rewards_no_candidates(self):
        """Test apply when no rewards are in available state (no-op)."""
        mod = _reload_module()
        
        mock_response = mock.Mock(data=[{
            "result_code": "success",
            "applied_count": 0,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder
        
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                result = await mod.apply_available_rewards()
        
        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.transitioned_count, 0)
    
    async def test_apply_available_rewards_idempotent(self):
        """Test that calling apply multiple times is safe (idempotent)."""
        mod = _reload_module()
        
        # First call applies 5 rewards
        mock_response1 = mock.Mock(data=[{
            "result_code": "success",
            "applied_count": 5,
        }])
        # Second call returns 0 (no more candidates)
        mock_response2 = mock.Mock(data=[{
            "result_code": "success",
            "applied_count": 0,
        }])
        
        mock_supabase = mock.Mock()
        
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                # First call
                mock_rpc_builder = mock.Mock()
                mock_rpc_builder.execute.return_value = mock_response1
                mock_supabase.rpc.return_value = mock_rpc_builder
                
                result1 = await mod.apply_available_rewards()
                self.assertEqual(result1.outcome, "success")
                self.assertEqual(result1.transitioned_count, 5)
                
                # Second call
                mock_rpc_builder = mock.Mock()
                mock_rpc_builder.execute.return_value = mock_response2
                mock_supabase.rpc.return_value = mock_rpc_builder
                
                result2 = await mod.apply_available_rewards()
                self.assertEqual(result2.outcome, "success")
                self.assertEqual(result2.transitioned_count, 0)
    
    async def test_apply_available_rewards_error_handling(self):
        """Test error handling when apply RPC fails."""
        mod = _reload_module()
        
        mock_supabase = mock.Mock()
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=RuntimeError("db-error"))):
                result = await mod.apply_available_rewards()
        
        self.assertEqual(result.outcome, "error_controlled")
        self.assertEqual(result.transitioned_count, 0)
        self.assertIsNotNone(result.error_message)
    
    async def test_debug_logging_structure(self):
        """Test that debug logging functions exist and are properly structured."""
        mod = _reload_module()
        
        # Verify functions exist
        self.assertTrue(callable(mod._is_debug_enabled))
        self.assertTrue(callable(mod._debug_log))
        
        # Test that _debug_log doesn't raise errors
        mod._debug_log("test message")  # Should not raise
        
        # Test that we can enable debug and call the functions
        mock_response = mock.Mock(data=[{
            "result_code": "success",
            "transitioned_count": 2,
        }])
        mock_rpc_builder = mock.Mock()
        mock_rpc_builder.execute.return_value = mock_response
        
        mock_supabase = mock.Mock()
        mock_supabase.rpc.return_value = mock_rpc_builder
        
        with mock.patch.object(mod, "get_supabase_client", return_value=mock_supabase):
            with mock.patch.object(mod, "async_db", new=mock.AsyncMock(side_effect=lambda fn: fn())):
                # Should succeed regardless of AUTHDBG_ENABLED status
                result = await mod.transition_rewards_on_hold_to_available()
                self.assertEqual(result.outcome, "success")


if __name__ == "__main__":
    unittest.main()
