"""Chronological portfolio / equity-curve simulator.

The per-strategy simulator (`simulate_strategy`) replays each strategy in isolation on a
fresh balance, which means it cannot model anything the live EA does at the *account* level:
concurrent positions, opposite-direction conflicts, a real equity curve, or drawdown-based
position sizing. This module adds that account layer so the backtest mirrors the EA's
portfolio behaviour.

Mirrors (see VALIDATION_DESIGN/03 drift matrix D2/D16 and 07 BACKTEST-MIRROR TRACKING):
  * EA-04 — reject a new entry if an OPPOSITE-direction position is already open on the
    same symbol.
  * EA-16 — drawdown protection keys off EQUITY (balance + floating PnL of open positions),
    not balance: when equity has fallen `drawdown_threshold`% from peak equity, new lots are
    multiplied by `drawdown_reduction_factor`.
  * EA-02 caps — `max_concurrent_trades` / `max_total_risk_percent` enforced (kept permissive
    in the config to match the deployed EA, per the user's testing setup).

Design — two phase:
  Phase 1 (caller): run `simulate_strategy` per strategy to get its independent entry/exit
    decision (entry_time, entry_price, sl, tp, exit_time, exit_price, gross/commission) plus a
    captured M1 `mark_path` for floating-equity marking.
  Phase 2 (here): walk the entries in chronological order on a single account. PnL is
    *re-scaled* linearly to the portfolio-chosen lot (PnL is linear in lot; SL/TP/exit prices
    are lot-independent). Known modelling boundary: the partial-close broker-min-volume skip
    in `check_partial_close` is lot-dependent, so if the portfolio lot differs enough to cross
    that floor the scaled PnL can deviate slightly from a full re-simulation. This is the
    documented approximation that keeps the build tractable; drawdown only *reduces* lots and
    partials are a fraction of the lot, so threshold crossings are rare.
"""

from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from backtest_engine.broker_specs import BrokerSymbolSpec
from backtest_engine.simulation.pnl import calculate_pnl
from backtest_engine.simulation.risk import calculate_lot_size, normalize_volume


@dataclass
class PortfolioTradeCtx:
    """Transient (non-persisted) context attached to a BacktestResult by `simulate_strategy`
    when a portfolio run is active. Carries the inputs the portfolio walker needs that are not
    stored as result columns: sizing inputs and the per-position M1 mark-path used to value
    floating equity at any decision timestamp."""
    confidence: str = "Medium"
    risk_reward_ratio: float = 0.0
    mark_times: list = field(default_factory=list)
    mark_prices: list = field(default_factory=list)


@dataclass
class PortfolioConfig:
    starting_balance: float = 500.0
    max_concurrent_trades: int = 20
    max_total_risk_percent: float = 95.0
    risk_percent: float = 2.0
    use_drawdown_protection: bool = True
    drawdown_threshold: float = 10.0
    drawdown_reduction_factor: float = 0.5
    block_opposite_open: bool = True  # EA-04
    base_lot_size: float = 0.02
    use_dynamic_sizing: bool = True
    high_confidence_multiplier: float = 1.5
    medium_confidence_multiplier: float = 1.0
    low_confidence_multiplier: float = 0.7
    min_lot_size: float = 0.01
    max_lot_size: float = 0.1

    @classmethod
    def from_ea_config(cls, ea_config: dict, starting_balance: float | None = None) -> "PortfolioConfig":
        return cls(
            starting_balance=float(starting_balance if starting_balance is not None
                                   else ea_config.get("portfolio_starting_balance", 500.0)),
            max_concurrent_trades=int(ea_config.get("max_concurrent_trades", 20) or 20),
            max_total_risk_percent=float(ea_config.get("max_total_risk_percent", 95.0) or 95.0),
            risk_percent=float(ea_config.get("max_risk_percent_per_trade",
                                             ea_config.get("risk_percent", 2.0)) or 2.0),
            use_drawdown_protection=bool(ea_config.get("use_drawdown_protection", True)),
            drawdown_threshold=float(ea_config.get("drawdown_threshold", 10.0) or 10.0),
            drawdown_reduction_factor=float(ea_config.get("drawdown_reduction_factor", 0.5) or 0.5),
            block_opposite_open=bool(ea_config.get("block_opposite_open_positions", True)),
            base_lot_size=float(ea_config.get("base_lot_size", 0.02) or 0.02),
            use_dynamic_sizing=bool(ea_config.get("use_dynamic_sizing", True)),
            high_confidence_multiplier=float(ea_config.get("high_confidence_multiplier", 1.5) or 1.5),
            medium_confidence_multiplier=float(ea_config.get("medium_confidence_multiplier", 1.0) or 1.0),
            low_confidence_multiplier=float(ea_config.get("low_confidence_multiplier", 0.7) or 0.7),
            min_lot_size=float(ea_config.get("min_lot_size", 0.01) or 0.01),
            max_lot_size=float(ea_config.get("max_lot_size", 0.1) or 0.1),
        )


