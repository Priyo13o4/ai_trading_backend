CREATE TABLE IF NOT EXISTS public.used_trial_emails (
    email_hash TEXT PRIMARY KEY,
    deleted_at TIMESTAMPTZ DEFAULT NOW()
);

-- Note: We can modify the verify_and_delete_account to insert into this table.
