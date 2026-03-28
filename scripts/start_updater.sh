#!/bin/bash
# Start Data Updater Scheduler in API Container
# This runs the data updater every 5 minutes to keep indicators fresh

echo "Starting data updater scheduler..."
python3 /app/scripts/data_updater_scheduler.py &

# Keep container alive
echo "Data updater scheduler started in background"
