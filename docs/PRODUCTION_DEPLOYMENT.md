# 🚀 Production Deployment Guide

Complete deployment guide for your AI Trading Bot with subscription system.

---

## 📋 Overview

This guide covers:
1. ✅ Supabase setup (already done if you followed SUPABASE_SETUP.md)
2. ✅ Environment configuration
3. ✅ Docker deployment
4. ✅ Cloudflare Tunnels (or VPS/Nginx alternative)
5. ✅ Subscription cron job setup
6. ✅ Payment provider configuration
7. ✅ Monitoring & backups

---

## Part 1: Prerequisites

### Must Complete First:
1. **SUPABASE_SETUP.md** - Database and auth configuration
2. **BACKEND_GUIDE.md** - Backend implementation

### What You Need:
- ✅ Supabase project with database migration run
- ✅ Domain name (e.g., pipfactor.com)
- ✅ Server or VPS (DigitalOcean, Vultr, AWS, etc.)
- ✅ Stripe account (for payments)
- ✅ Cloudflare account (free tier works)

---

## Part 2: Environment Configuration

### Frontend Environment (`.env.production`)

Create in `ai-trading_frontend/`:

```bash
# API
VITE_API_BASE_URL=https://api.pipfactor.com

# Supabase
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your-anon-key-here

# Optional: Analytics
VITE_GA_TRACKING_ID=G-XXXXXXXXXX
```

### Backend Environment (`.env`)

Create in `ai_trading_bot/`:

```bash
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_JWT_SECRET=your-jwt-secret

# Stripe Payments
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PUBLISHABLE_KEY=pk_live_...

# Redis
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_PASSWORD=your-redis-password

# API Configuration
API_PORT=8080
ENVIRONMENT=production
CORS_ORIGINS=https://pipfactor.com,https://www.pipfactor.com
ADMIN_API_KEY=your-random-secret-for-admin-endpoints

# Rate Limiting
RATE_LIMIT_PER_MINUTE=60

# Logging
LOG_LEVEL=info
SENTRY_DSN=https://...  # Optional error tracking
```

### Docker Compose Environment

Update `ai_trading_bot/.env`:

```bash
# Same as backend .env.production
# Plus database credentials if needed
POSTGRES_PASSWORD=your-postgres-password
```


---

## Part 3: Build for Production

### Frontend Build

```bash
cd ai-trading_frontend

# Install dependencies
npm install

# Build for production
npm run build

# Output will be in dist/ folder
ls dist/
```

### Backend Build (Docker)

```bash
cd ai_trading_bot

# Build Docker images
docker-compose build

# Test locally first
docker-compose up
```

---

## Part 4: Deployment Options

### Option A: Cloudflare Tunnels (Recommended - Zero Config!)

**Why Cloudflare Tunnels?**
- ✅ No firewall configuration needed
- ✅ Free SSL certificates (automatic)
- ✅ Built-in DDoS protection
- ✅ CDN caching included
- ✅ Works behind NAT/firewalls
- ✅ Easy subdomain management
- ✅ Zero server management

**Architecture:**
```
pipfactor.com → Cloudflare Tunnel → Your Server:3000 (Frontend)
api.pipfactor.com → Cloudflare Tunnel → Your Server:8080 (API)
n8n.pipfactor.com → Cloudflare Tunnel → Your Server:5678 (N8N)
```

**Step 1: Install cloudflared**

```bash
# macOS
brew install cloudflared

# Linux
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
sudo mv cloudflared-linux-amd64 /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
```

**Step 2: Create Tunnels**

```bash
# Login to Cloudflare
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create pipfactor

# Get tunnel ID
cloudflared tunnel list
# Copy the UUID (e.g., a1b2c3d4-e5f6-...)
```

**Step 3: Configure DNS in Cloudflare Dashboard**

Go to Cloudflare DNS settings and add:

| Type | Name | Target |
|------|------|--------|
| CNAME | pipfactor.com | `<tunnel-id>.cfargotunnel.com` |
| CNAME | www | `<tunnel-id>.cfargotunnel.com` |
| CNAME | api | `<tunnel-id>.cfargotunnel.com` |
| CNAME | n8n | `<tunnel-id>.cfargotunnel.com` |

**Step 4: Create Tunnel Configuration**

Create `~/.cloudflared/config.yml` (or edit your existing file) and fill in the tunnel ID + credentials path.

```yaml
tunnel: <your-tunnel-id>
credentials-file: /home/your-user/.cloudflared/<your-tunnel-id>.json

ingress:
  # Frontend
  - hostname: pipfactor.com
    # Vite dev server: 5173 (recommended for local testing)
    # Static build via `serve -s dist -l 3000`: use 3000 instead
    service: http://localhost:5173
  - hostname: www.pipfactor.com
    service: http://localhost:5173
  
  # API
  - hostname: api.pipfactor.com
    service: http://localhost:8080
  
  # N8N (optional)
  - hostname: n8n.pipfactor.com
    service: http://localhost:5678
  
  # Catch-all (required)
  - service: http_status:404
```

**Step 5: Start Services**

```bash
# Terminal 1: Backend
cd ai_trading_bot
docker-compose up -d

# Terminal 2: Frontend
cd ai-trading_frontend
npm install -g serve
serve -s dist -l 3000

# Terminal 3: Cloudflare Tunnel
cloudflared tunnel run pipfactor
```

**Step 6: Run Tunnel as Service (Production)**

```bash
# Install as system service
sudo cloudflared service install

# Start service
sudo systemctl start cloudflared
sudo systemctl enable cloudflared  # Auto-start on boot

# Check status
sudo systemctl status cloudflared
```

