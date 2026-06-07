import hashlib
import json
import yaml
from pathlib import Path

def generate_dict_hash(d: dict) -> str:
    """Generate SHA256 hash from dict."""
    s = json.dumps(d, sort_keys=True)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def load_ea_config(filepath: str | Path) -> dict:
    """Load EA config from YAML."""
    with open(filepath, 'r') as f:
        return yaml.safe_load(f)

def get_ea_config_hash(filepath: str | Path) -> str:
    config = load_ea_config(filepath)
    return generate_dict_hash(config)
