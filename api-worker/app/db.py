"""API Worker - Database configuration.

Creates service-specific database connection using shared utilities.
"""

import os
from trading_common.db import build_postgres_dsn_legacy

# Service-specific Postgres DSN (legacy format for psycopg compatibility)
POSTGRES_DSN = build_postgres_dsn_legacy()

# Re-export utility for convenience
from trading_common.db import build_postgres_dsn
