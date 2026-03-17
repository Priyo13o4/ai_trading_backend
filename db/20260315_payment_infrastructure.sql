-- Payment Infrastructure Migration (Sleeper Mode)

-- 1. Add new columns to existing tables
ALTER TABLE public.profiles 
  ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT UNIQUE;

ALTER TABLE public.user_subscriptions 
  ADD COLUMN IF NOT EXISTS plan_snapshot JSONB DEFAULT '{}'::JSONB;

-- 2. Create the unified payment_transactions table (Source of Truth)
CREATE TABLE IF NOT EXISTS public.payment_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    subscription_id UUID REFERENCES public.user_subscriptions(id) ON DELETE SET NULL,
    amount NUMERIC NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    provider TEXT NOT NULL CHECK (provider IN ('stripe', 'crypto', 'manual')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'succeeded', 'failed', 'refunded', 'cancelled')),
    
    -- External IDs for reconciliation
    external_payment_id TEXT,
    external_invoice_id TEXT,
    
    -- Crypto specific
    crypto_tx_hash TEXT UNIQUE,
    crypto_network TEXT,

    receipt_url TEXT,
    failure_reason TEXT,
    metadata JSONB DEFAULT '{}'::JSONB,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for fast lookup by user or external ID
CREATE INDEX IF NOT EXISTS idx_payment_transactions_user_id ON public.payment_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_ext_id ON public.payment_transactions(external_payment_id);

-- 3. Webhook Events (Idempotency and Audit)
CREATE TABLE IF NOT EXISTS public.webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    event_type TEXT NOT NULL,
    external_event_id TEXT UNIQUE NOT NULL,
    payload JSONB NOT NULL,
    processed BOOLEAN NOT NULL DEFAULT false,
    processing_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- 4. Crypto Invoices
CREATE TABLE IF NOT EXISTS public.crypto_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id),
    payment_transaction_id UUID REFERENCES public.payment_transactions(id),
    wallet_address TEXT NOT NULL,
    network TEXT NOT NULL,
    token TEXT NOT NULL,
    amount_expected NUMERIC NOT NULL,
    amount_received NUMERIC DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending', 'partial', 'paid', 'expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. Payment Audit Logs (Immutable Ledger)
CREATE TABLE IF NOT EXISTS public.payment_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id UUID REFERENCES public.payment_transactions(id),
    user_id UUID REFERENCES public.profiles(id),
    action TEXT NOT NULL,
    previous_state JSONB,
    new_state JSONB,
    actor TEXT NOT NULL, -- e.g., 'stripe_webhook', 'admin', 'system'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Update trigger for updated_at on payment_transactions
CREATE OR REPLACE FUNCTION update_modified_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS trigger_update_payment_tx ON public.payment_transactions;
CREATE TRIGGER trigger_update_payment_tx
    BEFORE UPDATE ON public.payment_transactions
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

-- Update trigger for updated_at on crypto_invoices
DROP TRIGGER IF EXISTS trigger_update_crypto_inv ON public.crypto_invoices;
CREATE TRIGGER trigger_update_crypto_inv
    BEFORE UPDATE ON public.crypto_invoices
    FOR EACH ROW
    EXECUTE FUNCTION update_modified_column();

-- RLS Policies
ALTER TABLE public.payment_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.crypto_invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_audit_logs ENABLE ROW LEVEL SECURITY;

-- Users can read their own transactions
CREATE POLICY read_own_transactions ON public.payment_transactions 
    FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY read_own_crypto_invoices ON public.crypto_invoices 
    FOR SELECT TO authenticated USING (auth.uid() = user_id);

-- System services bypass RLS (service role key)
