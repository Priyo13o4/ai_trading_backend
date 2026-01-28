# 🚀 Supabase Setup Guide - AI Trading Bot

Complete step-by-step guide to set up Supabase authentication and database for your trading platform.

---

## 📋 Prerequisites

- Supabase account (free tier works)
- Your project credentials ready
- 30 minutes of time

---

## Part 1: Initial Supabase Setup (15 minutes)

### Step 1: Create Supabase Project

1. Go to [supabase.com](https://supabase.com) and sign in
2. Click **"New Project"**
3. Fill in details:
   - **Name:** `ai-trading-bot`
   - **Database Password:** Generate strong password and **save it**
   - **Region:** Choose closest to your users
   - **Pricing:** Free (upgrade later if needed)
4. Click **"Create new project"**
5. Wait 2-3 minutes for setup

### Step 2: Get Your Credentials

After project is ready:

1. Go to **Settings → API**
2. Copy these values:

```bash
Project URL: https://xxxxx.supabase.co
anon/public key: eyJhbG... (copy this)
service_role key: eyJhbG... (copy this - KEEP SECRET!)
```

3. Save these in a safe place (you'll need them for `.env` files)

---

## Part 2: Database Setup (10 minutes)

### Step 3: Run Database Migration

1. In Supabase Dashboard, go to **SQL Editor** (left sidebar)
2. Open your `database_migration_v3.sql` file
3. **Copy ALL content** (Ctrl+A, Ctrl+C)
4. **Paste** into SQL Editor
5. Click **"Run"** (or press Ctrl+Enter)
6. Wait for **"Success. No rows returned"** message

**What this creates:**
- ✅ `subscription_plans` - 3 pricing tiers (Free 3-day trial, Basic 1 pair, Premium all pairs)
- ✅ `user_subscriptions` - User subscriptions with TTL expiration
- ✅ `payment_history` - Payment audit trail
- ✅ `signal_previews` - Main page teaser signals
- ✅ `profiles` - Minimal user data (optional, see schema doc)
- ✅ 9 smart functions for subscription management
- ✅ Automatic triggers for new user signup

### Step 4: Verify Database Setup

Run this query to check:

```sql
-- Check tables created
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public'
ORDER BY table_name;

-- Should see: payment_history, profiles, signal_previews, 
--             subscription_plans, user_subscriptions

-- Check subscription plans
SELECT name, price_usd, pairs_allowed 
FROM subscription_plans 
ORDER BY sort_order;

-- Should see 3 plans: free, basic, premium
```

---

## Part 3: Authentication Configuration (5 minutes)

### Step 5: Configure Auth Settings

1. Go to **Authentication → Settings** (left sidebar)

2. **Site URL** (where your app runs):
   ```
   Development: http://localhost:3000
   Production: https://pipfactor.com
   ```

3. **Redirect URLs** (allowed redirect after auth):
   ```
   http://localhost:3000/**
   https://pipfactor.com/**
   ```

4. **Email Auth** (should be enabled by default):
   - Confirm users: ✅ Enable email confirmations
   - Double confirm email changes: ✅ Enabled

5. Click **"Save"**

### Step 6: Customize Email Templates (Optional)

Go to **Authentication → Email Templates**:

**Confirm Signup:**
```html
<h2>Welcome to PipFactor Trading Signals!</h2>
<p>Confirm your email to start your 3-day free trial with full access:</p>
<p><a href="{{ .ConfirmationURL }}">Confirm Email</a></p>
```

**Reset Password:**
```html
<h2>Reset Your PipFactor Password</h2>
<p>Click below to reset your password:</p>
<p><a href="{{ .ConfirmationURL }}">Reset Password</a></p>
```

### Step 7: Enable OAuth Providers (Optional but Recommended!)

Go to **Authentication → Providers**:

**Enable Google Sign-In:**
1. Click **"Google"** provider
2. Toggle **"Enable Sign in with Google"**
3. Add **Client ID** and **Client Secret** from [Google Console](https://console.cloud.google.com)
4. Click **"Save"**

**Enable GitHub Sign-In:**
1. Click **"GitHub"** provider
2. Toggle **"Enable Sign in with GitHub"**
3. Add **Client ID** and **Client Secret** from [GitHub OAuth Apps](https://github.com/settings/developers)
4. Click **"Save"**

**Note:** Supabase automatically tracks OAuth users in `auth.identities` table - no extra code needed!

---

## Part 4: Environment Variables Setup

### Step 8: Configure Frontend Environment

Create/update `ai-trading_frontend/.env.development`:

```bash
# Supabase Configuration
VITE_SUPABASE_URL=https://xxxxx.supabase.co
VITE_SUPABASE_ANON_KEY=eyJhbG...your-anon-key

# API Configuration
VITE_API_BASE_URL=http://localhost:8080
```

Create `ai-trading_frontend/.env.production`:

```bash
# Production Supabase Configuration
VITE_SUPABASE_URL=https://xxxxx.supabase.co
VITE_SUPABASE_ANON_KEY=eyJhbG...your-anon-key

# Production API
VITE_API_BASE_URL=https://api.pipfactor.com
```

### Step 9: Configure Backend Environment

Create/update `ai_trading_bot/.env`:

```bash
# Supabase Configuration (NEVER commit service_role key!)
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbG...your-service-role-key
SUPABASE_ANON_KEY=eyJhbG...your-anon-key

# Database (if using direct connection)
DATABASE_URL=postgresql://postgres:[YOUR-PASSWORD]@db.xxxxx.supabase.co:5432/postgres

# Other config...
REDIS_HOST=localhost
REDIS_PORT=6379
```

**⚠️ Security Warning:**
- **NEVER** commit `.env` files to Git
- **NEVER** expose `service_role` key in frontend
- Add `.env*` to `.gitignore`

---

## Part 5: Testing the Setup (10 minutes)

### Step 10: Test Signup Flow

1. Start your frontend:
   ```bash
   cd ai-trading_frontend
   npm run dev
   ```

2. Open browser: `http://localhost:3000`

3. Click **"Sign Up"** and create test account:
   - Email: `test@example.com`
   - Password: `Test123!@#`

4. Check your email inbox for confirmation email

5. Click confirmation link

6. **Verify in Supabase Dashboard:**
   - Go to **Authentication → Users**
   - You should see your test user
   - Status should show ✅ confirmed

### Step 11: Verify Auto-Trial Creation

Run this in SQL Editor:

```sql
-- Check if trial was created automatically
SELECT 
  u.email,
  us.status,
  us.expires_at,
  sp.name as plan_name,
  EXTRACT(DAY FROM us.expires_at - NOW())::INTEGER as days_remaining
FROM auth.users u
LEFT JOIN user_subscriptions us ON u.id = us.user_id
LEFT JOIN subscription_plans sp ON us.plan_id = sp.id
WHERE u.email = 'test@example.com';
```

**Expected Result:**
- Status: `trial`
- Plan: `free`
- Days remaining: `7` (or close to it)

### Step 12: Test Login Flow

1. Log out from your app
2. Click **"Login"**
3. Enter same credentials
4. Should redirect to dashboard/signals page
5. Check browser console - no errors

### Step 13: Test OAuth (if enabled)

1. Log out
2. Click **"Sign in with Google"** (or GitHub)
3. Complete OAuth flow
4. Should create new user and redirect back

**Verify in Supabase:**
```sql
-- Check OAuth user
SELECT 
  u.email,
  u.is_sso_user,
  i.provider,
  i.provider_id
FROM auth.users u
JOIN auth.identities i ON u.id = i.user_id
WHERE u.is_sso_user = true;
```

---

## Part 6: Deploy Cron Job for Subscription Expiration

### Step 14: Deploy Edge Function

**Option A: GitHub Actions (Recommended)**

1. Copy `.github/workflows/expire-subscriptions.yml` (already created)
2. Edit line 15: Replace with your Supabase URL
3. Add GitHub Secret:
   - Go to: Repo → Settings → Secrets → Actions
   - Name: `CRON_SECRET`
   - Value: Generate random string (e.g., `cron_secret_abc123xyz`)
4. Commit and push workflow file

5. Deploy edge function:
   ```bash
   # Install Supabase CLI
   npm install -g supabase
   
   # Login
   supabase login
   
   # Link project
   supabase link --project-ref your-project-ref
   
   # Create function directory
   mkdir -p supabase/functions/expire-subscriptions
   
   # Copy function
   cp supabase_edge_function_expire_subscriptions.ts \
      supabase/functions/expire-subscriptions/index.ts
   
   # Deploy
   supabase functions deploy expire-subscriptions
   
   # Set secret (same as GitHub)
   supabase secrets set CRON_SECRET=cron_secret_abc123xyz
   ```

6. Test manually:
   ```bash
   curl -X POST https://your-project.supabase.co/functions/v1/expire-subscriptions \
     -H "Authorization: Bearer cron_secret_abc123xyz"
   ```

**Option B: cron-job.org (Easier)**

1. Go to [cron-job.org](https://cron-job.org) and sign up
2. Create new cron job:
   - **URL:** `https://your-project.supabase.co/functions/v1/expire-subscriptions`
   - **Schedule:** `0 0 * * *` (daily at midnight)
   - **Request method:** POST
   - **Add header:** `Authorization: Bearer cron_secret_abc123xyz`
3. Save and enable

---

## Part 7: Production Deployment Checklist

### Step 15: Pre-Launch Checks

- [ ] All environment variables updated with production URLs
- [ ] `.env` files added to `.gitignore`
- [ ] OAuth redirect URIs updated for production domain
- [ ] Site URL updated to production domain in Supabase
- [ ] Cron job scheduled and tested
- [ ] Database backups enabled (Supabase Dashboard → Database → Backups)
- [ ] Row Level Security policies enabled (should be by default)
- [ ] Email templates customized with production branding
- [ ] Test complete signup → trial → upgrade flow
- [ ] Payment provider webhooks configured (Stripe/Razorpay)

### Step 16: Monitor After Launch

**Daily Checks:**
```sql
-- Active users
SELECT COUNT(*) FROM auth.users WHERE deleted_at IS NULL;

-- Active subscriptions
SELECT COUNT(*) FROM user_subscriptions 
WHERE status IN ('active', 'trial') AND expires_at > NOW();

-- Failed logins (security)
SELECT COUNT(*) FROM auth.audit_log_entries 
WHERE created_at > NOW() - INTERVAL '24 hours'
AND payload->>'action' LIKE '%failed%';

-- Revenue today
SELECT SUM(amount) FROM payment_history 
WHERE status = 'succeeded' 
AND created_at > CURRENT_DATE;
```

---

## 🎯 Summary: What You Set Up

✅ Supabase project with authentication  
✅ Database with subscription management  
✅ 4 pricing tiers ready to use  
✅ Automatic 3-day trial on signup (full premium access)  
✅ Daily cron job for expiration  
✅ OAuth providers (optional)  
✅ Email templates configured  
✅ Environment variables set  

**Next Steps:**
1. Build frontend signup/login UI (use `useAuth` hook)
2. Protect API endpoints with subscription checks
3. Integrate payment provider (Stripe/Razorpay)
4. Build pricing page
5. Add subscription management UI

---

## 🆘 Troubleshooting

**Issue: Trial not created on signup**
```sql
-- Check trigger exists
SELECT * FROM pg_trigger WHERE tgname = 'on_auth_user_created';

-- If missing, re-run database migration
```

**Issue: "Invalid JWT" errors**
- Check SUPABASE_URL and ANON_KEY match in .env
- Make sure you're using anon key in frontend, service_role in backend
- Clear browser cache and cookies

**Issue: OAuth not working**
- Check redirect URIs match in Supabase and OAuth provider
- Verify Client ID/Secret are correct
- Check browser console for specific error

**Issue: Emails not sending**
- Check spam folder
- Verify email provider settings in Supabase
- Test with different email address

**Issue: Cron job not running**
- Check GitHub Actions logs
- Verify CRON_SECRET matches in both places
- Test manually with curl

---

## 📚 Related Documentation

- **SCHEMA_EXPLAINED.md** - Understand the database structure
- **BACKEND_GUIDE.md** - API endpoints and middleware
- **PRODUCTION_DEPLOYMENT.md** - Full production deployment
- **README.md** - Project overview

**You're all set! 🚀**
