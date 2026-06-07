from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SweepVariant:
    variant_id: str
    description: str
    overrides: dict[str, Any]


def merge_config(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_sweep_variants(path: str | Path) -> list[SweepVariant]:
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}

    variants = []
    for item in data.get("variants", []):
        variants.append(
            SweepVariant(
                variant_id=item["id"],
                description=item.get("description", ""),
                overrides=item.get("overrides", {}),
            )
        )
    return variants


def validate_management_config(config: dict[str, Any]) -> None:
    if config.get("use_ma_trailing_stop") and config.get("use_trailing_stop"):
        raise ValueError(
            "Invalid config: use_ma_trailing_stop and use_trailing_stop are mutually exclusive in sweeps."
        )
