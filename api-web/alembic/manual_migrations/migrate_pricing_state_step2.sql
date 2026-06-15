-- Step 2 Migration: Drop the old boolean field now that pricing_state is fully integrated
ALTER TABLE email_news_analysis 
DROP COLUMN IF EXISTS is_priced_in;
