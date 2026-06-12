import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Integer, Boolean, ForeignKey, text, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB, NUMERIC, BIGINT, TEXT, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

class Base(DeclarativeBase):
    pass

class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_name: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    sweep_id: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    sweep_name: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    management_family: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    profile_name: Mapped[str] = mapped_column(TEXT, nullable=False)
    profile_version: Mapped[str] = mapped_column(TEXT, nullable=False)
    engine_version: Mapped[str] = mapped_column(TEXT, nullable=False)
    source_database_name: Mapped[str] = mapped_column(TEXT, nullable=False)
    source_database_fingerprint: Mapped[dict] = mapped_column(JSONB, nullable=False)
    strategy_filter: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ea_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ea_config_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    broker_specs_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    trade_executor_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    fill_model: Mapped[str] = mapped_column(TEXT, nullable=False)
    status: Mapped[str] = mapped_column(TEXT, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    total_strategies: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    processed_strategies: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    executed_trades: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    no_trade_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    unsupported_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    results: Mapped[list["BacktestResult"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    artifacts: Mapped[list["BacktestArtifact"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class BacktestResult(Base):
    __tablename__ = "backtest_results"
    __table_args__ = (
        UniqueConstraint('run_id', 'strategy_id', name='uq_result_run_strategy'),
        Index('ix_backtest_results_strategy', 'strategy_id', 'strategy_hash', 'profile_hash'),
        Index('ix_backtest_results_symbol_outcome', 'symbol', 'outcome'),
        Index('ix_backtest_results_condition_outcome', 'condition_type', 'outcome'),
        Index('ix_backtest_results_entry_time', 'entry_time'),
    )

    result_id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("backtest_runs.run_id", ondelete="CASCADE"), index=True, nullable=False)
    strategy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    profile_hash: Mapped[str] = mapped_column(TEXT, nullable=False)
    symbol: Mapped[str] = mapped_column(TEXT, nullable=False)
    direction: Mapped[str] = mapped_column(TEXT, nullable=False)
    condition_type: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    timeframe: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    confirmation: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    strategy_timestamp: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    strategy_expiry_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(TEXT, nullable=False)
    outcome_reason: Mapped[str] = mapped_column(TEXT, nullable=False)
    entry_time: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    entry_price: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 8), nullable=True)
    exit_time: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 8), nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 8), nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 8), nullable=True)
    initial_stop_loss: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 8), nullable=True)
    final_stop_loss: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 8), nullable=True)
    lot_size: Mapped[Optional[float]] = mapped_column(NUMERIC(12, 4), nullable=True)
    partial_close_executed: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))
    break_even_moved: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))
    mae_pips: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    mfe_pips: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    hit_tp: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))
    hit_sl: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))
    gross_pnl: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    commission: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    swap: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    net_pnl: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    pnl_pips: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    r_multiple: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 6), nullable=True)
    balance_before: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    balance_after: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    equity_high_watermark: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    drawdown_after: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 6), nullable=True)
    bars_scanned: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    debug: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    # Sweep analysis fields
    management_family: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    regime_type: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    session: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    news_state: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    exit_efficiency: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    
    # Theoretical Hold-to-Target outcomes (for decoupling entry vs execution)
    theoretical_fixed_tp_net_pnl: Mapped[Optional[float]] = mapped_column(NUMERIC(18, 4), nullable=True)
    theoretical_fixed_tp_win: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))
    opportunity_cost_flag: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))
    
    # Risk and lot floor auditing flags
    lot_floor_violation: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))
    risk_exceeded_due_to_min_lot: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=text("false"))

    run: Mapped["BacktestRun"] = relationship(back_populates="results")


class BacktestArtifact(Base):
    __tablename__ = "backtest_artifacts"

    artifact_id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("backtest_runs.run_id", ondelete="CASCADE"), nullable=False)
    artifact_type: Mapped[str] = mapped_column(TEXT, nullable=False)
    path: Mapped[str] = mapped_column(TEXT, nullable=False)
    sha256: Mapped[Optional[str]] = mapped_column(TEXT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    run: Mapped["BacktestRun"] = relationship(back_populates="artifacts")
