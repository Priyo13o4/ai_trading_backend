# 🔧 Backend Implementation Guide

Complete guide for implementing subscription-based API access control with FastAPI and Supabase.

---

## 📋 Overview

Your FastAPI backend needs to:
1. ✅ Exchange Supabase access tokens for Redis-backed server sessions
2. ✅ Check if user has active subscription
3. ✅ Verify user can access requested trading pair
4. ✅ Handle payment webhooks
5. ✅ Update signal previews for main page

---

## Part 1: Setup & Dependencies

### Install Required Packages

```bash
cd ai_trading_bot/api
pip install supabase fastapi python-jose[cryptography] python-dotenv stripe
```

### Update `requirements.txt`:

```txt
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
supabase>=2.0.0
python-jose[cryptography]>=3.3.0
python-dotenv>=1.0.0
stripe>=7.0.0
redis>=5.0.0
pydantic>=2.0.0
```

### Environment Variables (`.env`):

```bash
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_AUDIENCE=authenticated
SUPABASE_JWKS_URL=https://your-project.supabase.co/auth/v1/.well-known/jwks.json

# Stripe (for payments)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Session Redis
SESSION_REDIS_URL=redis://:password@redis:6379/0
SERVER_SESSION_MAX_TTL=86400
PERMS_CACHE_TTL_SECONDS=900

# Session/Cookie
SESSION_COOKIE_NAME=session
CSRF_COOKIE_NAME=csrf_token
COOKIE_SECURE=1
COOKIE_SAMESITE=lax
TRUST_PROXY_HEADERS=0

# Invalidation webhook
AUTH_INVALIDATION_WEBHOOK_SECRET=replace-me
AUTH_INVALIDATION_USE_SIGNED=1
AUTH_INVALIDATION_TOLERANCE_SECONDS=300

# App
API_PORT=8080
CORS_ORIGINS=http://localhost:3000,https://pipfactor.com
```

---

## Part 2: Authentication System (`auth.py`)

**CURRENT IMPLEMENTATION:** Session auth is handled by:
- `ai_trading_bot/api-web/app/authn/routes.py` (`/auth/*` bootstrap + invalidation)
- `ai_trading_bot/api-web/app/authn/session_store.py` (Redis session/perms store)
- `ai_trading_bot/api-web/app/auth.py` (`auth_context` dependency for API routes)

### Key Components:

#### 1. **Session Bootstrap (`POST /auth/exchange`)**

`/auth/exchange` is the only place where a Supabase access token is accepted.

What happens:
1. Verifies `access_token` via Supabase JWKS (`verify_supabase_access_token`).
2. Reads/caches plan permissions in Redis.
3. Creates Redis session (`session:{sid}` + `user_sessions:{user_id}`).
4. Sets cookies:
   - `session` (HTTP-only)
   - `csrf_token` (readable; used for double-submit CSRF)
5. Returns `csrf_token`, `permissions`, and `expires_in`.

```python
from auth import auth_context

@app.get("/api/signals/{pair}")
async def get_signals(pair: str, ctx=Depends(auth_context)):
    user_id = ctx["user_id"]
    # Your endpoint logic here
    return {"signals": signals}
```

#### 2. **Primary Auth for API Routes (`auth_context`)**

`auth_context` is cookie + Redis only. It does not read `Authorization: Bearer`.

Behavior:
1. Reads `SESSION_COOKIE_NAME` from request cookies.
2. Loads session from Redis via `get_session`.
3. Returns `{user_id, plan, permissions}`.
4. Returns `401` when cookie is missing/invalid/expired.

#### 3. **CSRF Double-Submit**

Middleware enforces CSRF on cookie-authenticated state-changing requests:
- Methods: `POST`, `PUT`, `PATCH`, `DELETE`
- Requires `X-CSRF-Token` header equal to `csrf_token` cookie
- Exempt paths: `/auth/exchange`, `/auth/invalidate`

```python
from app.authn.csrf import enforce_csrf

if request.cookies.get(SESSION_COOKIE_NAME):
    enforce_csrf(request, CSRF_COOKIE_NAME)
```

#### 4. **Session Lifecycle Endpoints**

- `GET /auth/validate`: Returns `{allowed: true/false}` from session cookie validity.
- `POST /auth/logout`: Deletes current session and clears `session` + `csrf_token` cookies.
- `POST /auth/logout-all`: Requires current session, deletes all sessions for that user, clears current cookies.

#### 5. **Invalidation Webhook (`POST /auth/invalidate`)**

Purpose: internal invalidation of a user's permissions cache + all sessions.

