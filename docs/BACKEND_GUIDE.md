# 🔧 Backend Implementation Guide

Complete guide for implementing subscription-based API access control with FastAPI and Supabase.

---

## 📋 Overview

Your FastAPI backend needs to:
1. ✅ Verify JWT tokens from Supabase
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
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_JWT_SECRET=your-jwt-secret  # From Settings → API → JWT Secret

# Stripe (for payments)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# App
API_PORT=8080
CORS_ORIGINS=http://localhost:3000,https://pipfactor.com
```

---

## Part 2: Authentication System (`auth.py`)

**CURRENT IMPLEMENTATION:** The authentication is handled by `ai_trading_bot/api/app/auth.py` using an `auth_context` dependency.

### Key Components:

#### 1. **JWT Verification (Supabase Tokens)**

The system verifies Supabase JWT tokens using multiple methods:
- **JWKS** (JSON Web Key Set) - Primary method
- **HS256 shared secret** - Fallback method
- **Supabase user endpoint** - Final fallback

```python
from auth import auth_context

@app.get("/api/signals/{pair}")
async def get_signals(pair: str, ctx=Depends(auth_context)):
    if ctx["anonymous"]:
        raise HTTPException(402, "Login required")
    
    user_id = ctx["user_id"]
    # Your endpoint logic here
    return {"signals": signals}
```

#### 2. **Anonymous Access with Rate Limiting**

The system supports anonymous access with limits:
- Free tier: 3 requests per day (configurable via `ANON_FREE_LIMIT`)
- Uses HMAC-signed cookies for tracking
- Automatically creates anon tokens on first visit

```python
async def get_public_data(ctx=Depends(auth_context)):
    if ctx["anonymous"]:
        # Anonymous user - limited access
        anon_jti = ctx["anon_jti"]
    else:
        # Authenticated user - full access
        user_id = ctx["user_id"]
```

#### 3. **How auth_context Works**

**What it does:**
1. Checks for `Authorization: Bearer <token>` header
2. Verifies JWT token with Supabase (JWKS → HS256 → /user endpoint)
3. Returns `{"anonymous": False, "user_id": "..."}` for authenticated users
4. Falls back to anonymous cookie tracking for unauthenticated users
5. Returns `{"anonymous": True, "anon_jti": "..."}` for anon users
6. Creates new anon cookie if none exists
7. Increments Redis counter for anon usage
8. Raises `HTTPException(402)` if anon limit exceeded

#### 4. **Environment Variables Required**

```bash
# Supabase Auth
SUPABASE_JWKS_URL=https://your-project.supabase.co/auth/v1/.well-known/jwks.json
SUPABASE_JWT_SECRET=your-jwt-secret  # HS256 fallback
SUPABASE_PROJECT_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_AUDIENCE=authenticated

# Anonymous Access
ANON_HMAC_SECRET=change-me  # For signing anon cookies
ANON_COOKIE_NAME=anon_pass
ANON_FREE_LIMIT=3  # Free requests per day
RATE_LIMIT_PER_MIN=60

# Redis (for anon tracking)
REDIS_HOST=n8n-redis
REDIS_PORT=6379
REDIS_PASSWORD=your-redis-password
```

#### 5. **Using auth_context in Your Endpoints**

```python
from fastapi import Depends
from auth import auth_context

# Protected endpoint (requires authentication)
@app.get("/api/protected")
async def protected_route(ctx=Depends(auth_context)):
    if ctx["anonymous"]:
        raise HTTPException(401, "Authentication required")
    return {"user_id": ctx["user_id"]}

# Public endpoint with optional auth
@app.get("/api/public")
async def public_route(ctx=Depends(auth_context)):
    if ctx["anonymous"]:
        return {"message": "Anonymous access", "limited": True}
    return {"user_id": ctx["user_id"], "limited": False}
```

---

## Part 2B: Alternative - Subscription Middleware (Optional)

**NOTE:** The guide below describes `subscription_middleware.py` which can be used **alongside** `auth_context` for subscription-based access control. This is optional and not currently implemented in the main API.

If you want to add subscription checking, you can create `subscription_middleware.py`:

```python
from fastapi import Depends, HTTPException
from auth import auth_context
# Add subscription checking logic here
```

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
    user_id: str = Depends(get_user_from_token)
):
    """
    Get signals for multiple pairs
    Returns only pairs user has access to
    """
    requested_pairs = pairs.split(",")
    
    # Get user subscription
    subscription = await check_subscription_access(user_id)
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
    user_id: str = Depends(get_user_from_token)
):
    """
    Get user's current subscription details
    """
    subscription = supabase.rpc(
        "get_active_subscription",
        {"p_user_id": user_id}
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
    user_id: str = Depends(get_user_from_token)
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
    user = supabase.auth.admin.get_user_by_id(user_id)
    
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
            'user_id': user_id,
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
from subscription_middleware import (
    SubscriptionRequired, 
    get_user_from_token,
    check_subscription_access
)

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
# Get JWT token (from frontend login)
TOKEN="eyJhbG..."

# Test protected endpoint
curl -H "Authorization: Bearer $TOKEN" \
     http://localhost:8080/api/signals/XAUUSD

# Expected: 200 OK with signals (if subscribed)
# Or: 403 Forbidden (if not subscribed)
```

### Test Subscription Check

```python
# test_subscription.py
import requests

BASE_URL = "http://localhost:8080"
TOKEN = "your-jwt-token"

headers = {"Authorization": f"Bearer {TOKEN}"}

# Test allowed pair
response = requests.get(f"{BASE_URL}/api/signals/XAUUSD", headers=headers)
print(f"XAUUSD: {response.status_code}")  # Should be 200

# Test denied pair (if on free plan)
response = requests.get(f"{BASE_URL}/api/signals/EURUSD", headers=headers)
print(f"EURUSD: {response.status_code}")  # Should be 403

# Get subscription info
response = requests.get(f"{BASE_URL}/api/subscription", headers=headers)
print(response.json())
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
- ✅ JWT verification
- ✅ Subscription validation
- ✅ Pair-specific access control
- ✅ Webhook signature verification
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
