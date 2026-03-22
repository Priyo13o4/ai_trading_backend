# SYSTEM CONTEXT & GOAL
You are an expert full-stack engineer tasked with implementing a provider-agnostic payment gateway for PipFactor, an AI-powered strategy generator for forex markets.
You have access to the Razorpay MCP and Supabase MCP.
Your goal is to implement Razorpay (fiat) and NOWPayments (crypto) using a "sleeper architecture" where all payment code is deployed but inactive by default behind feature flags.

# STRICT ARCHITECTURAL CONSTRAINTS
* Do not create or use Supabase Edge Functions. All backend logic must reside in the FastAPI application (`api-web` service).
* Maintain the exact existing database schema and authentication flows.
* Do not implement direct wallet custody for crypto; rely solely on NOWPayments hosted invoices.
* Use the Razorpay Standard Checkout integration (JavaScript modal) on the frontend, not the Quick Integration auto-generated HTML button.
* Never use Redis as the payment source of truth.

# STEP 1: DATABASE MIGRATION (Supabase MCP)
* Create a single, idempotent migration file named `20260315_payment_infrastructure.sql`.
* Remove provider-specific schema fields from the existing `profiles` and `subscription_plans` tables.
* Create `provider_customers` and `provider_prices` tables for provider mappings.
* Create `payment_transactions` as the unified state machine for all payment attempts.
* Create `webhook_events` for idempotent event logging before processing.
* Create `crypto_invoices` for tracking blockchain-specific fields like network confirmations.
* Create `payment_audit_logs` to record all state transitions.
* Ensure all new tables have Row Level Security (RLS) enabled and grant mutation privileges strictly to the `service_role`.

# STEP 2: BACKEND IMPLEMENTATION (FastAPI)
* Create a provider abstraction layer in `api-web/app/payments/payment_providers/`.
* Implement `base.py` defining the `PaymentProvider` interface.
* Implement `razorpay_provider.py` using the Razorpay SDK to generate orders and verify HMAC SHA-256 webhooks.
* Implement `nowpayments_provider.py` using HTTPX to call the NOWPayments API (`https://api.nowpayments.io/v1/invoice`) and verify HMAC SHA-512 webhooks (ensure JSON keys are sorted before hashing).
* Create a generic `POST /api/payments/create-checkout` endpoint protected by existing cookie session auth and the `PAYMENTS_ENABLED` feature flag.
* Create a generic `POST /api/webhooks/{provider}` endpoint. 
* Explicitly add `/api/webhooks/razorpay` and `/api/webhooks/nowpayments` to `AUTH_CSRF_EXEMPT_PATHS` in `main.py`.
* Route all webhook processing through the `webhook_events` table first, then update `payment_transactions`, then call the existing `record_payment` and `create_subscription` Supabase RPCs.
* Invalidate the user permissions cache in the `SESSION_REDIS` instance (`user:perms:{user_id}`) immediately after a successful webhook update.

# STEP 3: FRONTEND IMPLEMENTATION (React/Vite)
* Wrap all payment UI components and checkout functions in a `VITE_PAYMENTS_ENABLED` environment variable check.
* Update the `ApiService` class to include a `createCheckout` method hitting the new FastAPI endpoint.
* Implement the Razorpay Standard Checkout JS integration in `Pricing.tsx` to open the modal upon successful backend order creation.
* Redirect users to the NOWPayments `checkout_url` when the crypto option is selected.
* Handle `?payment=success` and `?payment=cancelled` URL parameters on the Profile and Pricing pages to trigger frontend state refreshes.
* Update `vite.config.ts` Content-Security-Policy to explicitly allow Razorpay (`https://js.razorpay.com`) and NOWPayments domains.

# STEP 4: INFRASTRUCTURE CONFIGURATION
* Provide me with the exact Cloudflare WAF rules needed to whitelist Razorpay IP subnets (e.g., `52.66.111.41`) and NOWPayments IP subnets (`130.162.59.88`) so webhooks can bypass Bot Fight Mode.
