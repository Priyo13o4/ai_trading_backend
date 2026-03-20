# PAYMENT SYSTEM ARCHITECTURE PRD

*(Provider-Agnostic Payments: Razorpay + Crypto + Stripe Future | Supabase + FastAPI | Sleeper Mode Deployment)*

**Project:** PipFactor — AI Trading Tool
**Domain:** `pipfactor.com` / `api.pipfactor.com`
**Last audited:** 2026-03-14

---

## Preamble — Agent Context

This document is the **sole implementation handoff** for adding provider-agnostic payment infrastructure to PipFactor. The implementing agent must follow this document exactly.

**Assumptions:**

* Agent has full repo access to both repositories
* Agent can inspect schema via Supabase MCP
* Payments are **NOT activated yet** (sleeper architecture)
* Deployment should be **one-switch activation**
* Agent will NOT break existing schema, RLS policies, or auth flows

**Key constraint — read before coding:**

```
There are NO Supabase Edge Functions in this project.
The backend is a FastAPI application (api-web service, port 8080).
All "edge function" references in this PRD mean FastAPI endpoints.
Do NOT create a supabase/functions/ directory.
```

Architecture verification:

```
Supabase Edge Functions are OPTIONAL for payment integrations, not required.
This repository standardizes webhook and payment server logic in FastAPI.
Therefore webhook handlers must live in api-web (not in Supabase Edge Functions).
```

---

## Repository Layout

```
ai_trading_bot/                         ← Backend monorepo
  api-web/                              ← FastAPI HTTP API (port 8080) — ALL new endpoints go here
    app/
      main.py                           ← Router mounts, CORS, CSRF middleware
      auth.py                           ← auth_context / optional_auth_context dependencies
      authn/
        routes.py                       ← /auth/* endpoints (exchange, validate, logout, invalidate)
        authz.py                        ← require_permission(ctx, "signals")
        session_store.py                ← Redis session CRUD
        supabase_rpc.py                 ← rpc_get_active_subscription() via service_role
        rate_limit_auth.py              ← per-endpoint rate limiting
        token_verify.py                 ← Supabase JWT verification via JWKS
      redis_cache.py                    ← CACHE_REDIS async client (data Redis)
      rate_limiter.py                   ← external API key rate limiter
      sse.py                            ← SSE streaming endpoints
      routes/
        historical.py                   ← /api/historical/* routes
    start.sh                            ← gunicorn + uvicorn launcher
    requirements.txt                    ← Python deps
  api-worker/                           ← MT5 ingest + data processing (NOT for payment code)
  common/                               ← Shared Python package (trading-common)
  db/                                   ← SQL migration files (applied to Supabase via SQL Editor)
    20260307_phase1_security_hardening.sql   ← Current subscription RPCs + RBAC
    20260307_phase2_definer_search_path_hardening.sql
    schema.sql                          ← TimescaleDB tables (candlesticks, indicators — separate DB)
  docs/
    SUBSCRIPTION_SYSTEM_DOCS.md
    BETA_TO_PRODUCTION_MIGRATION.md     ← Contains user_pair_selections, account_deletion DDL, handle_new_user trigger
    PRODUCTION_DEPLOYMENT.md
  ../PRODUCTION_DEPLOYMENT_GUIDE.md     ← Root deployment guide also exists in this workspace
  scripts/
    supabase_policy_check.sql           ← RLS audit queries
    security_regression_smoke.sh        ← Smoke tests for auth enforcement
  docker-compose.yml                    ← 8 services: postgres, redis, redis-sessions, n8n, n8n-worker, api-web, api-worker, scraper
  docker-compose.prod.yml              ← Production resource limits overlay
  .env.example                          ← All env vars (110+ lines)

ai-trading_frontend/                    ← Vite + React + TypeScript + shadcn/ui
  src/
    services/
      api.ts                            ← ApiService class (fetch wrapper to FastAPI, cookie auth)
      subscriptionService.ts            ← SubscriptionService class (Supabase RPC calls)
      sseService.ts                     ← SSE real-time streaming
    hooks/
      useAuth.tsx                        ← AuthProvider + useAuth hook (plan, permissions, canAccessSignals)
      useSubscription.tsx                ← Subscription management hook
    components/
      subscription/
        PricingPlanCard.tsx
        QuickPricingTable.tsx
        SubscriptionStatus.tsx
        planCatalog.ts                  ← Plan normalization + tier logic
      ProtectedRoute.tsx                ← Route guard (CURRENTLY DISABLED — commented out in App.tsx)
      RequireAuth.tsx                    ← Inline auth gate (CURRENTLY DISABLED)
    pages/
      Pricing.tsx                       ← Pricing page (handleSubscribe is a STUB — shows toast only)
      Profile.tsx                        ← Profile page (cancel button is a STUB)
    types/
      subscription.ts                   ← SubscriptionPlan, UserSubscription, PaymentHistory, etc.
    lib/
      supabase.ts                       ← Supabase client (sessionStorage, auto-refresh)
  vite.config.ts                        ← CSP headers (NO payment provider domains yet)
  .env.example
  package.json                          ← NO payment SDK installed
```

---

## Objective

Prepare the application for **future monetization** without enabling payments yet.

The system must:

* Introduce **robust payment infrastructure**
* Remain **inactive by default**
* Require **minimal change to enable paywall**
* Maintain **production-grade security standards**
* Avoid breaking existing schema or features

The architecture must support:

```
Razorpay payments (primary)
Crypto payments (NOWPayments/Coinbase hosted invoices)
Stripe payments (future, no schema changes required)
Subscriptions (monthly/yearly/lifetime — already partially modeled)
One-time purchases (future flexibility)
Audit logging (all state transitions)
Webhook reconciliation (idempotent, replay-safe)
```

---

# Current System Overview

## Supabase Tables (cloud-hosted, NOT self-hosted)

```
profiles                     ← id (PK, FK auth.users), email, full_name, avatar_url, is_active, email_verified
subscription_plans           ← id, name, display_name, price_usd, billing_period, features (JSONB), pairs_allowed (TEXT[]),
                               ai_analysis_enabled, priority_support, api_access_enabled
user_subscriptions           ← id, user_id (FK profiles), plan_id (FK subscription_plans), status, started_at, expires_at,
                               payment_provider, external_subscription_id, auto_renew, cancel_at_period_end, metadata (JSONB)
payment_history              ← id, user_id, subscription_id, amount, currency, status, provider,
                               external_payment_id, invoice_url, receipt_url, payment_method_type/last4, failure_*, refund_*
user_pair_selections         ← id, user_id, subscription_id, selected_pairs (TEXT[]), locked_until, can_change_pairs
account_deletion_requests    ← id, user_id, otp_code, otp_expires_at, verified
```

