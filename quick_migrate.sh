#!/bin/bash
# Quick Migration Runner - Run this script to execute all migrations
# Usage: SUPABASE_DB_PASSWORD=... ./quick_migrate.sh

set -e

PROJECT_DIR="/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/ai_trading_bot"
cd "$PROJECT_DIR"

echo "=========================================="
echo "Supabase Migration Quick Runner"
echo "=========================================="
echo ""

# Reject CLI password argument to avoid leaking secrets in shell history/process list.
if [ -n "$1" ]; then
    echo "❌ ERROR: Passing password as a CLI argument is not supported for security reasons."
    echo ""
    echo "Use one of these methods instead:"
    echo "  1) export SUPABASE_DB_PASSWORD='your_password'"
    echo "  2) Run without env var and enter password at the hidden prompt"
    echo ""
    exit 1
fi

PASSWORD="${SUPABASE_DB_PASSWORD:-${PGPASSWORD:-}}"

if [ -z "$PASSWORD" ]; then
    echo "Enter Supabase database password (input hidden):"
    read -r -s PASSWORD
    echo ""
fi

if [ -z "$PASSWORD" ]; then
    echo "❌ ERROR: Database password is required."
    exit 1
fi

PGHOST="aws-0-us-east-1.pooler.supabase.com"
PGPORT="5432"
PGDATABASE="postgres"
PGUSER="postgres.lqhlsqsbvamjrxprbapp"
PSQL="/opt/homebrew/opt/libpq/bin/psql"

export PGPASSWORD="$PASSWORD"

echo "🔄 Executing migrations in order..."
echo ""

# Migration 1
echo "📝 Migration 1: Adding webhook lease columns..."
if $PSQL -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f db/20260323_webhook_lease_columns.sql > /tmp/migration1.log 2>&1; then
    echo "✅ Migration 1: SUCCESS"
else
    echo "❌ Migration 1: FAILED (check /tmp/migration1.log)"
    cat /tmp/migration1.log
    exit 1
fi

echo ""

# Migration 2
echo "📝 Migration 2: Creating claim_ready_webhooks RPC..."
if $PSQL -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f db/20260323_claim_ready_webhooks_rpc.sql > /tmp/migration2.log 2>&1; then
    echo "✅ Migration 2: SUCCESS"
else
    echo "❌ Migration 2: FAILED (check /tmp/migration2.log)"
    cat /tmp/migration2.log
    exit 1
fi

echo ""

# Migration 3
echo "📝 Migration 3: Creating active checkout index..."
if $PSQL -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -f db/20260323_active_checkout_index.sql > /tmp/migration3.log 2>&1; then
    echo "✅ Migration 3: SUCCESS"
else
    echo "❌ Migration 3: FAILED (check /tmp/migration3.log)"
    cat /tmp/migration3.log
    exit 1
fi

echo ""
echo "=========================================="
echo "✅ ALL MIGRATIONS COMPLETED SUCCESSFULLY!"
echo "=========================================="
echo ""
echo "🔍 Running verification queries..."
echo ""

# Verify columns
echo "Checking webhook_events columns..."
$PSQL -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'webhook_events' AND column_name IN ('processing', 'processing_started_at', 'retry_count', 'next_retry_at', 'last_error') ORDER BY column_name;"

echo ""

# Verify function
echo "Checking claim_ready_webhooks function..."
$PSQL -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -c "SELECT proname, pg_get_function_arguments(oid) as arguments FROM pg_proc WHERE proname = 'claim_ready_webhooks';"

echo ""

# Verify index
echo "Checking uq_active_payment_per_user index..."
$PSQL -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE -c "SELECT indexname FROM pg_indexes WHERE indexname = 'uq_active_payment_per_user';"

echo ""
echo "=========================================="
echo "✅ VERIFICATION COMPLETE"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Test webhook claiming functionality"
echo "2. Test payment double-click protection"
echo "3. Monitor for any issues"
echo ""

unset PGPASSWORD