@dataclass
class _OpenPosition:
    strategy_id: int
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    lot: float
    risk_amount: float
    net_pnl: float  # portfolio-scaled realized net PnL, applied at exit_time
    tick_size: float
    tick_value: float
    times: list = field(default_factory=list)   # mark-path timestamps (sorted)
    prices: list = field(default_factory=list)   # mark-path prices aligned with `times`

    def floating_pnl(self, t: datetime) -> float:
        if not self.times:
            price = self.entry_price
        else:
            idx = bisect.bisect_right(self.times, t) - 1
            price = self.prices[idx] if idx >= 0 else self.entry_price
        return calculate_pnl(self.entry_price, price, self.lot, self.direction,
                             self.tick_size, self.tick_value)


@dataclass
class PortfolioTrade:
    strategy_id: int
    symbol: str
    direction: str
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    admitted: bool
    reject_reason: Optional[str]
    independent_lot: float
    portfolio_lot: float
    net_pnl: float
    equity_at_entry: float
    peak_equity_at_entry: float
    drawdown_pct_at_entry: float
    drawdown_sizing_applied: bool
    balance_after: float
    concurrent_at_entry: int


@dataclass
class PortfolioOutcome:
    config: PortfolioConfig
    starting_balance: float
    final_balance: float
    final_equity: float
    total_net_pnl: float
    max_drawdown_pct: float
    max_drawdown_abs: float
    peak_equity: float
    profit_factor: float
    win_rate: float
    num_candidates: int
    num_admitted: int
    num_rejected: int
    reject_reasons: dict
    peak_concurrent: int
    trades: list = field(default_factory=list)            # list[PortfolioTrade]
    equity_curve: list = field(default_factory=list)       # list[(ts, equity, balance)]

    def to_summary_dict(self) -> dict:
        return {
            "starting_balance": round(self.starting_balance, 2),
            "final_balance": round(self.final_balance, 2),
            "final_equity": round(self.final_equity, 2),
            "total_net_pnl": round(self.total_net_pnl, 2),
            "return_pct": round((self.final_balance / self.starting_balance - 1.0) * 100.0, 2)
            if self.starting_balance else 0.0,
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "max_drawdown_abs": round(self.max_drawdown_abs, 2),
            "peak_equity": round(self.peak_equity, 2),
            "profit_factor": round(self.profit_factor, 3),
            "win_rate": round(self.win_rate, 4),
            "num_candidates": self.num_candidates,
            "num_admitted": self.num_admitted,
            "num_rejected": self.num_rejected,
            "reject_reasons": self.reject_reasons,
            "peak_concurrent": self.peak_concurrent,
        }


def _coerce_dt(value: Any) -> Optional[datetime]:
    return value if isinstance(value, datetime) else None


def _entered(result: Any) -> bool:
    return getattr(result, "entry_time", None) is not None and float(getattr(result, "lot_size", 0) or 0) > 0


