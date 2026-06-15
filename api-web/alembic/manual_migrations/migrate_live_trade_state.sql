-- Migration: create live_trade_state table
-- Purpose: Backend source of truth for open trade state so the EA can
--          rehydrate accurately after a restart.
-- Applied: 2026-06-15
-- Safe to re-run: uses IF NOT EXISTS

CREATE TABLE IF NOT EXISTS live_trade_state (
    -- Primary key: MT5 position ticket (always available in TRADE_EVENT)
    ticket               BIGINT         PRIMARY KEY,

    -- Secondary linkage key: signal_hash from the order comment AI_{sid}_{hash}
    -- Populated when available; EA does not send this in TRADE_EVENT JSON today.
    signal_hash          TEXT           DEFAULT NULL,

    strategy_id          INTEGER        REFERENCES strategies(strategy_id) ON DELETE SET NULL,

    -- Issued fields — set once on the first "open" event, never overwritten.
    direction            VARCHAR(10)    NOT NULL CHECK (direction IN ('long', 'short')),
    timeframe            VARCHAR(10)    DEFAULT NULL,  -- from strategy.entry_signal->>'timeframe'
    entry_price          NUMERIC(10,5)  NOT NULL,
    original_sl          NUMERIC(10,5)  DEFAULT NULL,
    original_tp          NUMERIC(10,5)  DEFAULT NULL,
    pre_entry_rule       JSONB          DEFAULT NULL,
    post_entry_rule      JSONB          DEFAULT NULL,

    -- Running state — updated on every subsequent event.
    partial_closed       BOOLEAN        NOT NULL DEFAULT FALSE,
    break_even_moved     BOOLEAN        NOT NULL DEFAULT FALSE,
    max_favorable_price  NUMERIC(10,5)  DEFAULT NULL,
    max_adverse_price    NUMERIC(10,5)  DEFAULT NULL,
    latest_mae_pips      NUMERIC(10,2)  DEFAULT NULL,
    latest_mfe_pips      NUMERIC(10,2)  DEFAULT NULL,
    state                VARCHAR(20)    NOT NULL DEFAULT 'open'
                             CHECK (state IN ('open','partial_close','closed_tp','closed_sl',
                                              'closed_manual','closed_expired','closed_breakeven')),

    created_at           TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- Index for signal_hash lookups (future EA sync request by hash)
CREATE UNIQUE INDEX IF NOT EXISTS idx_live_trade_state_signal_hash
    ON live_trade_state (signal_hash)
    WHERE signal_hash IS NOT NULL;

-- Index for strategy lookups
CREATE INDEX IF NOT EXISTS idx_live_trade_state_strategy_id
    ON live_trade_state (strategy_id);

-- Index for open-state queries (sync endpoint filters on this)
CREATE INDEX IF NOT EXISTS idx_live_trade_state_state
    ON live_trade_state (state)
    WHERE state = 'open' OR state = 'partial_close';

COMMENT ON TABLE live_trade_state IS
    'Per-ticket runtime record for every open MT5 trade. '
    'Issued fields are written once on the first TRADE_EVENT(open) and never overwritten. '
    'Running-state fields are updated on partial_close / break_even / close events. '
    'Used by the POSITION_SYNC_RESPONSE to rehydrate the EA after a restart.';