## Existing Supabase RPC Functions (SECURITY DEFINER, search_path hardened)

| Function | Access | Purpose |
|---|---|---|
| `get_active_subscription(p_user_id)` | authenticated + service_role | Returns current subscription + plan details |
| `create_subscription(p_user_id, p_plan_id, p_payment_provider, p_external_id, p_trial_days, p_metadata)` | service_role ONLY | Creates subscription, cancels prior active ones |
| `renew_subscription(p_subscription_id, p_payment_id)` | service_role ONLY | Extends expiry, sets status=active |
| `cancel_subscription(p_subscription_id, p_immediate)` | service_role ONLY | Cancels immediately or at period end |
| `record_payment(p_user_id, p_subscription_id, p_amount, p_currency, p_provider, p_external_payment_id, p_status, p_metadata)` | service_role ONLY | Inserts into payment_history, updates subscription |
| `can_access_pair(p_user_id, p_trading_pair)` | authenticated | Checks pair access against subscription |
| `select_trading_pairs(p_user_id, p_selected_pairs)` | authenticated | Updates user pair selections |
| `expire_subscriptions()` | service_role | Marks expired subscriptions |
| `handle_new_user()` | trigger on auth.users INSERT | Creates profile row |

## Existing RLS Policies

```
subscription_plans  → public_read_active_plans: SELECT for anon+authenticated WHERE is_active=true
user_subscriptions  → users view own only (auth.uid() = user_id)
payment_history     → users view own only (auth.uid() = user_id)
user_pair_selections → users SELECT own; service_role ALL
account_deletion_requests → users SELECT own; service_role ALL
profiles            → users view own only
```

## Current Auth Flow (DO NOT BREAK)

```
1. User signs in via Supabase Auth (frontend)
2. Frontend calls POST /auth/exchange with Supabase access_token
3. Backend verifies JWT via JWKS (RS256/ES256)
4. Backend calls rpc_get_active_subscription(user_id) via service_role
5. If subscription.is_current AND status in ('active','trial'):
     plan = plan_name, permissions = ["dashboard", "signals"]
   Else:
     plan = "free", permissions = ["dashboard"]
6. Session stored in Redis (redis-sessions container, ephemeral)
7. httpOnly session cookie + CSRF cookie set on response
8. All subsequent API calls auth'd via cookie → Redis session lookup
9. Endpoints use auth_context dependency → require_permission(ctx, "signals")
```

**Critical:** The permission cache lives in Redis with 15-minute TTL (`user:perms:{user_id}`). When a payment webhook or administrative action (deletion/logout-all) changes status, the system **automatically** invalidates this cache via **Supabase Database Triggers (`pg_net`)** hitting `POST /auth/invalidate`.

## Current Subscription Plans (in database)

| name | display_name | price_usd | billing_period | pairs_allowed |
|---|---|---|---|---|
| beta | Beta Access | 0.00 | yearly | {XAUUSD,EURUSD,GBPUSD,USDJPY,BTCUSD} |
| starter | Starter | 5.00 | monthly | {} (user selects 1) |
| professional | Professional | 8.00 | monthly | {} (user selects 3) |
| elite | Elite | 12.00 | monthly | {XAUUSD,EURUSD,GBPUSD,USDJPY,BTCUSD} |

## What Is Already Partially Payment-Ready

```
✓ user_subscriptions has payment_provider field (stripe|razorpay|paypal|manual)
✓ user_subscriptions has external_subscription_id field
✓ payment_history has external_payment_id, invoice_url, receipt_url, refund fields
✓ TypeScript types mirror all of the above
✓ subscriptionService.ts already calls create_subscription with paymentProvider parameter
✓ useSubscription hook returns subscribe(planId, {paymentProvider}) function
✓ Backend auth exchange already reads subscription data and maps to permissions
✓ Auth invalidation webhook (POST /auth/invalidate) exists with HMAC + replay protection
✓ Redis rate limiting infrastructure exists
✓ CSRF middleware exists (exempt /auth/exchange and /auth/invalidate)
```

## What Is Missing (this PRD adds)

```
✗ payment_transactions table (unified state machine)
✗ webhook_events table (idempotent event store)
✗ crypto_invoices table
✗ payment_audit_logs table
✗ provider_customers table
✗ provider_prices table
✗ user_subscriptions.plan_snapshot column
✗ Provider abstraction layer in FastAPI payment module
✗ FastAPI provider-agnostic endpoints (checkout, webhooks)
✗ PAYMENTS_ENABLED feature flag
✗ Provider env vars in .env.example
✗ CSP headers for payment domains (vite.config.ts)
✗ Frontend checkout flow wiring (Pricing.tsx handleSubscribe is a stub)
✗ Frontend cancel/manage subscription flow (Profile.tsx is a stub)
✗ Subscription expiration cron job (documented but not deployed)
```

---

# High-Level Architecture

Payment infrastructure must follow this pattern:

```
Frontend (React)
  ↓ POST /api/payments/create-checkout (cookie-authed, CSRF-protected, provider-driven)
FastAPI Endpoint (api-web)
  ↓ get_provider(provider).create_checkout(...)
Provider Checkout / Hosted Invoice
  ↓ (user completes payment with selected provider)
Provider Webhook → POST /api/webhooks/{provider} (signature-verified, no JWT)
    ↓
FastAPI Webhook Handler
    ↓ store in webhook_events → update payment_transactions → call Supabase RPCs → audit log
Database State Update (Supabase via service_role)
    ↓ Database Trigger (`on_auth_user_deleted` / `on_auth_session_deleted`)
    ↓ POST /auth/invalidate (signed HMAC request via pg_net)
    ↓ Backend wipes Redis perms cache + all user sessions
User's Next Request Picks Up New Permissions (or is forced to re-login)
```

**Important rules:**

```
Payment state must NEVER rely on frontend confirmation.
Only webhooks determine final payment state.
Frontend only redirects to provider checkout URL or displays provider invoice URL.
The existing record_payment() and create_subscription() RPCs MUST be reused — do not duplicate logic.
NEVER trust success_url/cancel_url redirects to activate or cancel subscriptions.
Only verified webhook events may change subscription/payment state.
```

---

# Sleeper Mode Payment Design

Payments must be **implemented but disabled**.

## Backend Feature Flag

Add to `.env.example` and `.env`:

```
PAYMENTS_ENABLED=false
```

Create a reusable dependency in `api-web/app/authn/payments_gate.py`:

```python
import os
from fastapi import HTTPException

def require_payments_enabled():
    if os.getenv("PAYMENTS_ENABLED", "false").lower() != "true":
        raise HTTPException(status_code=503, detail="Payments are not enabled")
```

All payment endpoints must use `Depends(require_payments_enabled)`.

## Frontend Feature Flag

Add to frontend `.env.example`, `.env.development`, `.env.production`:

```
VITE_PAYMENTS_ENABLED=false
```

In `Pricing.tsx`, the `handleSubscribe` function must check:

```typescript
if (import.meta.env.VITE_PAYMENTS_ENABLED !== 'true') {
  toast.info('Checkout is coming soon.');
  return;
}
```

Update `vite-env.d.ts` to declare `VITE_PAYMENTS_ENABLED`.

This allows **one-click monetization rollout** by flipping both flags to `true`.

---

# Required Database Additions

**All changes below go into a single migration file:**

```
ai_trading_bot/db/20260315_payment_infrastructure.sql
```

**Migration must be idempotent** (use `IF NOT EXISTS` / `CREATE OR REPLACE`).
**No destructive changes** (no DROP, no ALTER TYPE, no column renames on existing tables).
**Must follow existing security patterns** (SECURITY DEFINER, SET search_path = pg_catalog, public, REVOKE from PUBLIC/anon/authenticated, GRANT to service_role only for mutations).

---

## 1. Schema Cleanup + Provider Mapping Tables

Remove provider-specific schema fields:

```sql
ALTER TABLE public.profiles
  DROP COLUMN IF EXISTS stripe_customer_id;

ALTER TABLE public.subscription_plans
  DROP COLUMN IF EXISTS stripe_price_id,
  DROP COLUMN IF EXISTS stripe_product_id;
```

Create provider customer mapping:

```sql
CREATE TABLE IF NOT EXISTS public.provider_customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_customer_id TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT provider_customers_provider_customer_unique UNIQUE (provider, provider_customer_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_customers_user_provider
  ON public.provider_customers(user_id, provider);
```

Create provider plan-price mapping:

```sql
CREATE TABLE IF NOT EXISTS public.provider_prices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES public.subscription_plans(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_price_id TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT provider_prices_provider_price_unique UNIQUE (provider, provider_price_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_prices_plan_provider
  ON public.provider_prices(plan_id, provider);
```

Provider compatibility constraints:

```sql
-- Ensure provider fields accept: razorpay, stripe, coinbase, nowpayments
-- Apply additive CHECK updates where constraints already exist.

-- Example: user_subscriptions.payment_provider
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'user_subscriptions_payment_provider_check'
      AND conrelid = 'public.user_subscriptions'::regclass
  ) THEN
    ALTER TABLE public.user_subscriptions
      DROP CONSTRAINT user_subscriptions_payment_provider_check;

    ALTER TABLE public.user_subscriptions
      ADD CONSTRAINT user_subscriptions_payment_provider_check
      CHECK (payment_provider = ANY (ARRAY['razorpay','stripe','coinbase','nowpayments']::text[]));
  END IF;
END
$$;
```

Design rule:

```
Do not add provider-specific ID columns.
All provider identifiers must flow through:
payment_transactions.provider,
payment_transactions.provider_payment_id,
payment_transactions.provider_subscription_id,
payment_transactions.provider_checkout_session_id
```

Keep these tables unchanged (no structural redesign in this PRD):

```
payment_transactions
webhook_events
payment_audit_logs
crypto_invoices
```

---

## 2. Payment Transactions Table

A **unified payment state machine** that is the source of truth for all payment attempts.

```sql
CREATE TABLE IF NOT EXISTS public.payment_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    subscription_id UUID REFERENCES public.user_subscriptions(id) ON DELETE SET NULL,

    provider TEXT NOT NULL,                    -- 'razorpay', 'nowpayments', 'stripe'
    provider_payment_id TEXT,                  -- stripe payment_intent ID, crypto invoice ID
    provider_checkout_session_id TEXT,         -- stripe checkout.session ID (for linking)
    provider_subscription_id TEXT,             -- stripe subscription.id for renewal/cancel webhooks

    amount NUMERIC(12,2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',

    status TEXT NOT NULL DEFAULT 'pending',    -- pending, processing, succeeded, failed, refunded, cancelled, expired

    last_provider_event_time TIMESTAMPTZ,      -- prevents out-of-order webhook regressions

    payment_type TEXT NOT NULL DEFAULT 'subscription', -- 'subscription', 'one_time'

    metadata JSONB DEFAULT '{}',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Constraints
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'payment_transactions_status_check'
      AND conrelid = 'public.payment_transactions'::regclass
  ) THEN
    ALTER TABLE public.payment_transactions
      ADD CONSTRAINT payment_transactions_status_check
      CHECK (status IN ('pending', 'processing', 'succeeded', 'failed', 'refunded', 'cancelled', 'expired'));
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'payment_transactions_payment_type_check'
      AND conrelid = 'public.payment_transactions'::regclass
  ) THEN
    ALTER TABLE public.payment_transactions
      ADD CONSTRAINT payment_transactions_payment_type_check
      CHECK (payment_type IN ('subscription', 'one_time'));
  END IF;
END
$$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_payment_transactions_user_id
  ON public.payment_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_payment_transactions_provider_payment_id
  ON public.payment_transactions(provider, provider_payment_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_transactions_provider_payment_id
  ON public.payment_transactions(provider, provider_payment_id)
  WHERE provider_payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payment_transactions_status
  ON public.payment_transactions(status)
  WHERE status IN ('pending', 'processing');
CREATE INDEX IF NOT EXISTS idx_payment_transactions_checkout_session
  ON public.payment_transactions(provider_checkout_session_id)
  WHERE provider_checkout_session_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_transactions_checkout_session
  ON public.payment_transactions(provider_checkout_session_id)
  WHERE provider_checkout_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payment_transactions_provider_subscription_id
  ON public.payment_transactions(provider_subscription_id)
  WHERE provider_subscription_id IS NOT NULL;

-- updated_at trigger (reuse existing function)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'set_payment_transactions_updated_at'
      AND tgrelid = 'public.payment_transactions'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER set_payment_transactions_updated_at
      BEFORE UPDATE ON public.payment_transactions
      FOR EACH ROW EXECUTE FUNCTION update_updated_at();
  END IF;
END
$$;

-- RLS
ALTER TABLE public.payment_transactions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'payment_transactions'
      AND policyname = 'Users view own payment transactions'
  ) THEN
    CREATE POLICY "Users view own payment transactions"
      ON public.payment_transactions FOR SELECT
      USING (auth.uid() = user_id);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'payment_transactions'
      AND policyname = 'Service role manages payment transactions'
  ) THEN
    CREATE POLICY "Service role manages payment transactions"
      ON public.payment_transactions FOR ALL
      USING (auth.role() = 'service_role');
  END IF;
END
$$;

REVOKE ALL ON TABLE public.payment_transactions FROM PUBLIC, anon;
GRANT SELECT ON TABLE public.payment_transactions TO authenticated;
GRANT ALL ON TABLE public.payment_transactions TO service_role;
```