Signed mode (default):
- `AUTH_INVALIDATION_USE_SIGNED=1`
- Required headers: `x-webhook-timestamp`, `x-webhook-signature`, `x-webhook-id`
- Signature input: `timestamp + "." + raw_body`
- Replay guard: Redis `SET NX EX` on `replay:auth_invalidate:{id}`
- Timestamp skew limit: `AUTH_INVALIDATION_TOLERANCE_SECONDS`

Legacy mode (`AUTH_INVALIDATION_USE_SIGNED=0`):
- Accepts `x-webhook-secret` and compares with `AUTH_INVALIDATION_WEBHOOK_SECRET`

#### 6. **Using auth_context in Your Endpoints**

```python
from fastapi import Depends
from auth import auth_context

# Protected endpoint (requires authentication)
@app.get("/api/protected")
async def protected_route(ctx=Depends(auth_context)):
    return {
        "user_id": ctx["user_id"],
        "plan": ctx.get("plan"),
        "permissions": ctx.get("permissions", []),
    }

# Public endpoint with optional auth
from auth import optional_auth_context

@app.get("/api/public")
async def public_route(ctx=Depends(optional_auth_context)):
    return {
        "user_id": ctx.get("user_id"),
        "plan": ctx.get("plan", "free"),
    }
```

---

## Part 2B: Operational Notes

- `SESSION_REDIS_URL` (or host/port/password fallback) must be configured or app startup fails.
- Session TTL is capped by `SERVER_SESSION_MAX_TTL` and Supabase token `exp`.
- CSRF is only enforced when a session cookie is present.

---

## Part 3: API Endpoints

### Public Endpoints (No Auth)

#### 1. **Get Preview Signals (Main Page)**

```python
@app.get("/api/preview/{pair}")
async def get_preview_signals(pair: str):
    """
    Public endpoint - shows old signals as teasers
    No authentication required
    """
    preview = supabase.table("signal_previews")\
        .select("*")\
        .eq("trading_pair", pair)\
        .eq("is_current", True)\
        .order("signal_time", desc=True)\
        .limit(1)\
        .execute()
    
    if not preview.data:
        raise HTTPException(404, "No preview available")
    
    return {
        "pair": pair,
        "signal": preview.data[0]["signal_data"],
        "timestamp": preview.data[0]["signal_time"],
        "message": "Login to see current signals"
    }
```

#### 2. **Get Subscription Plans (Pricing Page)**

```python
@app.get("/api/plans")
async def get_plans():
    """
    Public endpoint - show all pricing plans
    """
    plans = supabase.table("subscription_plans")\
        .select("*")\
        .eq("is_active", True)\
        .order("sort_order")\
        .execute()
    
    return {"plans": plans.data}
```

---

### Protected Endpoints (Auth + Subscription)

#### 3. **Get Current Signals (Main Feature)**

```python
@app.get("/api/signals/{pair}")
async def get_signals(
    pair: str,
    user_id: str = Depends(SubscriptionRequired(required_pairs=[pair]))
):
    """
    Protected endpoint - requires active subscription with pair access
    """
    # Get signals from Redis (or database)
    signals = redis_client.get(f"signals:{pair}")
    
    if not signals:
        raise HTTPException(404, "No signals available")
    
    # Track signal view (optional analytics)
    # Could increment counter in database
    
    return {
        "pair": pair,
        "signals": json.loads(signals),
        "timestamp": datetime.now().isoformat()
    }
```

#### 4. **Get Multiple Pairs**

```python
@app.get("/api/signals")
async def get_multiple_signals(
    pairs: str,  # Comma-separated: "XAUUSD,EURUSD,GBPUSD"
    ctx=Depends(auth_context)
):
    """
    Get signals for multiple pairs
    Returns only pairs user has access to
    """
    requested_pairs = pairs.split(",")
    
    # Get user subscription
    subscription = await check_subscription_access(ctx["user_id"])
    allowed_pairs = subscription["pairs_allowed"]
    
    # Filter pairs
    accessible = [p for p in requested_pairs if p in allowed_pairs]
    denied = [p for p in requested_pairs if p not in allowed_pairs]
    
    # Get signals for accessible pairs
    signals = {}
    for pair in accessible:
        data = redis_client.get(f"signals:{pair}")
        if data:
            signals[pair] = json.loads(data)
    
    return {
        "signals": signals,
        "denied_pairs": denied,
        "upgrade_message": f"Upgrade to access {', '.join(denied)}" if denied else None
    }
```

#### 5. **Get User Subscription Info**

