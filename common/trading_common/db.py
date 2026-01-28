"""Database utilities - Factory functions only, no global instances."""

import os
from typing import Optional


def build_postgres_dsn(
    host: Optional[str] = None,
    port: Optional[str] = None,
    db: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None
) -> str:
    """
    Build PostgreSQL DSN from components.
    
    Args:
        host: Database host (default: from POSTGRES_HOST env)
        port: Database port (default: from POSTGRES_PORT env)
        db: Database name (default: from TRADING_BOT_DB or POSTGRES_DB env)
        user: Database user (default: from POSTGRES_USER env)
        password: Database password (default: from POSTGRES_PASSWORD env)
    
    Returns:
        PostgreSQL connection string
    """
    _host = host or os.getenv('POSTGRES_HOST', 'postgres')
    _port = port or os.getenv('POSTGRES_PORT', '5432')
    _db_name = db or os.getenv('TRADING_BOT_DB') or os.getenv('POSTGRES_DB', 'ai_trading_bot_data')
    _user = user or os.getenv('POSTGRES_USER')
    _password = password if password is not None else os.getenv('POSTGRES_PASSWORD', '')
    
    return f"postgresql://{_user}:{_password}@{_host}:{_port}/{_db_name}"


def build_postgres_dsn_legacy(
    host: Optional[str] = None,
    port: Optional[str] = None,
    db: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None
) -> str:
    """
    Build PostgreSQL DSN in legacy format (psycopg2 style).
    
    Returns:
        PostgreSQL connection string in key=value format
    """
    _host = host or os.getenv('POSTGRES_HOST', 'postgres')
    _port = port or os.getenv('POSTGRES_PORT', '5432')
    _db_name = db or os.getenv('TRADING_BOT_DB') or os.getenv('POSTGRES_DB', 'ai_trading_bot_data')
    _user = user or os.getenv('POSTGRES_USER')
    _password = password if password is not None else os.getenv('POSTGRES_PASSWORD', '')
    
    return f"host={_host} port={_port} dbname={_db_name} user={_user} password={_password}"