def simulate_portfolio(
    results: list,
    ea_config: dict,
    broker_specs: dict,
    starting_balance: float | None = None,
) -> PortfolioOutcome:
    """Walk entered strategies chronologically on one account and apply portfolio gates."""
    cfg = PortfolioConfig.from_ea_config(ea_config, starting_balance)

    candidates = [r for r in results if _entered(r)]
    candidates.sort(key=lambda r: r.entry_time)

    balance = cfg.starting_balance
    peak_equity = cfg.starting_balance
    open_positions: dict[int, _OpenPosition] = {}
    exit_heap: list[tuple] = []  # (exit_time, seq, strategy_id)
    seq = 0

    equity_curve: list[tuple] = [(None, cfg.starting_balance, cfg.starting_balance)]
    trades: list[PortfolioTrade] = []
    reject_reasons: dict[str, int] = {}
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    peak_concurrent = 0

    def realize_exits_until(t: datetime) -> None:
        nonlocal balance, peak_equity, max_dd_pct, max_dd_abs
        while exit_heap and exit_heap[0][0] <= t:
            _, _, sid = heapq.heappop(exit_heap)
            pos = open_positions.pop(sid, None)
            if pos is None:
                continue
            balance += pos.net_pnl
            eq = _equity(balance, open_positions, pos.exit_time)
            peak_equity = max(peak_equity, eq)
            _record_dd(eq)
            equity_curve.append((pos.exit_time, eq, balance))

    def _equity(bal: float, positions: dict, t: datetime) -> float:
        return bal + sum(p.floating_pnl(t) for p in positions.values())

    def _record_dd(eq: float) -> None:
        nonlocal max_dd_pct, max_dd_abs
        if peak_equity > 0:
            dd_abs = peak_equity - eq
            dd_pct = dd_abs / peak_equity * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
            if dd_abs > max_dd_abs:
                max_dd_abs = dd_abs

    for r in candidates:
        entry_t = r.entry_time
        # 1) realize any exits that happened before this entry
        realize_exits_until(entry_t)

        spec: BrokerSymbolSpec = broker_specs.get(r.symbol)
        equity_now = _equity(balance, open_positions, entry_t)
        peak_equity = max(peak_equity, equity_now)
        _record_dd(equity_now)
        dd_pct = (peak_equity - equity_now) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        concurrent = len(open_positions)
        peak_concurrent = max(peak_concurrent, concurrent)

        reject = None
        if spec is None:
            reject = "missing_broker_spec"
        elif cfg.block_opposite_open and any(
            p.symbol == r.symbol and p.direction != r.direction for p in open_positions.values()
        ):
            reject = "directional_conflict"  # EA-04
        elif concurrent >= cfg.max_concurrent_trades:
            reject = "max_concurrent"

        portfolio_lot = 0.0
        net_pnl = 0.0
        dd_sizing = False
        if reject is None:
            ctx = getattr(r, "pf_ctx", None)
            confidence = getattr(ctx, "confidence", None) or "Medium"
            rr = float(getattr(ctx, "risk_reward_ratio", 0.0) or 0.0)
            entry_price = float(r.entry_price or 0.0)
            stop_loss = float(r.initial_stop_loss or 0.0)

            # EA-16: size off EQUITY, then reduce in drawdown.
            portfolio_lot = calculate_lot_size(
                equity_now, cfg.risk_percent, entry_price, stop_loss, spec,
                confidence=confidence, risk_reward_ratio=rr,
                base_lot_size=cfg.base_lot_size, use_dynamic_sizing=cfg.use_dynamic_sizing,
                high_confidence_multiplier=cfg.high_confidence_multiplier,
                medium_confidence_multiplier=cfg.medium_confidence_multiplier,
                low_confidence_multiplier=cfg.low_confidence_multiplier,
                min_lot_size=cfg.min_lot_size, max_lot_size=cfg.max_lot_size,
            )
            if cfg.use_drawdown_protection and dd_pct >= cfg.drawdown_threshold and portfolio_lot > 0:
                dd_sizing = True
                portfolio_lot = normalize_volume(
                    portfolio_lot * cfg.drawdown_reduction_factor, spec,
                    cfg.min_lot_size, cfg.max_lot_size,
                )

            if portfolio_lot <= 0:
                reject = "lot_size_zero"

        if reject is None:
            new_risk = abs(calculate_pnl(entry_price, stop_loss, portfolio_lot, r.direction,
                                         spec.tick_size, spec.tick_value))
            open_risk = sum(p.risk_amount for p in open_positions.values())
            total_risk_pct = ((open_risk + new_risk) / equity_now * 100.0) if equity_now > 0 else math.inf
            if total_risk_pct > cfg.max_total_risk_percent:
                reject = "max_total_risk"

        if reject is not None:
            reject_reasons[reject] = reject_reasons.get(reject, 0) + 1
            trades.append(PortfolioTrade(
                strategy_id=r.strategy_id, symbol=r.symbol, direction=r.direction,
                entry_time=entry_t, exit_time=_coerce_dt(getattr(r, "exit_time", None)),
                admitted=False, reject_reason=reject,
                independent_lot=float(r.lot_size or 0.0), portfolio_lot=0.0, net_pnl=0.0,
                equity_at_entry=equity_now, peak_equity_at_entry=peak_equity,
                drawdown_pct_at_entry=dd_pct, drawdown_sizing_applied=False,
                balance_after=balance, concurrent_at_entry=concurrent,
            ))
            continue

        # Admit: scale independent PnL to the portfolio lot (linear in lot).
        independent_lot = float(r.lot_size or 0.0)
        scale = (portfolio_lot / independent_lot) if independent_lot > 0 else 0.0
        net_pnl = float(r.net_pnl or 0.0) * scale

        ctx = getattr(r, "pf_ctx", None)
        mark_times = list(getattr(ctx, "mark_times", []) or [])
        mark_prices = list(getattr(ctx, "mark_prices", []) or [])
        exit_t = _coerce_dt(getattr(r, "exit_time", None)) or entry_t

        pos = _OpenPosition(
            strategy_id=r.strategy_id, symbol=r.symbol, direction=r.direction,
            entry_time=entry_t, exit_time=exit_t, entry_price=entry_price, lot=portfolio_lot,
            risk_amount=new_risk, net_pnl=net_pnl,
            tick_size=spec.tick_size, tick_value=spec.tick_value,
            times=mark_times, prices=mark_prices,
        )
        open_positions[r.strategy_id] = pos
        heapq.heappush(exit_heap, (exit_t, seq, r.strategy_id))
        seq += 1
        peak_concurrent = max(peak_concurrent, len(open_positions))

        # stamp portfolio equity/drawdown back onto the result row (columns already exist)
        try:
            r.equity_high_watermark = round(peak_equity, 4)
            r.drawdown_after = round(dd_pct, 6)
        except Exception:
            pass

        equity_curve.append((entry_t, _equity(balance, open_positions, entry_t), balance))
        trades.append(PortfolioTrade(
            strategy_id=r.strategy_id, symbol=r.symbol, direction=r.direction,
            entry_time=entry_t, exit_time=exit_t, admitted=True, reject_reason=None,
            independent_lot=independent_lot, portfolio_lot=portfolio_lot, net_pnl=net_pnl,
            equity_at_entry=equity_now, peak_equity_at_entry=peak_equity,
            drawdown_pct_at_entry=dd_pct, drawdown_sizing_applied=dd_sizing,
            balance_after=balance, concurrent_at_entry=concurrent,
        ))

    # drain remaining exits
    if exit_heap:
        last_t = max(e[0] for e in exit_heap)
        realize_exits_until(last_t)

    admitted = [t for t in trades if t.admitted]
    gains = sum(t.net_pnl for t in admitted if t.net_pnl > 0)
    losses = sum(-t.net_pnl for t in admitted if t.net_pnl < 0)
    profit_factor = (gains / losses) if losses > 0 else (math.inf if gains > 0 else 0.0)
    wins = sum(1 for t in admitted if t.net_pnl > 0)
    win_rate = (wins / len(admitted)) if admitted else 0.0
    final_equity = _equity_static(balance, open_positions)

    return PortfolioOutcome(
        config=cfg, starting_balance=cfg.starting_balance, final_balance=balance,
        final_equity=final_equity, total_net_pnl=balance - cfg.starting_balance,
        max_drawdown_pct=max_dd_pct, max_drawdown_abs=max_dd_abs, peak_equity=peak_equity,
        profit_factor=profit_factor, win_rate=win_rate,
        num_candidates=len(candidates), num_admitted=len(admitted),
        num_rejected=len(trades) - len(admitted), reject_reasons=reject_reasons,
        peak_concurrent=peak_concurrent, trades=trades, equity_curve=equity_curve,
    )


def _equity_static(balance: float, positions: dict) -> float:
    # positions should be empty after draining; guard anyway.
    return balance + sum(p.net_pnl for p in positions.values())
