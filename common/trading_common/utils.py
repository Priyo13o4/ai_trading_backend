"""Utility functions."""

import json


def json_dumps(obj):
    """JSON serialization with datetime support."""
    return json.dumps(obj, default=str)
