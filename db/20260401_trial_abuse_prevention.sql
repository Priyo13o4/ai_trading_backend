CREATE TABLE IF NOT EXISTS public.fraud_prevention_lists (
    id SERIAL PRIMARY KEY,
    email_hash TEXT UNIQUE NOT NULL,
    recorded_at TIMESTAMPTZ DEFAULT NOW(),
    reason TEXT DEFAULT 'account_deletion_with_trial'
);
