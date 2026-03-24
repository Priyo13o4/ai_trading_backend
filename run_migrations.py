#!/usr/bin/env python3
"""
Script to run Supabase migrations using the Management API
"""
import os
import sys
import requests
import json

def get_supabase_credentials():
    """Load required Supabase credentials from environment variables."""
    supabase_url = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE_PROJECT_URL")
    service_role_key = os.getenv("SUPABASE_SECRET_KEY")

    missing = []
    if not supabase_url:
        missing.append("SUPABASE_URL")
    if not service_role_key:
        missing.append("SUPABASE_SECRET_KEY")

    if missing:
        print(f"✗ Missing required environment variable(s): {', '.join(missing)}")
        print("Set them in your shell before running this script.")
        sys.exit(1)

    return supabase_url, service_role_key

def execute_sql_file(file_path, supabase_url, service_role_key):
    """Execute a SQL file using Supabase REST API"""
    print(f"\n{'='*60}")
    print(f"Executing: {file_path}")
    print(f"{'='*60}")

    # Read SQL file
    with open(file_path, 'r') as f:
        sql = f.read()

    # Try using the SQL Editor API endpoint
    url = f"{supabase_url}/rest/v1/rpc/exec_sql"
    headers = {
        "apikey": service_role_key,
        "Content-Type": "application/json"
    }

    payload = {"sql": sql}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)

        if response.status_code == 200:
            print(f"✓ Success: Migration applied successfully")
            return True
        else:
            print(f"✗ Error: {response.status_code}")
            print("Migration request failed. Check Supabase logs for details.")
            return False
    except Exception as e:
        print(f"✗ Exception while calling migration endpoint: {e.__class__.__name__}")
        return False

def verify_columns(supabase_url, service_role_key):
    """Verify webhook_events table has new columns"""
    print("\n" + "="*60)
    print("Verifying Migration 1: Checking webhook_events columns")
    print("="*60)

    # Query to check if columns exist
    sql = """
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'webhook_events'
    AND column_name IN ('processing', 'processing_started_at', 'retry_count', 'next_retry_at', 'last_error')
    ORDER BY column_name;
    """

    url = f"{supabase_url}/rest/v1/rpc/exec_sql"
    headers = {
        "apikey": service_role_key,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json={"sql": sql}, timeout=30)
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Verification failed: {e.__class__.__name__}")

def verify_function(supabase_url, service_role_key):
    """Verify claim_ready_webhooks function exists"""
    print("\n" + "="*60)
    print("Verifying Migration 2: Checking claim_ready_webhooks function")
    print("="*60)

    sql = """
    SELECT proname, prokind
    FROM pg_proc
    WHERE proname = 'claim_ready_webhooks';
    """

    url = f"{supabase_url}/rest/v1/rpc/exec_sql"
    headers = {
        "apikey": service_role_key,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json={"sql": sql}, timeout=30)
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Verification failed: {e.__class__.__name__}")

def verify_index(supabase_url, service_role_key):
    """Verify uq_active_payment_per_user index exists"""
    print("\n" + "="*60)
    print("Verifying Migration 3: Checking uq_active_payment_per_user index")
    print("="*60)

    sql = """
    SELECT indexname, indexdef
    FROM pg_indexes
    WHERE indexname = 'uq_active_payment_per_user';
    """

    url = f"{supabase_url}/rest/v1/rpc/exec_sql"
    headers = {
        "apikey": service_role_key,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, headers=headers, json={"sql": sql}, timeout=30)
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Verification failed: {e.__class__.__name__}")

def main():
    base_path = "/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/ai_trading_bot/db"
    supabase_url, service_role_key = get_supabase_credentials()

    migrations = [
        ("20260323_webhook_lease_columns.sql", verify_columns),
        ("20260323_claim_ready_webhooks_rpc.sql", verify_function),
        ("20260323_active_checkout_index.sql", verify_index),
    ]

    results = []

    for migration_file, verify_func in migrations:
        file_path = os.path.join(base_path, migration_file)
        success = execute_sql_file(file_path, supabase_url, service_role_key)
        results.append((migration_file, success))

        if success and verify_func:
            verify_func(supabase_url, service_role_key)

    # Print summary
    print("\n" + "="*60)
    print("MIGRATION SUMMARY")
    print("="*60)
    for migration, success in results:
        status = "✓ SUCCESS" if success else "✗ FAILED"
        print(f"{status}: {migration}")

if __name__ == "__main__":
    main()