---

## 3. Webhook Event Store

Webhooks must always be logged **before** processing. Idempotency key is `(provider, event_id)`.

```sql
CREATE TABLE IF NOT EXISTS public.webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,         -- 'razorpay', 'nowpayments', 'stripe'
    event_id TEXT NOT NULL,         -- provider's event ID (Stripe: evt_xxx, Coinbase: charge:xxx)
    event_type TEXT NOT NULL,       -- 'checkout.session.completed', 'charge:confirmed', etc.

    payload JSONB NOT NULL,

    processed BOOLEAN NOT NULL DEFAULT FALSE,
    processing_error TEXT,          -- error message if processing failed (for debugging)

    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,

    CONSTRAINT webhook_events_provider_event_unique UNIQUE (provider, event_id)
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_unprocessed
  ON public.webhook_events(provider, processed)
  WHERE processed = FALSE;

-- RLS: NO user-facing access. Service role only.
ALTER TABLE public.webhook_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'webhook_events'
      AND policyname = 'Service role manages webhook events'
  ) THEN
    CREATE POLICY "Service role manages webhook events"
      ON public.webhook_events FOR ALL
      USING (auth.role() = 'service_role');
  END IF;
END
$$;

REVOKE ALL ON TABLE public.webhook_events FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.webhook_events TO service_role;
```

---

## 4. Crypto Invoice Table

Crypto payments are tracked separately from Stripe since they have blockchain-specific fields.

```sql
CREATE TABLE IF NOT EXISTS public.crypto_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    transaction_id UUID REFERENCES public.payment_transactions(id) ON DELETE SET NULL,

    provider TEXT NOT NULL,          -- 'coinbase', 'nowpayments', 'btcpay'
    provider_payment_id TEXT NOT NULL, -- NOWPayments payment_id or invoice ID

    pay_address TEXT,                -- blockchain address to send funds to
    pay_currency TEXT,               -- BTC, ETH, USDT, USDC
    pay_amount NUMERIC(24,8),        -- amount in crypto denomination

    usd_amount NUMERIC(12,2) NOT NULL,  -- original USD price

    confirmations INT DEFAULT 0,
    required_confirmations INT DEFAULT 1,

    status TEXT NOT NULL DEFAULT 'waiting',  -- waiting, confirming, confirmed, failed, expired

    expires_at TIMESTAMPTZ,

    hosted_url TEXT,                 -- provider-hosted payment page URL

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'crypto_invoices_status_check'
      AND conrelid = 'public.crypto_invoices'::regclass
  ) THEN
    ALTER TABLE public.crypto_invoices
      ADD CONSTRAINT crypto_invoices_status_check
      CHECK (status IN ('waiting', 'confirming', 'confirmed', 'failed', 'expired'));
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_crypto_invoices_user_id
  ON public.crypto_invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_crypto_invoices_provider_invoice
  ON public.crypto_invoices(provider, invoice_id);
CREATE INDEX IF NOT EXISTS idx_crypto_invoices_status
  ON public.crypto_invoices(status)
  WHERE status IN ('waiting', 'confirming');

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'set_crypto_invoices_updated_at'
      AND tgrelid = 'public.crypto_invoices'::regclass
      AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER set_crypto_invoices_updated_at
      BEFORE UPDATE ON public.crypto_invoices
      FOR EACH ROW EXECUTE FUNCTION update_updated_at();
  END IF;
END
$$;

-- RLS
ALTER TABLE public.crypto_invoices ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'crypto_invoices'
      AND policyname = 'Users view own crypto invoices'
  ) THEN
    CREATE POLICY "Users view own crypto invoices"
      ON public.crypto_invoices FOR SELECT
      USING (auth.uid() = user_id);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'crypto_invoices'
      AND policyname = 'Service role manages crypto invoices'
  ) THEN
    CREATE POLICY "Service role manages crypto invoices"
      ON public.crypto_invoices FOR ALL
      USING (auth.role() = 'service_role');
  END IF;
END
$$;

REVOKE ALL ON TABLE public.crypto_invoices FROM PUBLIC, anon;
GRANT SELECT ON TABLE public.crypto_invoices TO authenticated;
GRANT ALL ON TABLE public.crypto_invoices TO service_role;
```

---

## 5. Payment Audit Log

All state transitions must be recorded for dispute resolution and reconciliation.

```sql
CREATE TABLE IF NOT EXISTS public.payment_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id UUID REFERENCES public.payment_transactions(id) ON DELETE SET NULL,
    entity_type TEXT NOT NULL,        -- 'payment_transaction', 'crypto_invoice', 'user_subscription'
    entity_id UUID NOT NULL,          -- the ID of the entity that changed

    previous_state TEXT,
    new_state TEXT NOT NULL,

    trigger_source TEXT NOT NULL,     -- 'stripe_webhook', 'crypto_webhook', 'admin', 'cron', 'manual'
    trigger_event_id TEXT,            -- webhook_events.event_id that caused this change

    reason TEXT,

    metadata JSONB DEFAULT '{}',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_audit_logs_transaction_id
  ON public.payment_audit_logs(transaction_id);
CREATE INDEX IF NOT EXISTS idx_payment_audit_logs_entity
  ON public.payment_audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_payment_audit_logs_created_at
  ON public.payment_audit_logs(created_at);

-- RLS: NO user-facing access. Service role only.
ALTER TABLE public.payment_audit_logs ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename = 'payment_audit_logs'
      AND policyname = 'Service role manages audit logs'
  ) THEN
    CREATE POLICY "Service role manages audit logs"
      ON public.payment_audit_logs FOR ALL
      USING (auth.role() = 'service_role');
  END IF;
END
$$;

REVOKE ALL ON TABLE public.payment_audit_logs FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.payment_audit_logs TO service_role;
```

---

## 6. Subscription System Improvements

Add plan snapshot column to preserve historical pricing:

```sql
ALTER TABLE public.user_subscriptions
  ADD COLUMN IF NOT EXISTS plan_snapshot JSONB;
```

