import hashlib
import logging
import os
import random
from typing import Iterable


def _parse_debug_channels(raw: str) -> set[str]:
    channels: set[str] = set()
    for part in (raw or "").split(","):
        token = part.strip().lower()
        if token:
            channels.add(token)
    return channels


def _parse_sampling_rate(raw: str) -> float:
    try:
        value = float((raw or "1.0").strip())
    except Exception:
        return 1.0

    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def is_global_debug_enabled() -> bool:
    return _parse_bool(os.getenv("DEBUG_ENABLED"), True)


def _enabled_channels() -> set[str]:
    return _parse_debug_channels(os.getenv("DEBUG_CHANNELS") or "")


def _sampling_rate() -> float:
    return _parse_sampling_rate(os.getenv("DEBUG_SAMPLING_RATE") or "1.0")


def is_debug_enabled(channel: str, channels: Iterable[str] | None = None) -> bool:
    if not is_global_debug_enabled():
        return False

    normalized = (channel or "").strip().lower()
    if not normalized:
        return False

    active = set(channels) if channels is not None else _enabled_channels()
    if not active:
        return False

    if "*" in active or "all" in active:
        return True

    if normalized in active:
        return True

    # Support hierarchical channels: enabling "payments" also enables "payments.plisio".
    parts = normalized.split(".")
    for i in range(1, len(parts)):
        if ".".join(parts[:i]) in active:
            return True

    return False


def _is_sampled(sample_key: str | None, sampling_rate: float) -> bool:
    if sampling_rate >= 1.0:
        return True
    if sampling_rate <= 0.0:
        return False

    if sample_key:
        digest = hashlib.sha256(sample_key.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / 0xFFFFFFFF
        return bucket < sampling_rate

    return random.random() < sampling_rate


def debug_log(
    logger: logging.Logger,
    channel: str,
    message: str,
    *args: object,
    sample_key: str | None = None,
) -> None:
    normalized = (channel or "").strip().lower()
    if not is_debug_enabled(normalized):
        return

    sampling_rate = _sampling_rate()
    if not _is_sampled(sample_key, sampling_rate):
        return

    logger.info("DEBUG[%s] " + message, normalized, *args)