```python
@app.get("/api/subscription")
async def get_subscription(
    ctx=Depends(auth_context)
):
    """
    Get user's current subscription details
    """
    subscription = supabase.rpc(
        "get_active_subscription",
        {"p_user_id": ctx["user_id"]}
    ).execute()
    
    if not subscription.data:
        return {
            "status": "none",
            "message": "No active subscription"
        }
    
    sub = subscription.data[0]
    return {
        "status": sub["status"],
        "plan_name": sub["plan_name"],
        "expires_at": sub["expires_at"],
        "days_remaining": sub["days_remaining"],
        "pairs_allowed": sub["pairs_allowed"],
        "is_trial": sub["is_trial"],
        "can_upgrade": True
    }
```

---

### Payment Endpoints

#### 6. **Create Checkout Session (Stripe)**

```python
import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

@app.post("/api/create-checkout")
async def create_checkout(
    plan_id: str,
    ctx=Depends(auth_context)
):
    """
    Create Stripe checkout session for subscription upgrade
    """
    # Get plan details
    plan = supabase.table("subscription_plans")\
        .select("*")\
        .eq("id", plan_id)\
        .single()\
        .execute()
    
    if not plan.data:
        raise HTTPException(404, "Plan not found")
    
    # Get user email
    user = supabase.auth.admin.get_user_by_id(ctx["user_id"])
    
    # Create Stripe checkout session
    session = stripe.checkout.Session.create(
        customer_email=user.user.email,
        payment_method_types=['card'],
        line_items=[{
            'price': plan.data['stripe_price_id'],
            'quantity': 1,
        }],
        mode='subscription',
        success_url='https://pipfactor.com/success?session_id={CHECKOUT_SESSION_ID}',
        cancel_url='https://pipfactor.com/pricing',
        metadata={
            'user_id': ctx["user_id"],
            'plan_id': plan_id
        }
    )
    
    return {
        "checkout_url": session.url,
        "session_id": session.id
    }
```

#### 7. **Stripe Webhook Handler**

```python
@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    """
    Handle Stripe webhook events
    CRITICAL: This processes payments and renews subscriptions!
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    try:
        event = stripe.Webhook.construct_event(
            payload, 
            sig_header, 
            os.getenv("STRIPE_WEBHOOK_SECRET")
        )
    except Exception as e:
        raise HTTPException(400, f"Webhook error: {str(e)}")
    
    # Handle successful payment
    if event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        subscription_id = invoice["subscription"]
        user_id = invoice["metadata"].get("user_id")
        amount = invoice["amount_paid"] / 100  # Convert cents to dollars
        
        # Record payment
        payment_id = supabase.rpc("record_payment", {
            "p_user_id": user_id,
            "p_subscription_id": subscription_id,
            "p_amount": amount,
            "p_currency": "USD",
            "p_provider": "stripe",
            "p_external_payment_id": invoice["id"],
            "p_status": "succeeded"
        }).execute()
        
        # Renew or create subscription
        existing = supabase.table("user_subscriptions")\
            .select("id")\
            .eq("external_subscription_id", subscription_id)\
            .execute()
        
        if existing.data:
            # Renew existing subscription
            supabase.rpc("renew_subscription", {
                "p_subscription_id": existing.data[0]["id"]
            }).execute()
        else:
            # Create new subscription (first payment)
            plan_id = invoice["metadata"].get("plan_id")
            supabase.rpc("create_subscription", {
                "p_user_id": user_id,
                "p_plan_id": plan_id,
                "p_payment_provider": "stripe",
                "p_external_id": subscription_id,
                "p_trial_days": 0
            }).execute()
    
    # Handle failed payment
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        user_id = invoice["metadata"].get("user_id")
        
        # Record failed payment
        supabase.rpc("record_payment", {
            "p_user_id": user_id,
            "p_subscription_id": invoice["subscription"],
            "p_amount": invoice["amount_due"] / 100,
            "p_currency": "USD",
            "p_provider": "stripe",
            "p_external_payment_id": invoice["id"],
            "p_status": "failed"
        }).execute()
        
        # Update subscription status to past_due
        supabase.table("user_subscriptions")\
            .update({"status": "past_due"})\
            .eq("external_subscription_id", invoice["subscription"])\
            .execute()
        
        # TODO: Send email notification
    
    return {"success": True}
```

---

### Admin Endpoints

#### 8. **Update Signal Previews**