The `create_subscription` RPC must be updated to populate this on creation:

```
plan_snapshot = {
  plan_name, display_name, price_usd, features, pairs_allowed, billing_period
}
```

**Agent note:** Create an additive migration update for `create_subscription` using `CREATE OR REPLACE FUNCTION` in `20260315_payment_infrastructure.sql` so existing environments are updated safely.

Compatibility requirement:

```
Do NOT edit historical migration files already applied in production.
Implement the updated create_subscription() as CREATE OR REPLACE FUNCTION in 20260315_payment_infrastructure.sql.
After function replacement, re-apply REVOKE/GRANT on that exact function signature.
```

If user_subscriptions has a payment_provider CHECK constraint, update it additively to include 'coinbase' before crypto webhook paths call create_subscription().

---

# Redis Usage (Two Instances — Critical)

**The project has TWO separate Redis instances. Do not mix them.**

| Redis Instance | Container | Persistence | Purpose |
|---|---|---|---|
| `redis` (n8n-redis) | Port 6379 internal | AOF + RDB | Data cache, pub/sub, rate limiting, n8n queues |
| `redis-sessions` | Port 6379 internal | NONE (ephemeral) | Session cookies, perms cache, replay guards |

For payment integration:

**Use `redis` (data cache) for:**

```
webhook idempotency fast-path:  webhook:{provider}:{event_id}  TTL 24h
rate limiting payment endpoints: rl:payment:{ip}:{minute}       TTL 60s
```

**Use `redis-sessions` for:**

```
session invalidation after subscription change: existing POST /auth/invalidate flow
permissions cache flush: existing delete user:perms:{user_id} key
```

**Never use Redis as payment source of truth.** The `webhook_events` table is the durable idempotency store. Redis is only a fast-path check to avoid unnecessary DB queries.

---

# Required Backend Changes (FastAPI)

## Provider Abstraction Architecture

Refactor payments module to provider-driven structure:

```
api-web/app/
  payments/
    __init__.py
    gate.py
    routes.py
    webhook_handler.py
    constants.py
    payment_providers/
      base.py          ← provider interface
      razorpay.py      ← Razorpay implementation (primary)
      crypto.py        ← NOWPayments/Coinbase implementation
      stripe.py        ← future implementation (optional)
      router.py        ← get_provider(provider: str)
```

All endpoint/business logic must call providers only through `get_provider(provider)`.

## New Environment Variables

Add to `ai_trading_bot/.env.example`:

```
# Payment feature flag
PAYMENTS_ENABLED=false

# Razorpay (primary)
RAZORPAY_KEY_ID=CHANGE_ME
RAZORPAY_KEY_SECRET=CHANGE_ME
RAZORPAY_WEBHOOK_SECRET=CHANGE_ME

# Crypto (NOWPayments/Coinbase)
NOWPAYMENTS_API_KEY=CHANGE_ME
NOWPAYMENTS_WEBHOOK_SECRET=CHANGE_ME
COINBASE_COMMERCE_API_KEY=CHANGE_ME
COINBASE_COMMERCE_WEBHOOK_SECRET=CHANGE_ME

# Stripe (future, optional)
STRIPE_SECRET_KEY=CHANGE_ME
STRIPE_API_VERSION=2024-10-28
STRIPE_WEBHOOK_SECRET=CHANGE_ME
```

## Provider Contract

`payment_providers/base.py` must define the common interface:

```
create_checkout(...)
verify_webhook(...)
parse_webhook_event(...)
map_event_to_state(...)
```

## Endpoint Specifications

### POST /api/payments/create-checkout

```
Auth:          Depends(auth_context)   ← existing cookie session auth
Feature gate:  Depends(require_payments_enabled)
CSRF:          Required (existing middleware handles this — NOT in exempt list)
Rate limit:    5/min per user

Request body:
{
  "plan_id": "uuid",
  "provider": "razorpay" | "crypto" | "stripe",
  "billing_period": "monthly" | "yearly"    ← optional
}

Flow:
1. Validate user is authenticated (auth_context)
2. Resolve provider with `get_provider(provider)`
3. Fetch plan from subscription_plans
4. Fetch provider price mapping from provider_prices for (plan_id, provider)
5. Create payment_transaction with status='pending' and generic provider_* fields only
6. Call provider implementation to create checkout/invoice/order
7. Persist provider IDs into payment_transactions generic columns
8. Return {checkout_url}
```

### POST /api/webhooks/{provider}

```
Auth:          NONE
Feature gate:  Always accept, verify, and store webhook events.
CSRF exempt:   YES — add "/api/webhooks/{provider}" paths in middleware handling
Signature:     Verified by provider handler (`get_provider(provider).verify_webhook`)
Rate limit:    none (provider-origin traffic)

Flow:
1. Read raw request body
2. Resolve provider from path and verify signature with provider handler
3. Check Redis + webhook_events idempotency by (provider, event_id)
4. Insert webhook_events(provider, event_id, event_type, payload)
5. If PAYMENTS_ENABLED=false, store event and return 202
6. Parse provider event to generic state transition via provider handler
7. Update payment_transactions using only provider + provider_* fields
8. Call create_subscription/renew_subscription/cancel_subscription/record_payment as needed
9. Write payment_audit_logs and invalidate auth permissions
10. Mark webhook_events.processed=true on successful processing
11. Return 2xx on success/duplicate, non-2xx on processing failure
```

---

# Required Frontend Changes

## Provider-Driven Checkout Calls

## New Environment Variable

Add to `.env.example`, `.env.development`, `.env.production`:

```
VITE_PAYMENTS_ENABLED=false
VITE_DEFAULT_PAYMENT_PROVIDER=razorpay
```

Update `vite-env.d.ts`:

```typescript
interface ImportMetaEnv {
  // ... existing vars ...
  readonly VITE_PAYMENTS_ENABLED?: string;
  readonly VITE_DEFAULT_PAYMENT_PROVIDER?: string;
}
```

## CSP Header Update

In `vite.config.ts`, update the Content-Security-Policy for active providers.

```
script-src: add provider SDK domains as enabled
frame-src:  add provider hosted payment domains
connect-src: add provider API domains
```

## API Service Updates

Add to `api.ts` (ApiService class):

```typescript
async createCheckout(
  planId: string,
  provider: 'razorpay' | 'crypto' | 'stripe' = 'razorpay',
  billingPeriod?: string,
): Promise<{ checkout_url: string }> {
  return this.post('/api/payments/create-checkout', {
    plan_id: planId,
    provider,
    billing_period: billingPeriod,
  });
}
```

