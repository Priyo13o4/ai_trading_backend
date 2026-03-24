# Supabase Migration Guide

## Overview
This guide will help you run 3 SQL migration files against your Supabase database in the correct order.

## Prerequisites
- Supabase CLI installed ✓ (version 2.75.0)
- PostgreSQL client (psql) installed ✓ (version 18.3)
- Access to your Supabase project dashboard

## Migration Files (Must Run in This Order)

### Migration 1: Add Webhook Lease Columns
**File:** `db/20260323_webhook_lease_columns.sql`

**What it does:**
- Adds 5 new columns to the `webhook_events` table:
  - `processing` (BOOLEAN) - Indicates if a worker has claimed this event
  - `processing_started_at` (TIMESTAMPTZ) - When processing started (for lease expiry)
  - `retry_count` (INTEGER) - Number of processing attempts
  - `next_retry_at` (TIMESTAMPTZ) - When the event can be retried (exponential backoff)
  - `last_error` (TEXT) - Error message from last failed attempt
- Creates index `idx_webhook_events_ready_queue` for efficient claim queries
- Adds comments for documentation

**Why it's first:** The RPC function (Migration 2) depends on these columns.

### Migration 2: Create Webhook Claiming RPC Function
**File:** `db/20260323_claim_ready_webhooks_rpc.sql`

**What it does:**
- Creates the `claim_ready_webhooks()` RPC function
- Implements atomic webhook claiming with SKIP LOCKED
- Supports lease-based crash recovery
- Restricts access to service_role only for security

**Why it's second:** It depends on the columns created in Migration 1.

### Migration 3: Create Active Checkout Index
**File:** `db/20260323_active_checkout_index.sql`

**What it does:**
- Creates partial unique index `uq_active_payment_per_user`
- Prevents users from having multiple active checkouts
- Protects against double-click scenarios that could lead to duplicate charges
- Only enforces uniqueness for statuses: 'created', 'pending', 'processing'

**Why it's last:** Independent of the webhook migrations, but should be applied last.

## Method 1: Using Python Script (Recommended)

### Step 1: Get Your Supabase Database Password

1. Go to your Supabase Dashboard:
   https://lqhlsqsbvamjrxprbapp.supabase.co/project/lqhlsqsbvamjrxprbapp/settings/database

2. Scroll to "Connection string" section

3. Select "URI" tab

4. Click "Show" to reveal the password in the connection string:
   ```
   postgresql://postgres.lqhlsqsbvamjrxprbapp:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```

5. Copy just the password part (the text after `]:` and before `@`)

### Step 2: Run the Migration Script

```bash
cd "/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/ai_trading_bot"

# Run the script (it will prompt for password)
python3 run_migrations_psql.py

# OR use an environment variable (safer for automation)
export SUPABASE_DB_PASSWORD="YOUR_PASSWORD_HERE"
python3 run_migrations_psql.py
```

Security note:
- Do not pass database passwords as command-line arguments (they can leak via shell history and process lists).

### Step 3: Verify Results

The script will:
- Execute each migration in order
- Show success/failure for each
- Run verification queries to confirm changes
- Display a summary at the end

## Method 2: Using Supabase SQL Editor (Alternative)

If the Python script has issues, you can run migrations manually:

### Step 1: Open Supabase SQL Editor
Go to: https://lqhlsqsbvamjrxprbapp.supabase.co/project/lqhlsqsbvamjrxprbapp/sql/new

### Step 2: Run Migration 1

1. Open `db/20260323_webhook_lease_columns.sql`
2. Copy the entire file content
3. Paste into the SQL Editor
4. Click "Run"
5. Verify: Check that webhook_events table has the new columns

### Step 3: Run Migration 2

1. Open `db/20260323_claim_ready_webhooks_rpc.sql`
2. Copy the entire file content
3. Paste into the SQL Editor
4. Click "Run"
5. Verify: Check Database > Functions for `claim_ready_webhooks`

### Step 4: Run Migration 3

1. Open `db/20260323_active_checkout_index.sql`
2. Copy the entire file content
3. Paste into the SQL Editor
4. Click "Run"
5. Verify: Check that the index was created

## Method 3: Using psql Command Line

If you have the database password:

```bash
cd "/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/ai_trading_bot"

# Set your password (get it from Supabase Dashboard)
export PGPASSWORD="YOUR_PASSWORD_HERE"

# Connection details
PGHOST="aws-0-us-east-1.pooler.supabase.com"
PGPORT="5432"
PGDATABASE="postgres"
PGUSER="postgres.lqhlsqsbvamjrxprbapp"

# Run migrations in order
/opt/homebrew/opt/libpq/bin/psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f db/20260323_webhook_lease_columns.sql

/opt/homebrew/opt/libpq/bin/psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f db/20260323_claim_ready_webhooks_rpc.sql

/opt/homebrew/opt/libpq/bin/psql -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f db/20260323_active_checkout_index.sql

# Optional cleanup for interactive shells
unset PGPASSWORD
```

## Method 4: API-Based Migration Script (Service Role)

Use this method only when you need to execute SQL through Supabase REST RPC.

```bash
cd "/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/ai_trading_bot"

export SUPABASE_URL="https://your-project.supabase.co"
python3 run_migrations.py
```

Security note:
- Never commit keys/tokens to tracked files.

## Verification Queries

After running migrations, you can verify them with these SQL queries in the Supabase SQL Editor:

### Verify Migration 1 (Webhook Columns)
```sql
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
AND table_name = 'webhook_events'
AND column_name IN ('processing', 'processing_started_at', 'retry_count', 'next_retry_at', 'last_error')
ORDER BY column_name;
```

Expected: Should show 5 rows with the new columns.

### Verify Migration 2 (RPC Function)
```sql
SELECT
    proname as function_name,
    pg_get_function_arguments(oid) as arguments,
    prokind
FROM pg_proc
WHERE proname = 'claim_ready_webhooks'
AND pronamespace = 'public'::regnamespace;
```

Expected: Should show the function with arguments (batch_size INTEGER DEFAULT 50, lease_seconds INTEGER DEFAULT 300).

### Verify Migration 3 (Unique Index)
```sql
SELECT
    indexname,
    indexdef
FROM pg_indexes
WHERE schemaname = 'public'
AND indexname = 'uq_active_payment_per_user';
```

Expected: Should show the index with its definition including the WHERE clause.

## Troubleshooting

### "Column already exists" Error
This is OK! The migrations are idempotent (safe to run multiple times). The `IF NOT EXISTS` clauses prevent errors if objects already exist.

### "Function already exists" Error
This is also OK! The `CREATE OR REPLACE FUNCTION` will update the existing function.

### Connection Timeout
- Check your internet connection
- Verify the Supabase project is active
- Ensure you're using the correct password

### Permission Denied
- Make sure you're using the database password, not the service role key
- Verify your Supabase account has admin access to the project

### Security Best Practices
- Use secure prompt input or environment variables for secrets.
- Avoid plaintext secrets in scripts, docs, and shell history.
- Rotate any key immediately if it was ever committed.

## Important Notes

1. **Idempotency**: All migrations are safe to run multiple times. If an object already exists, it will be skipped or replaced.

2. **Order Matters**: You MUST run the migrations in order:
   1. webhook_lease_columns.sql (adds columns)
   2. claim_ready_webhooks_rpc.sql (uses those columns)
   3. active_checkout_index.sql (independent)

3. **Backup**: While these migrations are non-destructive (they only ADD things), it's always good practice to backup your database before applying migrations.

4. **Testing**: After applying migrations, test the webhook claiming functionality to ensure it works as expected.

## Files Created

- `/path/to/project/run_migrations_psql.py` - Python script for automated migration
- `/path/to/project/run_migrations.sh` - Bash script for manual migration
- `/path/to/project/MIGRATION_GUIDE.md` - This guide

## Success Criteria

After successful migration, you should have:
- ✓ 5 new columns in webhook_events table
- ✓ 1 new index on webhook_events (idx_webhook_events_ready_queue)
- ✓ 1 new RPC function (claim_ready_webhooks)
- ✓ 1 new unique index on payment_transactions (uq_active_payment_per_user)

## Next Steps

1. Update your application code to use the new `claim_ready_webhooks()` function
2. Test webhook processing with the new claiming mechanism
3. Monitor for any lease expiry issues (adjust lease_seconds if needed)
4. Test payment checkout flow to ensure double-click protection works

## Support

If you encounter issues:
1. Check the Supabase logs in Dashboard > Logs
2. Verify your database password is correct
3. Try running migrations manually via SQL Editor
4. Check that your Supabase project is on a paid plan if using advanced features
