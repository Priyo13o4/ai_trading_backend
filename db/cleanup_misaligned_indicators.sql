-- Cleanup: Remove legacy misaligned technical indicator rows
--
-- After migrating to Timescale continuous aggregates, indicator timestamps
-- must align exactly with cagg bucket timestamps. This removes orphaned/misaligned rows.
--
-- Safe to re-run.

DO $$
DECLARE
  total_before bigint;
  deleted_count bigint;
BEGIN
  SELECT COUNT(*) INTO total_before FROM technical_indicators;
  RAISE NOTICE 'Total indicators before cleanup: %', total_before;

  -- Delete W1 indicators not aligned with new forex-week buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'W1'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks c
      WHERE c.symbol = ti.symbol AND c.time = ti.time AND c.timeframe = 'W1'
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned W1 indicators: %', deleted_count;

  -- Delete MN1 indicators not aligned with cagg buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'MN1'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks c
      WHERE c.symbol = ti.symbol AND c.time = ti.time AND c.timeframe = 'MN1'
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned MN1 indicators: %', deleted_count;

  -- Delete D1 indicators not aligned with cagg buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'D1'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks c
      WHERE c.symbol = ti.symbol AND c.time = ti.time AND c.timeframe = 'D1'
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned D1 indicators: %', deleted_count;

  -- Delete H4 indicators not aligned with cagg buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'H4'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks_h4 c
      WHERE c.symbol = ti.symbol AND c.time = ti.time
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned H4 indicators: %', deleted_count;

  -- Delete H1 indicators not aligned with cagg buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'H1'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks_h1 c
      WHERE c.symbol = ti.symbol AND c.time = ti.time
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned H1 indicators: %', deleted_count;

  -- Delete M30 indicators not aligned with cagg buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'M30'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks_m30 c
      WHERE c.symbol = ti.symbol AND c.time = ti.time
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned M30 indicators: %', deleted_count;

  -- Delete M15 indicators not aligned with cagg buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'M15'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks_m15 c
      WHERE c.symbol = ti.symbol AND c.time = ti.time
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned M15 indicators: %', deleted_count;

  -- Delete M5 indicators not aligned with cagg buckets
  DELETE FROM technical_indicators ti
  WHERE ti.timeframe = 'M5'
    AND NOT EXISTS (
      SELECT 1 FROM candlesticks_m5 c
      WHERE c.symbol = ti.symbol AND c.time = ti.time
    );
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RAISE NOTICE 'Deleted misaligned M5 indicators: %', deleted_count;

  SELECT COUNT(*) INTO total_before FROM technical_indicators;
  RAISE NOTICE 'Total indicators after cleanup: %', total_before;
END $$;
