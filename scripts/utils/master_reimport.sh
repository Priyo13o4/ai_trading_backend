#!/bin/bash
# ==============================================================================
# Master Data Reimport Orchestration Script
# ==============================================================================
# Orchestrates the complete process of clearing and reimporting historical data
# 
# Process:
# 1. Apply schema updates for W1 and MN1 timeframes
# 2. Run pre-import cleanup (truncate tables, disable compression)
# 3. Import historical CSV data with aggregation
# 4. Run post-import restoration (re-enable compression, recalc indicators)
#
# Usage from host machine:
#   cd ai_trading_bot
#   ./scripts/master_reimport.sh
#
# Usage from inside Docker container:
#   docker compose exec api bash /app/scripts/master_reimport.sh
# ==============================================================================

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ==============================================================================
# Configuration
# ==============================================================================

DB_USER="Priyo13o4"
DB_NAME="ai_trading_bot_data"
DB_HOST="n8n-postgres"
CSV_SOURCE_DIR="/Volumes/My Drive/Priyodip/college notes and stuff/Coding stuff (Vs code)/Docker Projects/Mt5-chartdata-export"
CSV_MOUNT_DIR="/app/csv_data"

# ==============================================================================
# Helper Functions
# ==============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo ""
    echo "================================================================================"
    echo "$1"
    echo "================================================================================"
    echo ""
}

confirm_action() {
    local message="$1"
    echo -e "${YELLOW}⚠️  $message${NC}"
    read -p "Type 'yes' to continue, anything else to abort: " confirmation
    
    if [ "$confirmation" != "yes" ]; then
        log_error "Operation aborted by user"
        exit 1
    fi
}

# ==============================================================================
# Pre-flight Checks
# ==============================================================================

print_header "PRE-FLIGHT CHECKS"

# Check if running inside Docker container
if [ -f /.dockerenv ]; then
    log_info "Running inside Docker container ✓"
    INSIDE_DOCKER=true
else
    log_info "Running on host machine"
    INSIDE_DOCKER=false
fi

# Check database connection
log_info "Checking database connection..."
if [ "$INSIDE_DOCKER" = true ]; then
    if psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -c "SELECT 1" > /dev/null 2>&1; then
        log_success "Database connection successful"
    else
        log_error "Cannot connect to database"
        exit 1
    fi
else
    log_info "Skipping database check (will run in Docker)"
fi

# Check CSV files
log_info "Checking CSV files..."
if [ "$INSIDE_DOCKER" = true ]; then
    CSV_DIR="$CSV_MOUNT_DIR"
else
    CSV_DIR="$CSV_SOURCE_DIR"
fi

if [ ! -d "$CSV_DIR" ]; then
    log_error "CSV directory not found: $CSV_DIR"
    if [ "$INSIDE_DOCKER" = true ]; then
        log_error "Make sure CSV directory is mounted at $CSV_MOUNT_DIR"
    fi
    exit 1
fi