## Pricing.tsx Updates

Replace the `handleSubscribe` stub:

```typescript
const handleSubscribe = async (tierName: string, provider: 'razorpay' | 'crypto' | 'stripe' = 'razorpay') => {
  if (import.meta.env.VITE_PAYMENTS_ENABLED !== 'true') {
    toast.info('Checkout is coming soon.');
    return;
  }
  if (!isAuthenticated) {
    toast.info('Please sign in to continue');
    navigate('/?signin=true');
    return;
  }
  try {
    // Find the plan in the database plans list
    const plan = plans.find(p => normalizePlanName(p.name) === tierName);
    if (!plan) throw new Error('Plan not found');

    const { checkout_url } = await apiService.createCheckout(plan.id, provider);
    window.location.href = checkout_url;   // Redirect to provider checkout/invoice page
  } catch (err) {
    toast.error('Failed to start checkout. Please try again.');
  }
};
```

Add optional secondary action on pricing card:

```typescript
<Button onClick={() => handleSubscribe(tier.name, 'crypto')}>Pay with Crypto</Button>
```

## Success/Cancel Pages

After provider checkout/invoice completion, users are redirected to configured success/cancel URLs.

In `Profile.tsx` (success URL target), detect the `?payment=success` query param:

```typescript
useEffect(() => {
  const params = new URLSearchParams(location.search);
  if (params.get('payment') === 'success') {
    toast.success('Payment successful! Your subscription is being activated.');
    refreshProfile();  // re-fetch subscription from backend
    navigate('/profile', { replace: true });  // clean URL
  }
}, []);
```

In `Pricing.tsx`, detect `?payment=cancelled` and show a toast.

---

# CORS and CSRF Updates (main.py)

The agent MUST update webhook path handling in CSRF middleware for provider routes:

```python
request_path.startswith("/api/webhooks/")
```

CORS does not need changes — webhook endpoints are called server-to-server, not from browsers.

Access-boundary rule:

```
Backend writes and payment mutations always use Supabase service_role credentials.
Frontend performs read-only subscription visibility operations via RLS-safe paths.
Do not move mutation logic to frontend Supabase client calls.
```

## Router Mount

In `main.py`, add the payment router:

```python
from app.payments.routes import payment_router
app.include_router(payment_router)
```

---

# Auth Invalidation After Payment

**This is the most commonly missed step.** When a webhook changes subscription status:

1. The Supabase RPCs update the database
2. But the user's session in Redis still has the OLD `plan` and `permissions`
3. The `user:perms:{user_id}` cache also has the old data (15-min TTL)

**Solution:** After every successful subscription change in a webhook handler:

```python
async def invalidate_user_permissions(user_id: str):
    """Call the existing /auth/invalidate endpoint internally."""
    # Option A: Call the endpoint via HTTP (with HMAC signature)
    # Option B: Directly delete Redis keys (simpler for internal calls)
    await SESSION_REDIS.delete(f"user:perms:{user_id}")
    # Optionally destroy all sessions to force re-authentication:
    # This uses the existing session_store functions
    from app.authn.session_store import destroy_all_sessions
    await destroy_all_sessions(user_id)
```

The agent must decide: either call the HTTP endpoint with a signed webhook (reusing existing HMAC infrastructure), or directly import the session store functions. The direct approach is simpler for internal webhook handlers that already run in the same process.

---

# Crypto Payment Rules

Never implement direct wallet custody.

**Use hosted crypto invoice providers only:**

```
Preferred:  Coinbase Commerce (simplest compliance, stable APIs, good webhooks)
Fallback:   NOWPayments (more coin support) or BTCPay Server (self-hosted option)
```

**Supported coins:**

```
BTC   — 1 confirmation
ETH   — 12 confirmations
USDT  — TRC20 (19 confirmations) or ERC20 (12 confirmations)
USDC  — ERC20 (12 confirmations)
```

**Agent note:** The `required_confirmations` field in `crypto_invoices` allows per-coin configuration. The provider handles confirmation tracking — the webhook handler only reads the provider's status.

---

# Subscription Expiration Cron Job

The SQL functions `expire_subscriptions()` and `get_expiring_subscriptions()` already exist in Supabase but are NOT currently scheduled.

**Recommended approach (using pg_cron in Supabase):**

```sql
-- Enable pg_cron if not already enabled (via Supabase Dashboard → Extensions)
SELECT cron.schedule(
  'expire-subscriptions',
  '0 * * * *',    -- every hour
  $$SELECT expire_subscriptions()$$
);
```

**Alternative (using n8n):** Create an n8n workflow with a Cron trigger that calls:

```sql
SELECT expire_subscriptions();
```

via the Supabase Postgres connection, then calls `POST /auth/invalidate` for each expired user.

Only one scheduler may be active at a time. Implement one primary scheduler and keep the other documented as disabled fallback.

Mandatory ownership rule:

```
Primary: pg_cron.
Fallback: documented but disabled.
Never run both in parallel.
```

---

# Supabase Extensions

Ensure these are enabled (via Supabase Dashboard → Extensions):

```
pgcrypto           ← gen_random_uuid() — already in use
pg_stat_statements ← query monitoring — already in use
pg_cron            ← needed for subscription expiry cron
```

`pgjwt` is NOT needed — JWT verification happens in the FastAPI backend via PyJWKClient, not in the database.

---

# Observability Requirements

## Structured Logging

All payment handlers must log structured JSON to stdout (captured by Docker):

```python
import logging, json

logger = logging.getLogger("payments")

logger.info(json.dumps({
    "event": "checkout_created",
    "user_id": user_id,
    "plan_id": plan_id,
    "provider": "stripe",
    "session_id": session.id,
}))
```

## Metrics to Track (via audit logs table queries)

```
payment success rate:    COUNT(status='succeeded') / COUNT(*) per day
payment failures:        COUNT(status='failed') per hour
webhook processing lag:  AVG(processed_at - received_at)
checkout abandonment:    COUNT(status='pending' AND created_at < NOW() - INTERVAL '1 hour')
```

Minimum SLOs:

```
webhook processing success rate >= 99.9% (24h)
webhook p95 lag (processed_at - received_at) < 120s
payment success ratio monitored hourly with alert thresholds
```

## Alerts (implement via n8n workflows)

```
webhook signature failure → Telegram notification
payment failure spike (>3 in 5 minutes) → Telegram notification
subscription expired without renewal → email to user
```

---

# Security Requirements

## Webhook Signature Verification

Webhook signatures must be verified by provider handler selected from route path:

```python
provider_impl = get_provider(provider)
event = provider_impl.verify_webhook(raw_body=raw_body, headers=request.headers)
```

FastAPI webhook endpoints must read `await request.body()` before any JSON parsing.

## Idempotency

All payment creation endpoints must include idempotency keys at the provider client boundary:

```python
provider_impl = get_provider(provider)
provider_response = provider_impl.create_checkout(
    ...,
    idempotency_key=f"checkout_{user_id}_{request_id_uuid}",
)
```

Idempotency policy:

```
request_id_uuid is generated client-side per checkout intent and sent to backend.
Backend persists request_id_uuid on payment_transactions metadata.
If the same request_id_uuid is reused with different parameters, reject with 409.
```

Webhook handlers must check BOTH:
1. Redis fast-path: `webhook:{provider}:{event_id}` (24h TTL)
2. Database: `webhook_events` table `(provider, event_id)` UNIQUE constraint

## Secrets Management

Add provider secrets to `.env.example` with `CHANGE_ME` placeholders:

```
RAZORPAY_KEY_ID=CHANGE_ME
RAZORPAY_KEY_SECRET=CHANGE_ME
RAZORPAY_WEBHOOK_SECRET=CHANGE_ME
NOWPAYMENTS_API_KEY=CHANGE_ME
NOWPAYMENTS_WEBHOOK_SECRET=CHANGE_ME
COINBASE_COMMERCE_API_KEY=CHANGE_ME
COINBASE_COMMERCE_WEBHOOK_SECRET=CHANGE_ME
STRIPE_SECRET_KEY=CHANGE_ME
STRIPE_WEBHOOK_SECRET=CHANGE_ME
```

**NEVER** put real keys in `.env.example`. The `.gitignore` already excludes `.env` (verify this).

## Raw Body Access for Webhooks

FastAPI/Starlette requires special handling to read the raw body for webhook signature verification. The webhook endpoints MUST NOT use Pydantic models as request body — they must read raw bytes:

```python
@router.post("/api/webhooks/{provider}")
async def provider_webhook(provider: str, request: Request):
    raw_body = await request.body()           # raw bytes for signature verification
  provider_impl = get_provider(provider)
  event = provider_impl.verify_webhook(raw_body=raw_body, headers=request.headers)
    payload = json.loads(raw_body)            # parse AFTER verification
```

## Future Compatibility Requirement

System must support:

```
Razorpay (primary)
Crypto (NOWPayments/Coinbase)
Stripe (future)
```

No schema migration should be required when enabling Stripe later.

---

# Migration Requirements

All database changes must be in:

```
ai_trading_bot/db/20260315_payment_infrastructure.sql
```

The migration must:

```
✓ Be fully idempotent (re-runnable without error)
✓ Use IF NOT EXISTS for CREATE TABLE/INDEX
✓ Use guarded DO $$ blocks for ADD CONSTRAINT, CREATE TRIGGER, and CREATE POLICY
✓ Use ADD COLUMN IF NOT EXISTS for ALTER TABLE
✓ Include RLS policies for all new tables
✓ Include explicit REVOKE/GRANT table privileges aligned to service_role-only mutation design
✓ Follow existing SECURITY DEFINER + search_path conventions
✓ Include CHECK constraints for status enums
✓ Include updated_at triggers using existing update_updated_at() function
✓ NOT drop/rename any existing columns or tables
✓ NOT modify existing RLS policies
✓ Run within a single transaction (BEGIN; ... COMMIT;)
```

**Agent note:** The original `supabase_migration_v3.sql` was applied via Supabase SQL Editor, not via CLI migrations. This new file should also be applied via the SQL Editor. Document this in a comment at the top of the file.

---

# Frontend Build Configuration

## vite.config.ts CSP Update

The current CSP does not allow Stripe domains. Add to the Content-Security-Policy header in `vite.config.ts`:

```
script-src 'self' https://js.stripe.com https://challenges.cloudflare.com https://static.cloudflareinsights.com
connect-src 'self' https://*.supabase.co wss://*.supabase.co https://api.pipfactor.com http://localhost:* ws://localhost:* https://challenges.cloudflare.com https://api.stripe.com
frame-src https://js.stripe.com https://hooks.stripe.com https://challenges.cloudflare.com
```

---

# Activation Plan (Future Monetization)

When payments go live, the checklist is:

**Step 1:** Razorpay Setup (Primary)
```
Create Razorpay products/plans mapped to internal subscription_plans via provider_prices
Configure Razorpay webhook endpoint: https://api.pipfactor.com/api/webhooks/razorpay
Store Razorpay key/secret/webhook secret in backend env
```

**Step 2:** Crypto Provider Setup
```
Configure NOWPayments/Coinbase credentials
Configure webhook endpoint: https://api.pipfactor.com/api/webhooks/crypto
Map plans/prices via provider_prices
```

**Step 3:** Optional Stripe Future Setup
```
Configure Stripe credentials and webhook endpoint: /api/webhooks/stripe
Map plans/prices via provider_prices
No schema change required
```

**Step 4:** Verify
```
Run security_regression_smoke.sh
Test checkout with Razorpay (default) and Crypto button
Verify webhook received and processed (check webhook_events table)
Verify user subscription activated
Verify Redis perms cache flushed
Verify user sees "signals" permission on next request
```

**Step 5:** Enable ProtectedRoute
```
Uncomment ProtectedRoute in App.tsx (lines 79-84, 96-99)
Uncomment RequireAuth in RequireAuth.tsx (lines 26-40)
This re-enables frontend auth gating alongside backend enforcement
```

No schema migration should be required at activation time.

## Production Cutover Sequence (Mandatory)

1. Apply and verify SQL migration while PAYMENTS_ENABLED=false.
2. Deploy api-web with webhook endpoints enabled but entitlement writes gated by PAYMENTS_ENABLED.
3. Verify health endpoints and send provider test webhooks.
4. Confirm webhook signature verification, event insert, and retry behavior on forced failure.
5. Enable exactly one subscription-expiry scheduler.
6. Set PAYMENTS_ENABLED=true in backend first, then frontend.
7. Run synthetic checkout success and payment-failure scenarios.
8. Run synthetic out-of-order webhook scenario to confirm last_provider_event_time protection.
9. Run reconciliation queries between provider events, webhook_events, payment_transactions, and payment_history.
9. Declare cutover complete only if reconciliation and permission invalidation checks pass.

## Rollback Plan (Application + Provider + Scheduler)

