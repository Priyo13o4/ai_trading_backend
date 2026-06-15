-- Migration: widen price columns from NUMERIC(10,5) to NUMERIC(20,8)
-- Reason: NUMERIC(10,5) overflows at 99999.99999 — BTCUSD prices > 100k INSERT-fail.
-- Applied: 2026-06-15  (widening an existing NUMERIC is always safe, no data loss)
-- Safe to re-run: ALTER TABLE TYPE on a wider/compatible type is idempotent.

BEGIN;

ALTER TABLE live_trade_state
    ALTER COLUMN entry_price         TYPE NUMERIC(20,8),
    ALTER COLUMN original_sl         TYPE NUMERIC(20,8),
    ALTER COLUMN original_tp         TYPE NUMERIC(20,8),
    ALTER COLUMN max_favorable_price TYPE NUMERIC(20,8),
    ALTER COLUMN max_adverse_price   TYPE NUMERIC(20,8);

ALTER TABLE strategies
    ALTER COLUMN stop_loss   TYPE NUMERIC(20,8),
    ALTER COLUMN take_profit TYPE NUMERIC(20,8);

ALTER TABLE signals
    ALTER COLUMN entry_price TYPE NUMERIC(20,8),
    ALTER COLUMN exit_price  TYPE NUMERIC(20,8),
    ALTER COLUMN stop_loss   TYPE NUMERIC(20,8),
    ALTER COLUMN take_profit TYPE NUMERIC(20,8);

COMMIT;