---

### Option B: Traditional VPS with Nginx

If you prefer traditional reverse proxy:

**Install Nginx:**

```bash
sudo apt update
sudo apt install nginx certbot python3-certbot-nginx
```

**Configure Nginx (`/etc/nginx/sites-available/pipfactor`):**

```nginx
# Frontend
server {
    listen 80;
    server_name pipfactor.com www.pipfactor.com;
    
    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}

# API
server {
    listen 80;
    server_name api.pipfactor.com;
    
    location / {
        proxy_pass http://localhost:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# N8N
server {
    listen 80;
    server_name n8n.pipfactor.com;
    
    location / {
        proxy_pass http://localhost:5678;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
```

**Enable Site & SSL:**

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/pipfactor /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Get SSL certificates
sudo certbot --nginx -d pipfactor.com -d www.pipfactor.com
sudo certbot --nginx -d api.pipfactor.com
sudo certbot --nginx -d n8n.pipfactor.com

# Auto-renewal (already set up by certbot)
sudo systemctl status certbot.timer
```

---

## Part 5: Subscription Expiration Cron Job

**CRITICAL:** Your subscription system needs daily automation to expire old subscriptions.

### Option A: GitHub Actions (Recommended - Free!)

Create `.github/workflows/expire-subscriptions.yml`:

```yaml
name: Expire Subscriptions Daily

on:
  schedule:
    - cron: '0 0 * * *'  # Midnight UTC daily
  workflow_dispatch:  # Allow manual trigger

jobs:
  expire:
    runs-on: ubuntu-latest
    steps:
      - name: Call Supabase Edge Function
        run: |
          curl -X POST \
            https://your-project.supabase.co/functions/v1/expire-subscriptions \
            -H "Authorization: Bearer ${{ secrets.CRON_SECRET }}" \
            -H "Content-Type: application/json"
```

**Setup:**
1. Go to GitHub repo → Settings → Secrets and variables → Actions
2. Add secret: `CRON_SECRET` (use any random string)
3. Deploy edge function (see below)
4. Commit workflow file
5. Test manually: Actions tab → Expire Subscriptions Daily → Run workflow

### Deploy Supabase Edge Function

```bash
# Install Supabase CLI
npm install -g supabase

# Login
supabase login

# Link to your project
supabase link --project-ref your-project-ref

# Create function directory
mkdir -p supabase/functions/expire-subscriptions

# Copy function code
# (Use code from supabase_edge_function_expire_subscriptions.ts)
cat > supabase/functions/expire-subscriptions/index.ts << 'EOF'
// Copy contents from supabase_edge_function_expire_subscriptions.ts
EOF

# Deploy
supabase functions deploy expire-subscriptions

# Set secret
supabase secrets set CRON_SECRET=your-random-secret-key
```

### Option B: External Cron Service

**cron-job.org (Free):**
1. Sign up at https://cron-job.org
2. Create new cron job:
   - URL: `https://your-project.supabase.co/functions/v1/expire-subscriptions`
   - Schedule: Daily at midnight
   - HTTP Headers: Add `Authorization: Bearer your-secret-key`
3. Enable job

**EasyCron (Free tier):**
1. Sign up at https://www.easycron.com
2. Add cron expression: `0 0 * * *`
3. Set URL and authorization header
4. Enable

### Option C: Server Crontab

If you have shell access to server:

```bash
# Edit crontab
crontab -e

# Add this line
0 0 * * * curl -X POST https://your-project.supabase.co/functions/v1/expire-subscriptions -H "Authorization: Bearer your-secret-key"
```

### Testing Cron Job

```bash
# Test manually
curl -X POST https://your-project.supabase.co/functions/v1/expire-subscriptions \
  -H "Authorization: Bearer your-secret-key"

# Expected response:
# {
#   "success": true,
#   "expired_count": 3,
#   "cancelled_count": 1,
#   "processed_at": "2025-01-15T00:00:00Z"
# }
```

---

## Part 6: Payment Provider Setup

### Stripe Configuration

**1. Create Products in Stripe Dashboard:**

```bash
# Go to https://dashboard.stripe.com/products
# Create products matching your subscription_plans:

# Basic Plan
- Name: Basic Trading Signals (1 Pair + News)
- Price: $4.99/month
- Billing: Recurring monthly
- Copy Price ID: price_xxx...

# Premium Plan
- Name: Premium Trading Signals (All Pairs + News)
- Price: $14.99/month
- Billing: Recurring monthly
- Copy Price ID: price_yyy...
```

**2. Update Database with Stripe Price IDs:**

```sql
-- Run in Supabase SQL Editor
UPDATE subscription_plans 
SET stripe_price_id = 'price_xxx...' 
WHERE name = 'basic';

UPDATE subscription_plans 
SET stripe_price_id = 'price_yyy...' 
WHERE name = 'premium';
```

**3. Configure Webhooks:**

Go to https://dashboard.stripe.com/webhooks

- **Add endpoint:** `https://api.pipfactor.com/api/webhooks/stripe`
- **Events to listen for:**
  - `invoice.payment_succeeded`
  - `invoice.payment_failed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
- **Copy webhook signing secret:** `whsec_...`
- **Add to environment:** `STRIPE_WEBHOOK_SECRET=whsec_...`

**4. Test Webhook:**

```bash
# Install Stripe CLI
brew install stripe/stripe-cli/stripe

# Login
stripe login

# Forward webhooks to local API (for testing)
stripe listen --forward-to localhost:8080/api/webhooks/stripe

# Trigger test payment
stripe trigger invoice.payment_succeeded
```

---

## Part 7: Monitoring & Maintenance
