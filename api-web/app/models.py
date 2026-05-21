from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, String, Text, Float
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ToDictMixin:
    def to_dict(self) -> dict[str, Any]:
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class Strategy(Base, ToDictMixin):
    __tablename__ = "strategies"

    strategy_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(Integer)
    strategy_name: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(10))
    direction: Mapped[str] = mapped_column(String(10))
    entry_signal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    take_profit: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    risk_reward_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    confidence: Mapped[str] = mapped_column(String(10))
    expiry_minutes: Mapped[int | None] = mapped_column(Integer)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expiry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detailed_analysis: Mapped[str | None] = mapped_column(Text)
    market_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String(20))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    rating_count: Mapped[int | None] = mapped_column(Integer)
    avg_rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    user_feedback: Mapped[str | None] = mapped_column(Text)
    trade_mode: Mapped[str | None] = mapped_column(String(20))
    execution_allowed: Mapped[bool | None] = mapped_column(Boolean)
    risk_level: Mapped[str | None] = mapped_column(String(20))
    trade_recommended: Mapped[bool] = mapped_column(Boolean)
    summary: Mapped[str | None] = mapped_column(Text)
    news_context: Mapped[str | None] = mapped_column(Text)


class Signal(Base, ToDictMixin):
    __tablename__ = "signals"

    signal_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int | None] = mapped_column(Integer)
    mt5_ticket: Mapped[int] = mapped_column(BigInteger)
    mt5_magic_number: Mapped[int | None] = mapped_column(Integer)
    trading_pair: Mapped[str] = mapped_column(String(10))
    direction: Mapped[str] = mapped_column(String(10))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    lot_size: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(20))
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    pnl_pips: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    hit_tp: Mapped[bool | None] = mapped_column(Boolean)
    hit_sl: Mapped[bool | None] = mapped_column(Boolean)
    partial_close_executed: Mapped[bool | None] = mapped_column(Boolean)
    break_even_moved: Mapped[bool | None] = mapped_column(Boolean)
    commission: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    swap: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    execution_notes: Mapped[str | None] = mapped_column(Text)
    market_conditions_at_entry: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Candlestick(Base, ToDictMixin):
    __tablename__ = "candlesticks"

    # Composite PK matching the hypertable unique index: (symbol, timeframe, time DESC)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    timeframe: Mapped[str] = mapped_column(Text, primary_key=True)
    open: Mapped[float] = mapped_column(Float(asdecimal=False))
    high: Mapped[float] = mapped_column(Float(asdecimal=False))
    low: Mapped[float] = mapped_column(Float(asdecimal=False))
    close: Mapped[float] = mapped_column(Float(asdecimal=False))
    volume: Mapped[int] = mapped_column(BigInteger)


class TechnicalIndicator(Base, ToDictMixin):
    __tablename__ = "technical_indicators"

    # Composite PK matching the hypertable unique index: (symbol, timeframe, time DESC)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(10), primary_key=True)
    ema_9: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ema_21: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ema_50: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ema_100: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ema_200: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ema_momentum_slope: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    rsi: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    macd_main: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    macd_signal: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    macd_histogram: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    roc_percent: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    atr: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    atr_percentile: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bb_upper: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bb_middle: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bb_lower: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bb_squeeze_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bb_width_percentile: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    adx: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    dmp: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    dmn: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    obv_slope: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    indicators_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



class EmailNewsAnalysis(Base, ToDictMixin):
    __tablename__ = "email_news_analysis"

    email_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    headline: Mapped[str] = mapped_column(Text)
    original_email_content: Mapped[str] = mapped_column(Text)
    ai_analysis_summary: Mapped[str | None] = mapped_column(Text)
    forex_relevant: Mapped[bool | None] = mapped_column(Boolean)
    forex_instruments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    primary_instrument: Mapped[str | None] = mapped_column(String(50))
    us_political_related: Mapped[bool | None] = mapped_column(Boolean)
    breaking_news: Mapped[bool | None] = mapped_column(Boolean)
    trade_deal_related: Mapped[bool | None] = mapped_column(Boolean)
    central_bank_related: Mapped[bool | None] = mapped_column(Boolean)
    importance_score: Mapped[int | None] = mapped_column(Integer)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    analysis_confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    news_category: Mapped[str | None] = mapped_column(String(50))
    entities_mentioned: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    trading_sessions: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    market_impact_prediction: Mapped[str | None] = mapped_column(String(20))
    impact_timeframe: Mapped[str | None] = mapped_column(String(20))
    volatility_expectation: Mapped[str | None] = mapped_column(String(20))
    forexfactory_urls: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    forexfactory_content_id: Mapped[str | None] = mapped_column(Text)
    email_uid: Mapped[int | None] = mapped_column(Integer)
    from_address: Mapped[str | None] = mapped_column(Text)
    vector_store_id: Mapped[int | None] = mapped_column(Integer)
    email_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    similar_news_context: Mapped[str | None] = mapped_column(Text)
    similar_news_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    forexfactory_category: Mapped[str | None] = mapped_column(String(100))
    human_takeaway: Mapped[str | None] = mapped_column(Text)
    attention_score: Mapped[int | None] = mapped_column(Integer)
    news_state: Mapped[str | None] = mapped_column(String(20))
    market_pressure: Mapped[str | None] = mapped_column(String(20))
    attention_window: Mapped[str | None] = mapped_column(String(20))
    confidence_label: Mapped[str | None] = mapped_column(String(20))
    expected_followups: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    is_priced_in: Mapped[bool | None] = mapped_column(Boolean)


class RegimeData(Base, ToDictMixin):
    __tablename__ = "regime_data"

    regime_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(Integer)
    trading_pair: Mapped[str] = mapped_column(String(10))
    regime_type: Mapped[str] = mapped_column(String(50))
    regime_summary: Mapped[str] = mapped_column(Text)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    market_data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    collection_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    analysis_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WeeklyMacroPlaybook(Base, ToDictMixin):
    __tablename__ = "weekly_macro_playbook"

    playbook_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_week_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    date_range: Mapped[str | None] = mapped_column(String(100))
    dominant_themes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    currency_bias: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    high_risk_windows: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    overall_strategy: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pair_bias: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    playbook_hash: Mapped[str | None] = mapped_column(Text)


class EconomicEventAnalysis(Base, ToDictMixin):
    __tablename__ = "economic_event_analysis"

    analysis_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_name: Mapped[str] = mapped_column(String(255))
    event_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    country: Mapped[str] = mapped_column(String(10))
    impact: Mapped[str | None] = mapped_column(String(20))
    key_numbers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    market_pricing_sentiment: Mapped[str | None] = mapped_column(Text)
    primary_affected_pairs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trading_scenarios: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    market_dynamics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
