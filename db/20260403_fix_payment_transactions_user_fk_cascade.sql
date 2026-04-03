BEGIN;

-- Align FK delete behavior with user_id NOT NULL and account deletion flow.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'payment_transactions_user_id_fkey'
          AND conrelid = 'public.payment_transactions'::regclass
    ) THEN
        ALTER TABLE public.payment_transactions
            DROP CONSTRAINT payment_transactions_user_id_fkey;
    END IF;
END;
$$;

ALTER TABLE public.payment_transactions
    ADD CONSTRAINT payment_transactions_user_id_fkey
    FOREIGN KEY (user_id)
    REFERENCES public.profiles(id)
    ON DELETE CASCADE;

COMMIT;