```python
@app.post("/api/admin/update-preview")
async def update_preview(
    pair: str,
    signal_data: dict,
    admin_key: str
):
    """
    Update preview signal for main page
    Called by your signal generator service
    """
    # Verify admin key
    if admin_key != os.getenv("ADMIN_API_KEY"):
        raise HTTPException(401, "Unauthorized")
    
    # Update preview
    supabase.rpc("update_signal_preview", {
        "p_trading_pair": pair,
        "p_signal_data": signal_data
    }).execute()
    
    return {"success": True, "pair": pair}
```

---

## Part 4: Main Application Setup (`main.py`)

```python
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI(
    title="AI Trading Bot API",
    version="3.0.0",
    description="Subscription-based trading signals API"
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import routes
from auth import auth_context

# Health check
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "3.0.0"}

# Include all endpoint routes here...
# (Copy endpoints from Part 3 above)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("API_PORT", 8080)),
        reload=True
    )
```

---

## Part 5: Error Handling

### Custom Error Responses

```python
from fastapi import HTTPException
from fastapi.responses import JSONResponse

# Subscription errors
def subscription_error(error_type: str, message: str, **kwargs):
    """
    Standardized error format for subscription issues
    """
    return JSONResponse(
        status_code=403,
        content={
            "error": error_type,
            "message": message,
            "action": "upgrade",  # What user should do
            **kwargs
        }
    )

# Example usage in middleware:
if not subscription:
    return subscription_error(
        "no_subscription",
        "You don't have an active subscription",
        upgrade_url="/pricing"
    )

if pair not in allowed_pairs:
    return subscription_error(
        "pair_not_allowed",
        f"Your plan doesn't include {pair}",
        current_plan=plan_name,
        allowed_pairs=allowed_pairs,
        upgrade_url="/pricing"
    )
```

---

## Part 6: Testing

### Test Authentication

```bash
# 1) Exchange Supabase access token for session + csrf cookies
ACCESS_TOKEN="eyJhbG..."
curl -i -c cookies.txt -X POST http://localhost:8080/auth/exchange \
    -H "Content-Type: application/json" \
    -d '{"access_token":"'"$ACCESS_TOKEN"'"}'

# 2) Call protected endpoint with cookie session
curl -b cookies.txt http://localhost:8080/api/signals/XAUUSD

# Expected: 200 OK with signals (if subscribed)
# Or: 403 Forbidden (if not subscribed)
```

### Test Subscription Check

```python
# test_subscription.py
import requests

BASE_URL = "http://localhost:8080"
cookies = {"session": "your-session-cookie-value"}

# Test allowed pair
response = requests.get(f"{BASE_URL}/api/signals/XAUUSD", cookies=cookies)
print(f"XAUUSD: {response.status_code}")  # Should be 200

# Test denied pair (if on free plan)
response = requests.get(f"{BASE_URL}/api/signals/EURUSD", cookies=cookies)
print(f"EURUSD: {response.status_code}")  # Should be 403

# Get subscription info
response = requests.get(f"{BASE_URL}/api/subscription", cookies=cookies)
print(response.json())
```

### Test CSRF (state-changing route)

```bash
# Read csrf_token value from /auth/exchange response or cookie jar.
CSRF_TOKEN="from_exchange_response"

curl -i -b cookies.txt -X POST http://localhost:8080/auth/logout \
    -H "X-CSRF-Token: $CSRF_TOKEN"
```

---

## Part 7: Deployment

### Run Locally

```bash
cd ai_trading_bot/api
python -m uvicorn app.main:app --reload --port 8080
```

### Run with Docker

```bash
docker-compose up api
```

### Production with Gunicorn

```bash
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8080
```

---

## 🎯 Summary

**Endpoints Implemented:**
- ✅ Public: Preview signals, pricing plans
- ✅ Protected: Current signals (with pair check)
- ✅ Auth: Subscription info, user profile
- ✅ Payment: Checkout, webhooks
- ✅ Admin: Update previews

**Security Features:**
- ✅ Supabase token verification at `/auth/exchange` only
- ✅ Redis-backed server sessions for API auth
- ✅ CSRF double-submit for cookie-authenticated writes
- ✅ Subscription validation
- ✅ Pair-specific access control
- ✅ Signed invalidation webhook verification (default)
- ✅ Rate limiting ready (add if needed)

**Next Steps:**
1. Test all endpoints
2. Set up Stripe webhooks
3. Deploy to production
4. Monitor error rates
5. Add analytics

---

## 📚 Related Docs

- **SCHEMA_EXPLAINED.md** - Database structure
- **SUPABASE_SETUP.md** - Initial setup
- **PRODUCTION_DEPLOYMENT.md** - Deploy to production
