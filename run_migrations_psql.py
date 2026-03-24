#!/usr/bin/env python3
"""
Supabase Migration Runner - Executes SQL migrations via direct PostgreSQL connection
"""
import subprocess
import sys
import os
import getpass

# Project paths
PROJECT_DIR = "/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/ai_trading_bot"
DB_DIR = os.path.join(PROJECT_DIR, "db")

# Supabase connection details
SUPABASE_HOST = "aws-0-us-east-1.pooler.supabase.com"
SUPABASE_DB = "postgres"
SUPABASE_USER = "postgres.lqhlsqsbvamjrxprbapp"
SUPABASE_PORT = "5432"

# Note: Password needs to be provided
# You can get it from: Supabase Dashboard > Settings > Database > Connection string

def print_header(text):
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)

def print_success(text):
    print(f"✓ {text}")

def print_error(text):
    print(f"✗ {text}")

def print_info(text):
    print(f"ℹ {text}")

def get_db_password():
    """Get database password from env or secure prompt."""
    env_password = os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("PGPASSWORD")
    if env_password:
        return env_password

    print("\nEnter your Supabase database password (input hidden):")
    return getpass.getpass("Password: ")

def read_sql_file(filepath):
    """Read SQL file content"""
    with open(filepath, 'r') as f:
        return f.read()