1. Set PAYMENTS_ENABLED=false in backend and frontend.
2. Roll back api-web image/tag to prior stable release.
3. Pause or repoint provider webhook endpoints if required during incident response.
4. Disable active cron schedule temporarily.
5. Recover and replay failed/unprocessed events from webhook_events in bounded batches.
6. Reconcile subscription/payment state before re-enabling live processing.

## Webhook Failure Recovery Runbook (Required)

1. Detect unprocessed events older than alert threshold by provider.
2. Replay by provider + event_id with idempotent guardrails.
3. Replay in small batches with concurrency limits.
4. Re-run reconciliation query after each batch.
5. Escalate if backlog or failure ratio crosses on-call thresholds.

## Docker Restart and Webhook Continuity

1. Prefer rolling restart strategy for api-web.
2. Verify readiness before accepting full traffic.
3. After restart, reconcile webhook gap window to confirm no missed entitlement updates.
4. Document expected Stripe/Coinbase retry behavior and confirm retries are observed when failures are induced.

## Checkout Session Expiry Cleanup (Required)

Implement a scheduled cleanup job (pg_cron) to prevent zombie pending checkouts:

```sql
UPDATE public.payment_transactions
SET status = 'expired',
    updated_at = NOW(),
    metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('expiry_reason', 'checkout_session_timeout')
WHERE provider = 'stripe'
  AND status = 'pending'
  AND created_at < NOW() - INTERVAL '24 hours';
```

Run hourly and audit affected row count.

---

# Completion Criteria

The implementation is considered complete when:

```
✓ Migration file 20260315_payment_infrastructure.sql created and documented
✓ All new tables have RLS policies (verified via supabase_policy_check.sql)
✓ provider_customers table added
✓ provider_prices table added
✓ user_subscriptions.plan_snapshot column added
✓ Stripe-specific schema columns removed (profiles.stripe_customer_id, subscription_plans.stripe_price_id, subscription_plans.stripe_product_id)
✓ provider abstraction module created at payments/payment_providers/
✓ POST /api/payments/create-checkout endpoint works (returns checkout_url)
✓ POST /api/webhooks/{provider} endpoint works (verifies signature, stores event, processes)
✓ CSRF exempt paths updated in main.py
✓ Payment router mounted in main.py
✓ PAYMENTS_ENABLED flag implemented (backend + frontend)
✓ Pricing.tsx handleSubscribe wired to create-checkout
✓ Pricing.tsx passes provider explicitly (default razorpay)
✓ Optional "Pay with Crypto" action available
✓ Success/cancel URL handling in frontend
✓ CSP headers updated in vite.config.ts for active provider domains
✓ .env.example updated with all new vars
✓ Frontend .env.example updated
✓ vite-env.d.ts updated with new env var types
✓ Auth invalidation called after webhook subscription changes
✓ security_regression_smoke.sh still passes
✓ All new endpoints respect rate limiting
```

But:

```
PAYMENTS_ENABLED = false (both backend and frontend)
```

Therefore:

```
System remains free for all users until activation.
```

---

# Recommended Agent Skills

### 1. Supabase Agent Skills (recommended)

```
https://github.com/supabase/agent-skills
```

Skills: RLS best practices, schema migrations, query optimization.

### 2. Provider Integration Skill (recommended)

```
Provider-specific SDK/webhook skills (Razorpay/NOWPayments/Stripe)
```

Skills: checkout/order creation, webhook handling, subscription lifecycle, reconciliation.

### 3. Payment Orchestration

```
https://github.com/sentient-agi/agent-payments-skill
```

Skills: Stripe + crypto payment routing.

### 4. Security Audit

```
https://github.com/dmgrok/agent_skills_directory
```

Skills: secret scanning, security checks.

---

# Critical Gotchas for the Implementation Agent

1. **There are NO Supabase Edge Functions.** All server logic is in FastAPI (`api-web`). Do not create `supabase/functions/`.

  Clarification: Supabase Edge Functions can handle Stripe webhooks in other architectures, but this repository intentionally centralizes payment logic in FastAPI for consistency with existing auth/session/cache flows.

2. **The frontend calls Supabase RPCs directly** for subscription reads (`subscriptionService.ts`). Do NOT change this pattern. Payment mutations flow through the backend; reads can stay direct.

3. **The `create_subscription` RPC already cancels prior active subscriptions.** Do not implement duplicate cancellation logic in the webhook handler.

4. **The `record_payment` RPC already updates `user_subscriptions.last_payment_amount/date`.** Using both RPCs together is the correct flow.

5. **CSRF middleware is active for all POST endpoints except explicitly exempted paths.** Webhook endpoints MUST be added to `AUTH_CSRF_EXEMPT_PATHS`.

6. **The auth exchange flow caches permissions for 15 minutes.** After a webhook changes subscription status, you MUST invalidate `user:perms:{user_id}` in Redis AND optionally destroy sessions to force re-auth.

7. **Do not add provider-specific schema fields.** Use `provider_customers` and `provider_prices` for all provider mappings.

8. **Provider webhook endpoint must read raw bytes before parsing JSON.** FastAPI auto-parses request body — use `await request.body()` explicitly.

9. **The existing auth invalidation webhook (`POST /auth/invalidate`) uses HMAC + timestamp + replay protection.** Reuse this pattern (or its underlying functions) for crypto webhook verification.

10. **Two Redis instances exist.** Use `CACHE_REDIS` (from `redis_cache.py`) for webhook idempotency. Use `SESSION_REDIS` (from `session_store.py`) for session/perms invalidation.

11. **Frontend uses `sessionStorage` (not `localStorage`).** Sessions don't survive browser close. This is intentional — don't change it.

12. **The `ProtectedRoute` wrapper is currently DISABLED** (commented out in `App.tsx`). Payment integration does NOT need to re-enable it. That's a separate activation step.

13. **Docker rebuild required for backend changes.** After adding provider SDK dependencies, the `api-web` container must be rebuilt: `docker compose build api-web`.

14. **Cloudflare Tunnel proxies all traffic.** Webhook URLs must use the public domain: `https://api.pipfactor.com/api/webhooks/{provider}`. Do not use localhost in provider dashboard webhook config.

15. **TypeScript types in `src/types/subscription.ts` already include payment-related fields** (`payment_provider`, `external_payment_id`, etc.). Extend these types if needed but do not replace them.

16. **The `payment_history` table already exists** with extensive fields (external_payment_id, invoice_url, receipt_url, refund fields). The new `payment_transactions` table is a separate state-machine table — both coexist. `payment_transactions` tracks the payment lifecycle; `payment_history` is the permanent record written by `record_payment()` RPC.

17. **All provider CHECK constraints must allow** `razorpay|stripe|coinbase|nowpayments`.

