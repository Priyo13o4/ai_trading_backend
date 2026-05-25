import re

new_content = """import logging
import os
from fastapi import FastAPI, Depends, HTTPException

# Configure logging with UTC
import time
logging.Formatter.converter = time.gmtime
LOG_LEVEL_NAME = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s UTC | %(levelname)-5s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)
logger.info("logging.configured level=%s", LOG_LEVEL_NAME)

from .core.lifespan import lifespan
app = FastAPI(
    lifespan=lifespan,
    title="AI Trading Bot API",
    description="FastAPI backend with Redis caching, auth gating, and MT5 integration",
    version="2.0.0"
)

from .core.cors import setup_cors
setup_cors(app)

from .core.middleware import request_context_middleware, csrf_middleware, security_headers_middleware, cors_debug_middleware
app.middleware("http")(cors_debug_middleware)
app.middleware("http")(security_headers_middleware)
app.middleware("http")(csrf_middleware)
app.middleware("http")(request_context_middleware)

from .core.exceptions import global_http_exception_handler, global_unhandled_exception_handler
app.exception_handler(HTTPException)(global_http_exception_handler)
app.exception_handler(Exception)(global_unhandled_exception_handler)

# Include routers
from .routes.system import router as system_router
from .routes.trading import router as trading_router
from .routes.regime import router as regime_router
from .routes.strategies import router as strategies_router
from .routes.news import router as news_router
from .routes.historical import router as historical_router
from .authn.routes import router as auth_router
from .payments.routes import payments_router
from .payments.webhook_handler import webhook_router
from .routes.referrals import referrals_router
from .core.dependencies import require_signals_context

app.include_router(system_router)
app.include_router(trading_router)
app.include_router(regime_router)
app.include_router(strategies_router)
app.include_router(news_router)
app.include_router(historical_router, dependencies=[Depends(require_signals_context)])
app.include_router(auth_router)
app.include_router(payments_router)
app.include_router(webhook_router)
app.include_router(referrals_router)
"""

with open("app/main.py", "w") as f:
    f.write(new_content)