def execute_sql_with_psql(sql_file, description, password=None):
    """
    Execute SQL file using psql command

    Args:
        sql_file: Path to SQL file
        description: Description of the migration
        password: Database password (optional, will prompt if not provided)

    Returns:
        bool: True if successful, False otherwise
    """
    print_header(f"Executing: {description}")
    print_info(f"File: {sql_file}")

    env = os.environ.copy()
    env["PGPASSWORD"] = password

    try:
        # Execute using psql
        result = subprocess.run(
            [
                "/opt/homebrew/opt/libpq/bin/psql",
                "-h", SUPABASE_HOST,
                "-p", SUPABASE_PORT,
                "-U", SUPABASE_USER,
                "-d", SUPABASE_DB,
                "-f", sql_file,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        if result.returncode == 0:
            print_success("Migration executed successfully")
            if result.stdout:
                print("Output:", result.stdout[:500])  # Print first 500 chars
            return True
        else:
            print_error(f"Migration failed with return code {result.returncode}")
            print("psql reported an error. Re-run with SUPABASE_DB_PASSWORD set to verify credentials.")
            return False

    except subprocess.TimeoutExpired:
        print_error("Migration timed out after 30 seconds")
        return False
    except Exception as e:
        print_error(f"Exception occurred: {e}")
        return False

def verify_column_exists(password):
    """Verify webhook_events columns exist"""
    print_header("Verification: Checking webhook_events columns")

    sql = """
    SELECT column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = 'public'
    AND table_name = 'webhook_events'
    AND column_name IN ('processing', 'processing_started_at', 'retry_count', 'next_retry_at', 'last_error')
    ORDER BY column_name;
    """

    env = os.environ.copy()
    env["PGPASSWORD"] = password

    try:
        result = subprocess.run(
            [
                "/opt/homebrew/opt/libpq/bin/psql",
                "-h", SUPABASE_HOST,
                "-p", SUPABASE_PORT,
                "-U", SUPABASE_USER,
                "-d", SUPABASE_DB,
                "-c", sql,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        if result.returncode == 0:
            print_success("Verification query executed")
            print(result.stdout)
            return True
        else:
            print_error("Verification failed")
            print("Verification query failed.")
            return False
    except Exception as e:
        print_error(f"Verification exception: {e}")
        return False

def verify_function_exists(password):
    """Verify claim_ready_webhooks function exists"""
    print_header("Verification: Checking claim_ready_webhooks function")

    sql = """
    SELECT
        proname as function_name,
        pg_get_functiondef(oid) as definition
    FROM pg_proc
    WHERE proname = 'claim_ready_webhooks'
    AND pronamespace = 'public'::regnamespace;
    """

    env = os.environ.copy()
    env["PGPASSWORD"] = password

    try:
        result = subprocess.run(
            [
                "/opt/homebrew/opt/libpq/bin/psql",
                "-h", SUPABASE_HOST,
                "-p", SUPABASE_PORT,
                "-U", SUPABASE_USER,
                "-d", SUPABASE_DB,
                "-c", sql,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        if result.returncode == 0:
            print_success("Verification query executed")
            print(result.stdout[:1000])  # Print first 1000 chars
            return True
        else:
            print_error("Verification failed")
            print("Verification query failed.")
            return False
    except Exception as e:
        print_error(f"Verification exception: {e}")
        return False

def verify_index_exists(password):
    """Verify uq_active_payment_per_user index exists"""
    print_header("Verification: Checking uq_active_payment_per_user index")

    sql = """
    SELECT
        indexname,
        indexdef
    FROM pg_indexes
    WHERE schemaname = 'public'
    AND indexname = 'uq_active_payment_per_user';
    """

    env = os.environ.copy()
    env["PGPASSWORD"] = password

    try:
        result = subprocess.run(
            [
                "/opt/homebrew/opt/libpq/bin/psql",
                "-h", SUPABASE_HOST,
                "-p", SUPABASE_PORT,
                "-U", SUPABASE_USER,
                "-d", SUPABASE_DB,
                "-c", sql,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        if result.returncode == 0:
            print_success("Verification query executed")
            print(result.stdout)
            return True
        else:
            print_error("Verification failed")
            print("Verification query failed.")
            return False
    except Exception as e:
        print_error(f"Verification exception: {e}")
        return False

def main():
    """Main execution function"""
    print_header("Supabase Migration Runner")

    if len(sys.argv) > 1:
        print_error("Passing password via CLI argument is not supported for security reasons.")
        print_info("Use SUPABASE_DB_PASSWORD/PGPASSWORD env var or secure prompt input.")
        return 1

    print("\nTo get your Supabase database password:")
    print("1. Go to your Supabase project settings > Database")
    print("2. Find the 'Connection string' section")
    print("3. Click 'Show' to reveal the password")
    print("4. Use SUPABASE_DB_PASSWORD env var or enter it at the secure prompt\n")
    password = get_db_password()

    if not password:
        print_error("Password is required!")
        sys.exit(1)

    # Define migrations in order
    migrations = [
        {
            "file": os.path.join(DB_DIR, "20260323_webhook_lease_columns.sql"),
            "description": "Migration 1: Add webhook lease columns",
            "verify": verify_column_exists
        },
        {
            "file": os.path.join(DB_DIR, "20260323_claim_ready_webhooks_rpc.sql"),
            "description": "Migration 2: Create claim_ready_webhooks RPC function",
            "verify": verify_function_exists
        },
        {
            "file": os.path.join(DB_DIR, "20260323_active_checkout_index.sql"),
            "description": "Migration 3: Create active checkout index",
            "verify": verify_index_exists
        },
    ]

    # Track results
    results = []

    # Execute each migration
    for i, migration in enumerate(migrations, 1):
        print(f"\n{'#' * 60}")
        print(f"MIGRATION {i} OF {len(migrations)}")
        print(f"{'#' * 60}")

        success = execute_sql_with_psql(
            migration["file"],
            migration["description"],
            password
        )

        results.append({
            "number": i,
            "description": migration["description"],
            "success": success
        })

        # Run verification if migration succeeded
        if success and migration.get("verify"):
            migration["verify"](password)

        if not success:
            print_error(f"Migration {i} failed! Check the error above.")
            print_info("Note: If the error is 'column/function/index already exists', that's OK - migrations are idempotent")

    # Print summary
    print_header("MIGRATION SUMMARY")

    all_success = True
    for result in results:
        status = "✓ SUCCESS" if result["success"] else "✗ FAILED"
        print(f"{status}: Migration {result['number']} - {result['description']}")
        if not result["success"]:
            all_success = False

    print("\n" + "=" * 60)

    if all_success:
        print_success("All migrations completed successfully!")
    else:
        print_error("Some migrations failed. Please review the errors above.")
        print_info("If errors are about existing objects, migrations may have already been applied.")

    print("\nNext steps:")
    print("1. Verify changes in Supabase Dashboard")
    print("2. Check Tables > webhook_events for new columns")
    print("3. Check Database > Functions for claim_ready_webhooks")
    print("4. Check payment_transactions for uq_active_payment_per_user index")

    return 0 if all_success else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nMigration cancelled by user")
        sys.exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e.__class__.__name__}")
        sys.exit(1)
