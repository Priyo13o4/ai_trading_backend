BEGIN;

-- Remove now-deleted soft-delete filters (archived/archived_at) from helper functions.

CREATE OR REPLACE FUNCTION public.get_active_strategies(pair character varying)
RETURNS TABLE(
  strategy_id integer,
  strategy_name character varying,
  direction character varying,
  entry_signal jsonb,
  take_profit numeric,
  stop_loss numeric,
  confidence character varying,
  expiry_time timestamp with time zone
)
LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT
    s.strategy_id,
    s.strategy_name,
    s.direction,
    s.entry_signal,
    s.take_profit,
    s.stop_loss,
    s.confidence,
    s.expiry_time
  FROM strategies s
  WHERE s.trading_pair = pair
    AND s.status = 'active'
    AND s.expiry_time > NOW()
  ORDER BY s.confidence DESC, s.timestamp DESC;
END;
$function$;


CREATE OR REPLACE FUNCTION public.get_latest_regime(pair character varying)
RETURNS TABLE(
  regime_type character varying,
  regime_summary text,
  confidence_score numeric,
  analysis_timestamp timestamp with time zone
)
LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT
    rd.regime_type,
    rd.regime_summary,
    rd.confidence_score,
    rd.analysis_timestamp
  FROM regime_data rd
  WHERE rd.trading_pair = pair
  ORDER BY rd.analysis_timestamp DESC
  LIMIT 1;
END;
$function$;


CREATE OR REPLACE FUNCTION public.get_pair_performance(pair character varying)
RETURNS TABLE(
  total_trades bigint,
  winning_trades bigint,
  losing_trades bigint,
  total_pnl numeric,
  win_rate numeric,
  avg_win numeric,
  avg_loss numeric,
  best_trade numeric,
  worst_trade numeric
)
LANGUAGE plpgsql
AS $function$
BEGIN
  RETURN QUERY
  SELECT
    COUNT(*)::BIGINT AS total_trades,
    COUNT(*) FILTER (WHERE pnl > 0)::BIGINT AS winning_trades,
    COUNT(*) FILTER (WHERE pnl < 0)::BIGINT AS losing_trades,
    COALESCE(SUM(pnl), 0) AS total_pnl,
    CASE
      WHEN COUNT(*) > 0 THEN ROUND((COUNT(*) FILTER (WHERE pnl > 0)::NUMERIC / COUNT(*)::NUMERIC * 100), 2)
      ELSE 0
    END AS win_rate,
    COALESCE(AVG(pnl) FILTER (WHERE pnl > 0), 0) AS avg_win,
    COALESCE(AVG(pnl) FILTER (WHERE pnl < 0), 0) AS avg_loss,
    COALESCE(MAX(pnl), 0) AS best_trade,
    COALESCE(MIN(pnl), 0) AS worst_trade
  FROM signals
  WHERE trading_pair = pair
    AND status LIKE 'closed%';
END;
$function$;

COMMIT;
