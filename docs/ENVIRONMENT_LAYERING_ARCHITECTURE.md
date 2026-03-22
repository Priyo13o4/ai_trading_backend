# Environment Layering Architecture: Development, Staging, Production

**Date:** March 2026  
**Purpose:** Comprehensive guide to implementing and maintaining separate development, staging, and production environments with proper cookie authentication and security isolation.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [The Problem We Encountered](#the-problem-we-encountered)
3. [The Solution: Layered Segregation](#the-solution-layered-segregation)
4. [Architecture Overview](#architecture-overview)
5. [Current Cloudflare Configuration](#current-cloudflare-configuration)
6. [Required Changes](#required-changes)
7. [Implementation Guide](#implementation-guide)
8. [CI/CD Pipeline Integration](#cicd-pipeline-integration)
9. [Testing & Validation](#testing--validation)
10. [Troubleshooting](#troubleshooting)

---

## Executive Summary

### The Challenge
When localhost frontend (http://localhost:3000) tried to authenticate against production API (https://api.pipfactor.com), browser cookie policies blocked the session cookie due to **cross-site restrictions**.

### The Solution
Create **three isolated environments** with matching domain pairs:
- **Development:** localhost:3000 ↔ localhost:8000 (HTTP)
- **Staging:** app.dev ↔ api.dev (HTTPS with self-signed certs)
- **Production:** pipfactor.com ↔ api.pipfactor.com (HTTPS, real domains)

### Key Insight
Each environment is **self-contained**. Frontend and backend always talk *within the same layer*, preventing cross-site cookie issues.

---

## The Problem We Encountered

### What Went Wrong

You were testing authentication locally with this topology:

```
❌ BLOCKED SETUP
┌─────────────────────────┐
│ Frontend                │
│ http://localhost:3000   │
└────────────┬────────────┘
             │ POST /auth/exchange
             ▼
┌─────────────────────────────────────┐
│ API Backend                         │
│ https://api.pipfactor.com           │
│ (Production domain)                 │
└─────────────────────────────────────┘

Problem:
- Frontend origin: http://localhost:3000
- Backend origin: https://api.pipfactor.com
- Cookie set with Domain=pipfactor.com, SameSite=Lax
- Browser policy: "These are different sites, block cookie"
- Result: Login succeeds (session created), but validation fails with 401 missing_sid
```

### Root Cause: SameSite Cookie Policy

SameSite=Lax cookies are rejected when:
- Different domains (localhost ≠ api.pipfactor.com)
- Different protocols (http ≠ https)
- Different ports (3000 ≠ 5000)

Even though both are "your" domains, the browser sees them as **cross-site** and blocks the cookie.

### Why This Only Affects Development

| Environment | Frontend | Backend | Cookie Domain | Works? |
|-------------|----------|---------|---------------|--------|
| Production | pipfactor.com | api.pipfactor.com | pipfactor.com | ✅ YES (same site: *.pipfactor.com) |
| Development (Old) | localhost:3000 | api.pipfactor.com | pipfactor.com | ❌ NO (different sites) |
| Development (Fixed) | localhost:3000 | localhost:8000 | localhost | ✅ YES (same site) |

---

## The Solution: Layered Segregation

### Why Three Layers?

1. **Development (localhost)**: Fast iteration, minimal setup, your machine only
2. **Staging (*.dev)**: Team testing, mimics production, HTTPS validation
3. **Production**: Real users, real security, real performance

### Core Principle

```
┌─────────────────────────────────────────────────────────────┐
│ Each environment is completely self-contained               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ DEVELOPMENT          STAGING          PRODUCTION            │
│ ─────────────────────────────────────────────────          │
│ Frontend: localhost   app.dev         pipfactor.com         │
│ Backend: localhost    api.dev         api.pipfactor.com     │
│ Database: local       staging db      prod db               │
│ Cookies: localhost    *.dev           *.pipfactor.com       │
│ HTTPS: No             Yes             Yes                   │
│ Test Data: Fake       Test            Real                  │
│                                                             │
│ Key: Frontend always talks to Backend in same layer         │
│      No cross-site communication                            │
│      Cookies work because origins match                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Architecture Overview

### Network Topology per Environment

#### DEVELOPMENT: Your Local Machine

```
┌────────────────────────────────────────┐
│ Your Computer (macOS)                  │
├────────────────────────────────────────┤
│                                        │
│  ┌──────────────────────────────────┐ │
│  │ Frontend Dev Server              │ │
│  │ http://localhost:3000            │ │
│  │ (React/Vite dev server)          │ │
│  │ 🔄 Hot reload enabled            │ │
│  └────────────┬─────────────────────┘ │
│               │ XHR/fetch             │
│  ┌────────────▼─────────────────────┐ │
│  │ Backend API Server               │ │
│  │ http://localhost:8000            │ │
│  │ (FastAPI --reload)               │ │
│  │ 🔄 Auto restart on code change   │ │
│  └────────────┬─────────────────────┘ │
│               │ TCP connection        │
│  ┌────────────▼─────────────────────┐ │
│  │ PostgreSQL Database              │ │
│  │ localhost:5432                   │ │
│  │ (Docker container)               │ │
│  └──────────────────────────────────┘ │
│                                        │
└────────────────────────────────────────┘

How to start:
$ docker-compose -f docker-compose.dev.yml up
$ npm run dev  (in separate terminal)
```

#### STAGING: Team/CI Environment (Behind VPN)

```
┌──────────────────────────────────────────────────┐
│ Staging Server / CI Environment                  │
│ (Behind company VPN, e.g., 10.0.0.50)           │
├──────────────────────────────────────────────────┤
│                                                  │
│ Internet + /etc/hosts override                  │
│ ────────────────────────────     ─────────────  │
│ 127.0.0.1 app.dev               Frontend layer │
│ 127.0.0.1 api.dev                              │
│                                                  │
│  ┌─────────────────────────────────────────┐   │
│  │ Frontend (HTTPS)                        │   │
│  │ https://app.dev                         │   │
│  │ (React SPA served by Nginx)             │   │
│  │ 📦 Pre-built, optimized                 │   │
│  └────────────┬────────────────────────────┘   │
│               │ HTTPS XHR request              │
│  ┌────────────▼────────────────────────────┐   │
│  │ Backend API (HTTPS)                     │   │
│  │ https://api.dev                         │   │
│  │ (FastAPI behind Nginx)                  │   │
│  │ 🔒 Self-signed certs (*.dev)            │   │
│  └────────────┬────────────────────────────┘   │
│               │ TCP connection                 │
│  ┌────────────▼────────────────────────────┐   │
│  │ PostgreSQL Database                     │   │
│  │ db.dev:5432                             │   │
│  │ (Docker container)                      │   │
│  └─────────────────────────────────────────┘   │
│                                                  │
└──────────────────────────────────────────────────┘

How to deploy:
- GitHub Actions runs: docker-compose -f docker-compose.staging.yml up
- Tests against staging environment
- Team accesses via: https://app.dev (with /etc/hosts override)
```

#### PRODUCTION: Live Users

```
┌────────────────────────────────────────────────┐
│ Production Infrastructure (Cloudflare + AWS)   │
├────────────────────────────────────────────────┤
│                                                │
│  ┌──────────────────────────────────────────┐ │
│  │ Frontend (HTTPS/CDN)                     │ │
│  │ https://pipfactor.com                    │ │
│  │ https://www.pipfactor.com                │ │
│  │                                          │ │
│  │ Cloudflare Edge (Global CDN)             │ │
│  │ ✅ Automatic SSL certificates           │ │
│  │ ✅ DDoS protection                       │ │
│  │ ✅ WAF enabled                           │ │
│  │ ✅ Bot management (Turnstile)            │ │
│  └──────────────┬───────────────────────────┘ │
│                 │ HTTPS XHR request           │
│  ┌──────────────▼───────────────────────────┐ │
│  │ API Backend (HTTPS)                      │ │
│  │ https://api.pipfactor.com                │ │
│  │                                          │ │
│  │ Cloudflare Tunnel                        │ │
│  │ (Connects internal FastAPI to CF Edge)   │ │
│  │ ✅ Zero-trust networking                 │ │
│  │ ✅ Origin certificate validation         │ │
│  └──────────────┬───────────────────────────┘ │
│                 │ TCP connection              │
│  ┌──────────────▼───────────────────────────┐ │
│  │ PostgreSQL Database (RDS)                │ │
│  │ Encrypted, Multi-AZ, Backups             │ │
│  └──────────────────────────────────────────┘ │
│                                                │
└────────────────────────────────────────────────┘

DNS:
- pipfactor.com → Cloudflare (CNAME to tunnel)
- www.pipfactor.com → Cloudflare (CNAME to tunnel)
- api.pipfactor.com → Cloudflare (CNAME to tunnel)
- sse.pipfactor.com → Cloudflare (CNAME to tunnel)
```

---

## Current Cloudflare Configuration

### DNS Records (Active)

```
Zone: pipfactor.com

1. CNAME | pipfactor.com             | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied
2. CNAME | www.pipfactor.com         | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied
3. CNAME | api.pipfactor.com         | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied
4. CNAME | sse.pipfactor.com         | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied
5. CNAME | cdn.pipfactor.com         | public.r2.dev                                          | ✅ Proxied
6. CNAME | mt5.pipfactor.com         | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied
7. CNAME | n8n.pipfactor.com         | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied

Email (Zoho):
8. MX   | pipfactor.com              | mx.zoho.in        | Priority: 10
9. MX   | pipfactor.com              | mx2.zoho.in       | Priority: 20
10. MX  | pipfactor.com              | mx3.zoho.in       | Priority: 30
11. TXT | pipfactor.com              | v=spf1 include:zoho.in ~all
12. TXT | zmail._domainkey.pipfactor.com | DKIM keys...

NS Records (for reference):
13. NS  | pipfactor.com              | dns1.registrar-servers.com  (old)
14. NS  | pipfactor.com              | dns2.registrar-servers.com  (old)
```

### Turnstile Configuration (Current)

```
Widget Name: "pipfactor"

Hostnames Configured (5):
- api.pipfactor.com      ✅ Correct
- localhost              ❌ Should be removed (causes issues)
- pipfactor.com          ✅ Correct
- sse.pipfactor.com      ✅ Correct
- www.pipfactor.com      ✅ Correct

Widget Mode: Managed (Recommended) ✅
- Cloudflare decides verification method based on traffic risk
- Most visitors: non-interactive or invisible check
- High-risk visitors: additional challenges

Bot Fight Mode: (Check your settings)
```

**⚠️ ACTION ITEM:** Remove `localhost` from Hostnames (you mentioned you'll do this)

### WAF/Security Rules (Current)

```
Custom Security Rule #1:
Name: "Allow API & SSE (Skip Bot Checks)"
Matches: Hostname is in (api.pipfactor.com, sse.pipfactor.com)
Action: Skip (all bot management)
Events: 3,44k in last 24h (ACTIVE)

Why this exists:
- SSE (Server-Sent Events) connections would be interrupted by Turnstile challenges
- API requests need fast processing, not challenged
- This is correct for production

Rate Limiting Rules: ❌ Not configured
- Recommendation: Add rules for /auth endpoints (5 attempts per 15 min)
- (Optional, only if you see abuse)

Managed Rules: ❌ Not activated
- Available with Pro/Business plan
- Not critical for now
```

---

## Required Changes

### 1. DNS Records: Add Staging Domains

Add these **2 new CNAME records** to `pipfactor.com` zone:

```
CNAME | api.dev    | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied
CNAME | app.dev    | c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com | ✅ Proxied
```

These route staging traffic through the **same Cloudflare tunnel** as production.

### 2. Turnstile: Create 3 Separate Widgets

#### Widget 1: Development (localhost)

```
Name: "PipFactor Dev (localhost)"
Hostnames: localhost
Widget Mode: Managed
Bot Fight Mode: Off/Relaxed (faster for dev)
Purpose: Testing CAPTCHA locally
```

Store the sitekey as: `VITE_TURNSTILE_SITEKEY_DEV`

#### Widget 2: Staging (app.dev)

```
Name: "PipFactor Staging (app.dev)"
Hostnames: app.dev
Widget Mode: Managed
Bot Fight Mode: On (match production)
Purpose: Full integration testing before production
```

Store the sitekey as: `VITE_TURNSTILE_SITEKEY_STAGING`

#### Widget 3: Production (pipfactor.com)

```
Name: "PipFactor Production"
Hostnames: 
  - pipfactor.com
  - www.pipfactor.com
  - (Remove "localhost")
Widget Mode: Managed
Bot Fight Mode: On
Purpose: Protect real users
```

Store the sitekey as: `VITE_TURNSTILE_SITEKEY_PROD` (already in use)

### 3. WAF Rules: Keep Existing, Consider Adding Rate Limiting

**Current rule is good, keep it:**
```
"Allow API & SSE (Skip Bot Checks)" ✅
- Prevents SSE interruption
- Allows fast API responses
```

**Optional but recommended:**
```
Create rate limiting rules for auth endpoints:

Name: "Auth Endpoint Rate Limit"
Request: Path matches /auth/exchange
Rate limiting: 5 requests per 15 minutes per IP
Action: Challenge (Turnstile)
Purpose: Prevent brute-force login attempts
```

---

## Implementation Guide

### Phase 1: Update Turnstile Configuration

**Time: ~20 minutes**

1. **Remove localhost from existing widget:**
   - Go to Cloudflare Dashboard → Turnstile
   - Edit "pipfactor" widget
   - Remove "localhost" from Hostnames
   - Save

2. **Create new development widget:**
   - New Widget → "PipFactor Dev (localhost)"
   - Add hostname: `localhost`
   - Mode: Managed
   - Get the sitekey (something like: 1x00000000000000000000AA)

3. **Create new staging widget:**
   - New Widget → "PipFactor Staging (app.dev)"
   - Add hostname: `app.dev`
   - Mode: Managed
   - Get the sitekey

### Phase 2: Update DNS Records

**Time: ~5 minutes**

1. Go to Cloudflare Dashboard → Domains → pipfactor.com
2. Add DNS records:
   ```
   Type: CNAME
   Name: api.dev
   Content: c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com
   TTL: Automatic
   Proxy: On
   
   Type: CNAME
   Name: app.dev
   Content: c956f821-686f-4405-9580-9d75db14a5dc.cfargotunnel.com
   TTL: Automatic
   Proxy: On
   ```

### Phase 3: Update Environment Files

**File: `ai-trading_frontend/.env.development`**

```bash
VITE_ENVIRONMENT=development
VITE_TURNSTILE_SITEKEY=<dev-widget-sitekey>
VITE_API_URL=http://localhost:8000
```

**File: `ai-trading_frontend/.env.staging`**

```bash
VITE_ENVIRONMENT=staging
VITE_TURNSTILE_SITEKEY=<staging-widget-sitekey>
VITE_API_URL=https://api.dev
```

**File: `ai-trading_frontend/.env.production`**

```bash
VITE_ENVIRONMENT=production
VITE_TURNSTILE_SITEKEY=<prod-widget-sitekey>
VITE_API_URL=https://api.pipfactor.com
```

**File: `ai_trading_bot/.env.example`**

```bash
ENVIRONMENT=development  # or staging, production
API_URL=http://localhost:8000
FRONTEND_URL=http://localhost:3000
```

### Phase 4: Update Frontend Turnstile Component

**File: `ai-trading_frontend/src/components/Turnstile.tsx` (new or updated)**

```typescript
import { useEffect } from 'react';

interface TurnstileProps {
  onSuccess?: (token: string) => void;
  onError?: () => void;
  onExpire?: () => void;
}

export const Turnstile: React.FC<TurnstileProps> = ({
  onSuccess,
  onError,
  onExpire,
}) => {
  useEffect(() => {
    // Get environment-specific sitekey
    const sitekey = import.meta.env.VITE_TURNSTILE_SITEKEY;
    
    if (!sitekey) {
      console.error('Turnstile sitekey not configured');
      return;
    }

    // Initialize widget
    window.turnstile?.render('#turnstile-container', {
      sitekey,
      theme: 'light',
      callback: onSuccess,
      'error-callback': onError,
      'expired-callback': onExpire,
    });

    return () => {
      window.turnstile?.reset();
    };
  }, [onSuccess, onError, onExpire]);

  return <div id="turnstile-container" />;
};
```

### Phase 5: Update Backend Cookie Logic

**File: `ai_trading_bot/api-web/app/main.py`**

```python
import os

def get_cookie_config():
    """Return environment-specific cookie settings."""
    env = os.getenv("ENVIRONMENT", "development")
    
    if env == "production":
        return {
            "domain": "pipfactor.com",
            "samesite": "strict",
            "secure": True,
            "httponly": True,
        }
    elif env == "staging":
        return {
            "domain": ".dev",
            "samesite": "lax",
            "secure": True,  # Self-signed certs work with Secure=True
            "httponly": True,
        }
    else:  # development
        return {
            "domain": "localhost",
            "samesite": "lax",
            "secure": False,  # HTTP allowed in dev
            "httponly": True,
        }

# In your auth endpoints:
@app.post("/auth/exchange")
async def auth_exchange(email: str, password: str, response: Response):
    # ... validate credentials ...
    
    session_token = create_session(user_id=user.id)
    cookie_config = get_cookie_config()
    
    response.set_cookie(
        key="sid",
        value=session_token,
        **cookie_config
    )
    
    return {"user_id": user.id, "email": user.email}
```

---

## CI/CD Pipeline Integration

### GitHub Actions: Three Branches, Three Environments

**File: `.github/workflows/deploy.yml`**

```yaml
name: Build, Test, Deploy

on:
  push:
    branches: [main, develop, feature/*]
  pull_request:
    branches: [main, develop]

jobs:
  # ════════════════════════════════════════════════════════════════
  # TEST DEVELOPMENT: Run on all PRs and develop branch
  # ════════════════════════════════════════════════════════════════
  test-development:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request' || github.ref == 'refs/heads/develop'
    
    services:
      postgres:
        image: postgres:15-alpine
        env:
          POSTGRES_DB: trading_test
          POSTGRES_USER: test
          POSTGRES_PASSWORD: testpass
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432

    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install backend dependencies
        run: |
          cd ai_trading_bot
          pip install -r api-web/requirements.txt
      
      - name: Test backend (development)
        env:
          ENVIRONMENT: development
          DATABASE_URL: postgresql://test:testpass@localhost:5432/trading_test
        run: |
          cd ai_trading_bot
          pytest tests/ -v
      
      - name: Set up Node
        uses: actions/setup-node@v3
        with:
          node-version: '18'
      
      - name: Install frontend dependencies
        run: |
          cd ai-trading_frontend
          npm install
      
      - name: Test frontend (development)
        env:
          VITE_ENVIRONMENT: development
        run: |
          cd ai-trading_frontend
          npm run test --if-present
      
      - name: Build frontend (development)
        env:
          VITE_ENVIRONMENT: development
        run: |
          cd ai-trading_frontend
          npm run build

  # ════════════════════════════════════════════════════════════════
  # DEPLOY STAGING: Push to staging environment
  # ════════════════════════════════════════════════════════════════
  deploy-staging:
    runs-on: ubuntu-latest
    needs: test-development
    if: github.ref == 'refs/heads/develop'
    
    steps:
      - uses: actions/checkout@v3
      
      # Generate self-signed certificates for *.dev
      - name: Generate staging certificates
        run: |
          mkdir -p ai_trading_bot/certs
          openssl req -x509 -newkey rsa:4096 \
            -keyout ai_trading_bot/certs/key.pem \
            -out ai_trading_bot/certs/cert.pem \
            -days 365 -nodes -subj "/CN=*.dev"
      
      # Build and deploy backend
      - name: Build backend Docker image (staging)
        run: |
          cd ai_trading_bot
          docker build -t pipfactor-api:staging .
      
      - name: Build frontend Docker image (staging)
        env:
          VITE_ENVIRONMENT: staging
        run: |
          cd ai-trading_frontend
          docker build -t pipfactor-frontend:staging .
      
      - name: Deploy to staging (via Docker Compose)
        run: |
          cd ai_trading_bot
          docker-compose -f docker-compose.staging.yml up -d

  # ════════════════════════════════════════════════════════════════
  # DEPLOY PRODUCTION: Push to production (manual approval)
  # ════════════════════════════════════════════════════════════════
  deploy-production:
    runs-on: ubuntu-latest
    needs: test-development
    if: github.ref == 'refs/heads/main'
    environment:
      name: production
      url: https://pipfactor.com
    
    steps:
      - uses: actions/checkout@v3
      
      - name: Build backend Docker image (production)
        run: |
          cd ai_trading_bot
          docker build -t pipfactor-api:latest .
      
      - name: Build frontend Docker image (production)
        env:
          VITE_ENVIRONMENT: production
        run: |
          cd ai-trading_frontend
          docker build -t pipfactor-frontend:latest .
      
      - name: Deploy to production
        run: |
          echo "Deploying to production..."
          # Your production deployment command here
          # (Cloudflare Tunnel, Docker registry push, etc.)
```

---

## Testing & Validation

### Development Environment Validation

**Test 1: Login Flow**

```bash
# Terminal 1: Start backend
cd ai_trading_bot
docker-compose -f docker-compose.dev.yml up

# Terminal 2: Start frontend
cd ai-trading_frontend
npm run dev

# Browser: Go to http://localhost:3000
# - Click login
# - Enter credentials
# - Check browser DevTools → Application → Cookies
#   Should see: sid=...; Domain=localhost; Secure=0
# - Verify you're logged in
```

**Test 2: Page Navigation**

```
- Navigate between authenticated pages
- Check DevTools Console for errors
- Verify SSE connections work (no disconnects)
```

### Staging Environment Validation

**Prerequisites:**
```bash
# Add /etc/hosts entries
sudo nano /etc/hosts
# 127.0.0.1 app.dev
# 127.0.0.1 api.dev
```

**Test 1: Access via HTTPS**

```bash
# Start staging docker
docker-compose -f docker-compose.staging.yml up

# Browser: Go to https://app.dev
# - Accept self-signed certificate warning
# - Login with test credentials
# - Check DevTools → Cookies
#   Should see: sid=...; Domain=.dev; Secure=1
```

**Test 2: Turnstile Challenge**

```
- Clear cookies for app.dev
- Try logging in
- Turnstile widget should appear (staging-specific sitekey)
- After challenge, login should succeed
```

### Production Validation

```bash
# No local testing needed
# When deployed to pipfactor.com:

# Test 1: Login at https://pipfactor.com
# - Should see Turnstile (production sitekey)
# - Cookies: Domain=pipfactor.com; Secure=1

# Test 2: Monitor Cloudflare dashboard
# - Check WAF logs for any false positives
# - Check Turnstile analytics for blocked bots
```

---

## Troubleshooting

### Problem: "Cookies not being sent in requests"

**Diagnosis:**
```bash
curl -v -X POST http://localhost:8000/auth/exchange \
  -d '{"email": "test@example.com", "password": "pass"}' \
  -H "Content-Type: application/json"

# Look for: Set-Cookie header in response
# Check Domain, SameSite, Secure flags
```

**Solutions:**
- ✅ Check `ENVIRONMENT` variable is set correctly
- ✅ Verify frontend and backend are on **same domain/port combo**
- ✅ In development: frontend must be `localhost:3000`, backend `localhost:8000`
- ✅ In staging: use `app.dev` and `api.dev`

### Problem: "Self-signed certificate errors in staging"

**Symptoms:**
```
curl: (60) SSL certificate problem: self-signed certificate
```

**Solution:**
```bash
# For testing only (NOT production):
curl -k https://api.dev/auth/validate  # -k ignores cert errors

# In production code, regenerate certs:
openssl req -x509 -newkey rsa:4096 \
  -keyout ai_trading_bot/certs/key.pem \
  -out ai_trading_bot/certs/cert.pem \
  -days 365 -nodes -subj "/CN=*.dev"
```

### Problem: "CORS error when calling API"

**Example Error:**
```
Access to XMLHttpRequest at 'https://api.dev' blocked by CORS policy
```

**Cause:**
- Frontend origin: `https://app.dev`
- Backend origin: `https://api.dev`
- If CORS not configured, requests blocked

**Solution:**
```python
# In backend (FastAPI):
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # dev
        "https://app.dev",        # staging
        "https://pipfactor.com",  # prod
        "https://www.pipfactor.com"
    ],
    allow_credentials=True,  # ← Allow cookies!
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### Problem: "Turnstile widget not appearing"

**Check:**
```javascript
// In browser console:
console.log(window.turnstile);  // Should not be undefined
console.log(import.meta.env.VITE_TURNSTILE_SITEKEY);  // Should have value
```

**Solution:**
1. Verify sitekey is in `.env` file
2. Verify Turnstile script is loaded: `<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>`
3. Verify hostname matches Turnstile widget configuration

### Problem: "localhost not removed from Turnstile"

**Error:**
```
Turnstile widget failing for production domain
```

**Why:**
- Cloudflare might be smart-matching to wrong widget
- If `localhost` still in production widget, conflicts arise

**Fix:**
1. Go to Cloudflare Turnstile
2. Edit "pipfactor" widget
3. Remove `localhost` from Hostnames list
4. Save
5. Verify only production domains remain

---

## Environment Variables Quick Reference

### Frontend Configuration

```bash
# Development (.env.development)
VITE_ENVIRONMENT=development
VITE_TURNSTILE_SITEKEY=<dev-sitekey>
VITE_API_URL=http://localhost:8000

# Staging (.env.staging)
VITE_ENVIRONMENT=staging
VITE_TURNSTILE_SITEKEY=<staging-sitekey>
VITE_API_URL=https://api.dev

# Production (.env.production)
VITE_ENVIRONMENT=production
VITE_TURNSTILE_SITEKEY=<prod-sitekey>
VITE_API_URL=https://api.pipfactor.com
```

### Backend Configuration

```bash
# .env file (or docker-compose env section)

# Development
ENVIRONMENT=development
DATABASE_URL=postgresql://dev:devpass@postgres:5432/trading_dev
API_URL=http://localhost:8000
FRONTEND_URL=http://localhost:3000

# Staging
ENVIRONMENT=staging
DATABASE_URL=postgresql://staging:stagingpass@postgres:5433/trading_staging
API_URL=https://api.dev
FRONTEND_URL=https://app.dev

# Production
ENVIRONMENT=production
DATABASE_URL=postgresql://user:pass@prod-db.rds.amazonaws.com:5432/trading
API_URL=https://api.pipfactor.com
FRONTEND_URL=https://pipfactor.com
```

---

## Implementation Checklist

### Cloudflare Configuration

- [ ] Remove `localhost` from existing Turnstile widget
- [ ] Create new Turnstile widget for development (localhost)
- [ ] Create new Turnstile widget for staging (app.dev)
- [ ] Keep/verify production Turnstile widget
- [ ] Add DNS `api.dev` CNAME record
- [ ] Add DNS `app.dev` CNAME record
- [ ] Verify WAF rule for API/SSE (skip bot checks) exists
- [ ] (Optional) Add rate limiting rule for /auth endpoints

### Code Changes

- [ ] Create `.env.staging` file in `ai-trading_frontend`
- [ ] Update `ai-trading_frontend/.env.development` with dev sitekey
- [ ] Update `ai-trading_frontend/.env.production` with prod sitekey
- [ ] Update backend `get_cookie_config()` function in `main.py`
- [ ] Update Turnstile component to use environment-specific sitekey
- [ ] Update CORS config to allow all three domains
- [ ] Update `.env.example` with new variables

### Docker Configuration

- [ ] Create `/ai_trading_bot/certs/` directory (for staging)
- [ ] Generate self-signed certificates for *.dev
- [ ] Verify `docker-compose.staging.yml` exists
- [ ] Update `docker-compose.staging.yml` with certificate paths
- [ ] Test local startup: `docker-compose -f docker-compose.dev.yml up`

### CI/CD Pipeline

- [ ] Create `.github/workflows/deploy.yml` (three environments)
- [ ] Set GitHub environment: `production` with manual approval
- [ ] Test PR workflow triggers staging deployment
- [ ] Test main branch deployment requires approval

### Documentation & Knowledge

- [ ] Share this document with your team
- [ ] Document any custom DNS records (e.g., for email)
- [ ] Create runbook for troubleshooting
- [ ] Update README with environment setup instructions

---

## FAQ

### Q: Why use *.dev instead of localhost for staging?

**A:** 
- `.dev` is HTTPS-only, closer to production (https vs http)
- Allows team members to test via VPN (localhost only works locally)
- Catches HTTPS-specific bugs before production
- Can be shared with non-technical stakeholders for testing

### Q: Can I use staging environment with my own domain?

**A:**
Yes, but it's more complex:
- You'd need: `app.staging.yourcompany.com` and `api.staging.yourcompany.com`
- Both would need real SSL certs (more expensive)
- *.dev is free with self-signed certs and provides same benefits

### Q: What if I forget to switch environments?

**A:**
Scenarios:
- **Dev frontend → Prod backend:** Cookies fail (cross-site), login breaks
- **Prod frontend → Dev backend:** Cookies fail, login breaks
- **Staging frontend → Prod backend:** Cookies fail (*.dev ≠ *.pipfactor.com)

**Protection:**
- Add environment badges to UI (`[DEV]`, `[STAGING]`, `[PROD]`)
- CI/CD prevents mismatches by using specific `.env` per deployment
- Test suite validates correct API endpoint is configured

### Q: Do I need all three environments?

**Minimum setup:** Development + Production (2 layers)
- Development: localhost localhost (your machine)
- Production: pipfactor.com api.pipfactor.com (real users)

**Recommended:** All three (3 layers)
- Staging catches bugs before production
- Team can test without local setup
- Safer deployment process

### Q: How do I switch environments locally?

```bash
# To test different environments locally:

# Development
VITE_ENVIRONMENT=development npm run dev

# Staging (requires /etc/hosts override + docker-compose staging)
VITE_ENVIRONMENT=staging npm run dev -- --host app.dev

# Production (not recommended locally, but possible)
VITE_ENVIRONMENT=production npm run dev
```

---

## References

**Related Documentation:**
- `../BACKEND_GUIDE.md` - Backend API setup
- `../DEPLOYMENT_GUIDE.md` - Deployment procedures
- `.github/workflows/deploy.yml` - CI/CD pipeline

**External Resources:**
- [MDN: SameSite Cookie Attribute](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie/SameSite)
- [Cloudflare Tunnel Documentation](https://developers.cloudflare.com/cloudflare-one/connections/connect-applications/)
- [Cloudflare Turnstile Documentation](https://developers.cloudflare.com/turnstile/)
- [Docker Compose Best Practices](https://docs.docker.com/compose/production/)

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | March 2026 | Initial documentation: 3-layer architecture, Turnstile, WAF, DNS configuration |

---

**Last Updated:** March 21, 2026  
**Maintained By:** Development Team  
**Questions?** Refer to troubleshooting section or create an issue on GitHub

