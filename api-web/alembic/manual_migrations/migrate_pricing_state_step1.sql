-- Step 1 Migration: Add nuanced pricing fields while retaining the old boolean for downstream compatibility

-- 1. Add the new columns
ALTER TABLE email_news_analysis 
ADD COLUMN IF NOT EXISTS pricing_state VARCHAR(50),
ADD COLUMN IF NOT EXISTS reaction_certainty VARCHAR(50),
ADD COLUMN IF NOT EXISTS directional_confidence REAL,
ADD COLUMN IF NOT EXISTS repricing_type VARCHAR(50);

-- 2. Backfill existing data
-- Map boolean to pricing_state
-- True = 'priced_in', fundamental repricing, clear certainty.
-- False = 'not_priced_in', clear certainty.
UPDATE email_news_analysis
SET 
    pricing_state = CASE WHEN is_priced_in = true THEN 'priced_in' ELSE 'not_priced_in' END,
    reaction_certainty = 'clear',
    directional_confidence = 0.8,
    repricing_type = CASE WHEN is_priced_in = true THEN 'fundamental' ELSE 'none' END
WHERE pricing_state IS NULL AND is_priced_in IS NOT NULL;
