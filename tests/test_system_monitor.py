import pytest
from unittest.mock import patch

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCKER_MON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "docker-monitor"))
sys.path.insert(0, DOCKER_MON_DIR)

from system_monitor import calculate_cpu, send_alert

def test_calculate_cpu():
    mock_stats = {
        'cpu_stats': {
            'cpu_usage': {'total_usage': 1000000, 'percpu_usage': [1]},
            'system_cpu_usage': 20000000,
            'online_cpus': 1
        },
        'precpu_stats': {
            'cpu_usage': {'total_usage': 500000},
            'system_cpu_usage': 10000000
        }
    }
    # cpu_delta = 500k, sys_delta = 10m
    # percent = (500k / 10m) * 1 * 100 = 5%
    result = calculate_cpu(mock_stats)
    assert result == 5.0

@patch('system_monitor.requests.post')
@patch('system_monitor.N8N_WEBHOOK_URL', 'http://fake-webhook')
def test_send_alert(mock_post):
    send_alert("test_container", 95.5, 90.0, 5)
    
    mock_post.assert_called_once()
    payload = mock_post.call_args[1]['json']
    assert payload['container'] == "test_container"
    assert payload['metrics']['cpu_percent'] == 95.5
    assert payload['metrics']['memory_percent'] == 90.0
