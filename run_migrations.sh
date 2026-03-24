#!/bin/bash

# Script to run Supabase migrations
# This script executes 3 SQL migration files in order

set -e  # Exit on error

PROJECT_DIR="/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/ai_trading_bot"
cd "$PROJECT_DIR"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "========================================"
echo "Supabase Migration Runner"
echo "========================================"
echo ""

# Supabase URL is not secret but is configurable for different projects/environments.
SUPABASE_URL="${SUPABASE_URL:-https://lqhlsqsbvamjrxprbapp.supabase.co}"

# Function to execute SQL file using psql directly
execute_sql_file_psql() {
    local file_path="$1"
    local description="$2"

    echo ""
    echo "========================================"
    echo -e "${YELLOW}$description${NC}"
    echo "========================================"

    # Try to get database connection URL
    # Format: postgresql://postgres:[YOUR-PASSWORD]@db.lqhlsqsbvamjrxprbapp.supabase.co:5432/postgres

    echo -e "${YELLOW}Attempting to execute SQL file: $file_path${NC}"

    # Read the SQL file
    # Try direct execution via psql if password is available
    # Otherwise, print the SQL for manual execution

    echo ""
    echo -e "${YELLOW}NOTE: Direct psql connection requires database password.${NC}"
    echo -e "${YELLOW}You can find this in your Supabase Dashboard > Settings > Database${NC}"
    echo ""
    echo "SQL Content Preview:"
    echo "--------------------"
    head -n 20 "$file_path"
    echo "..."
    echo ""

    # Ask user to run manually via Supabase SQL Editor
    echo -e "${YELLOW}RECOMMENDED: Execute this SQL in Supabase SQL Editor:${NC}"
    echo "1. Go to: ${SUPABASE_URL}/project/lqhlsqsbvamjrxprbapp/sql/new"
    echo "2. Copy-paste the contents of: $file_path"
    echo "3. Click 'Run'"
    echo ""

    read -p "Have you executed this migration in Supabase SQL Editor? (y/n) " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${GREEN}✓ Migration marked as completed${NC}"
        return 0
    else
        echo -e "${RED}✗ Migration skipped${NC}"
        return 1
    fi
}

# Migration files in order
echo "Migrations to run:"
echo "1. db/20260323_webhook_lease_columns.sql - Adds columns to webhook_events table"
echo "2. db/20260323_claim_ready_webhooks_rpc.sql - Creates the RPC function"
echo "3. db/20260323_active_checkout_index.sql - Creates partial unique index"
echo ""

# Run migrations in order
migration_results=()

# Migration 1
if execute_sql_file_psql "db/20260323_webhook_lease_columns.sql" "Migration 1: Add webhook lease columns"; then
    migration_results+=("1:SUCCESS")
else
    migration_results+=("1:FAILED")
fi

# Migration 2
if execute_sql_file_psql "db/20260323_claim_ready_webhooks_rpc.sql" "Migration 2: Create claim_ready_webhooks RPC"; then
    migration_results+=("2:SUCCESS")
else
    migration_results+=("2:FAILED")
fi

# Migration 3
if execute_sql_file_psql "db/20260323_active_checkout_index.sql" "Migration 3: Create active checkout index"; then
    migration_results+=("3:SUCCESS")
else
    migration_results+=("3:FAILED")
fi

# Summary
echo ""
echo "========================================"
echo "MIGRATION SUMMARY"
echo "========================================"

for result in "${migration_results[@]}"; do
    migration_num=$(echo "$result" | cut -d: -f1)
    status=$(echo "$result" | cut -d: -f2)

    if [ "$status" = "SUCCESS" ]; then
        echo -e "${GREEN}✓ Migration $migration_num: SUCCESS${NC}"
    else
        echo -e "${RED}✗ Migration $migration_num: FAILED${NC}"
    fi
done

echo ""
echo "========================================"
echo "Next Steps:"
echo "========================================"
echo "1. Verify the changes in your Supabase Database"
echo "2. Check the Tables and Functions are created correctly"
echo "3. Test the webhook claiming functionality"
echo ""