CSV_COUNT=$(ls -1 "$CSV_DIR"/*.csv 2>/dev/null | wc -l)
if [ "$CSV_COUNT" -eq 0 ]; then
    log_error "No CSV files found in $CSV_DIR"
    exit 1
fi

log_success "Found $CSV_COUNT CSV files"
ls -lh "$CSV_DIR"/*.csv | awk '{print "  - " $9 " (" $5 ")"}'

# Get current data count
log_info "Checking current database state..."
if [ "$INSIDE_DOCKER" = true ]; then
    CURRENT_ROWS=$(psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -t -c "SELECT COUNT(*) FROM candlesticks" 2>/dev/null | xargs)
    log_info "Current candlesticks rows: $CURRENT_ROWS"
else
    log_info "Will check database state inside Docker"
fi

# ==============================================================================
# Confirmation
# ==============================================================================

print_header "⚠️  WARNING - DESTRUCTIVE OPERATION ⚠️"

cat << EOF
This script will:
1. TRUNCATE candlesticks, technical_indicators, and market_structure tables
2. DELETE all existing historical data
3. Import ~9 years of data from CSV files
4. Recalculate all indicators

Current database will be COMPLETELY REPLACED with CSV data.

This process may take 2-4 HOURS to complete.

EOF

confirm_action "Are you sure you want to proceed with FULL DATA REIMPORT?"

# ==============================================================================
# Step 1: Schema Update
# ==============================================================================

print_header "STEP 1/4: Apply Schema Updates"

log_info "Applying W1 and MN1 timeframe support..."

if [ "$INSIDE_DOCKER" = true ]; then
    psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -f /app/db/add_weekly_monthly_timeframes.sql
    log_success "Schema updated successfully"
else
    log_info "Running schema update via Docker..."
    docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" < db/add_weekly_monthly_timeframes.sql
    log_success "Schema updated successfully"
fi

# ==============================================================================
# Step 2: Pre-Import Cleanup
# ==============================================================================

print_header "STEP 2/4: Pre-Import Cleanup"

log_info "Running pre-import cleanup script..."
log_warning "This will truncate all candlestick data!"

if [ "$INSIDE_DOCKER" = true ]; then
    python /app/scripts/pre_import_cleanup.py
else
    docker compose exec api python /app/scripts/pre_import_cleanup.py
fi

log_success "Pre-import cleanup completed"

# ==============================================================================
# Step 3: Import Historical Data
# ==============================================================================

print_header "STEP 3/4: Import Historical CSV Data"

log_info "Starting CSV import (this will take 2-4 hours)..."
log_info "CSV files will be read from: $CSV_DIR"

if [ "$INSIDE_DOCKER" = true ]; then
    python /app/scripts/import_historical_csv.py
else
    # Mount CSV directory and run import
    docker compose run --rm \
        -v "$CSV_SOURCE_DIR:$CSV_MOUNT_DIR:ro" \
        api python /app/scripts/import_historical_csv.py
fi

log_success "Historical data import completed"

# ==============================================================================
# Step 4: Post-Import Restoration
# ==============================================================================

print_header "STEP 4/4: Post-Import Restoration"

log_info "Running post-import restoration script..."
log_info "Re-enabling compression, refreshing views, recalculating indicators..."

if [ "$INSIDE_DOCKER" = true ]; then
    python /app/scripts/post_import_restoration.py
else
    docker compose exec api python /app/scripts/post_import_restoration.py
fi

log_success "Post-import restoration completed"

# ==============================================================================
# Verification
# ==============================================================================

print_header "VERIFICATION & SUMMARY"

log_info "Running final verification queries..."

if [ "$INSIDE_DOCKER" = true ]; then
    NEW_ROWS=$(psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -t -c "SELECT COUNT(*) FROM candlesticks" | xargs)
    
    echo ""
    log_success "Final row count: $NEW_ROWS"
    echo ""
    
    log_info "Data distribution by symbol and timeframe:"
    psql -U "$DB_USER" -d "$DB_NAME" -h "$DB_HOST" -c "
        SELECT 
            symbol, 
            timeframe, 
            COUNT(*) AS rows,
            MIN(time) AS earliest,
            MAX(time) AS latest
        FROM candlesticks
        GROUP BY symbol, timeframe
        ORDER BY symbol, 
            CASE timeframe
                WHEN 'M1' THEN 1 WHEN 'M5' THEN 2 WHEN 'M15' THEN 3
                WHEN 'M30' THEN 4 WHEN 'H1' THEN 5 WHEN 'H4' THEN 6
                WHEN 'D1' THEN 7 WHEN 'W1' THEN 8 WHEN 'MN1' THEN 9
            END;
    "
else
    log_info "Run verification queries via Docker:"
    docker compose exec postgres psql -U "$DB_USER" -d "$DB_NAME" -c "
        SELECT 
            symbol, 
            COUNT(DISTINCT timeframe) AS timeframes,
            COUNT(*) AS total_rows,
            MIN(time) AS earliest,
            MAX(time) AS latest
        FROM candlesticks
        GROUP BY symbol
        ORDER BY symbol;
    "
fi

# ==============================================================================
# Completion
# ==============================================================================

print_header "🎉 DATA REIMPORT COMPLETED SUCCESSFULLY 🎉"

cat << EOF
All steps completed successfully!

Summary:
✓ Schema updated with W1 and MN1 timeframe support
✓ Old data cleared and tables truncated
✓ Historical CSV data imported with full aggregation
✓ Compression re-enabled and indicators recalculated

Next steps:
1. Verify data quality with sample queries
2. Check data_freshness view for any gaps
3. Restart realtime_updater.py to resume live data collection
4. Monitor TimescaleDB compression over next 24 hours

Database is now ready for production use!

EOF

log_success "Reimport orchestration completed at $(date)"
