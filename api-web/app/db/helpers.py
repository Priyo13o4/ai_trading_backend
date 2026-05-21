from .connection import _use_timescale_caggs


def _ohlcv_relation_for_timeframe(timeframe: str) -> str:
    # LOCKED ARCHITECTURE:
    # - Broker provides: M1, D1, W1, MN1  (always queried from candlesticks)
    # - CAGGs provide: M5, M15, M30, H1, H4 (optional, gated by USE_TIMESCALE_CAGGS)
    from trading_common.timeframes import (
        normalize_timeframe,
        is_derived_cagg_timeframe,
        cagg_relation_for_timeframe,
        assert_timeframe_policy,
        TimeframePolicyError,
    )

    tf = normalize_timeframe(timeframe)

    # Broker-provided TFs always come from the base candlesticks table.
    if tf in {"M1", "D1", "W1", "MN1"}:
        assert_timeframe_policy(tf, "broker_raw")
        return "candlesticks"

    # Derived TFs must come from Timescale CAGGs (no raw fallback).
    if is_derived_cagg_timeframe(tf):
        assert_timeframe_policy(tf, "cagg")
        if not _use_timescale_caggs():
            raise TimeframePolicyError(
                f"Derived timeframe {tf} requires Timescale CAGGs (USE_TIMESCALE_CAGGS=true)"
            )
        return cagg_relation_for_timeframe(tf)

    raise ValueError(f"Unsupported timeframe: {tf}")


def _compute_swing_analysis(candle_list: list[dict], lookback: int = 50, flank: int = 2) -> dict[str, int]:
    """Compute swing highs/lows and their directional transitions."""
    base = {
        "total_swing_highs": 0,
        "total_swing_lows": 0,
        "higher_highs": 0,
        "lower_highs": 0,
        "higher_lows": 0,
        "lower_lows": 0,
    }
    if not candle_list:
        return base

    window = list(reversed(candle_list[:lookback]))
    min_points = (flank * 2) + 1
    if len(window) < min_points:
        return base

    swing_highs: list[float] = []
    swing_lows: list[float] = []

    for i in range(flank, len(window) - flank):
        current_high = float(window[i]["high"])
        current_low = float(window[i]["low"])

        prev_highs = [float(window[j]["high"]) for j in range(i - flank, i)]
        next_highs = [float(window[j]["high"]) for j in range(i + 1, i + flank + 1)]
        if current_high > max(prev_highs) and current_high >= max(next_highs):
            swing_highs.append(current_high)

        prev_lows = [float(window[j]["low"]) for j in range(i - flank, i)]
        next_lows = [float(window[j]["low"]) for j in range(i + 1, i + flank + 1)]
        if current_low < min(prev_lows) and current_low <= min(next_lows):
            swing_lows.append(current_low)

    higher_highs = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] > swing_highs[i - 1])
    lower_highs = sum(1 for i in range(1, len(swing_highs)) if swing_highs[i] < swing_highs[i - 1])
    higher_lows = sum(1 for i in range(1, len(swing_lows)) if swing_lows[i] > swing_lows[i - 1])
    lower_lows = sum(1 for i in range(1, len(swing_lows)) if swing_lows[i] < swing_lows[i - 1])

    return {
        "total_swing_highs": len(swing_highs),
        "total_swing_lows": len(swing_lows),
        "higher_highs": int(higher_highs),
        "lower_highs": int(lower_highs),
        "higher_lows": int(higher_lows),
        "lower_lows": int(lower_lows),
    }
