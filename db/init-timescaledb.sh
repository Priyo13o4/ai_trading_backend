#!/bin/bash
set -e

echo "🚀 Installing TimescaleDB extension..."

# Install TimescaleDB from PostgreSQL apt repository
apt-get update
apt-get install -y postgresql-18-timescaledb-2

# Add timescaledb to shared_preload_libraries
echo "shared_preload_libraries = 'timescaledb'" >> /var/lib/postgresql/data/postgresql.conf

echo "✅ TimescaleDB installed! Restart PostgreSQL to activate."
