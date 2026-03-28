#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${SCRAPER_PORT:-8000}"
HOST="${SCRAPER_HOST:-0.0.0.0}"
LOG_FILE="${SCRAPER_LOG_FILE:-uvicorn.log}"
PID_FILE="${SCRAPER_PID_FILE:-uvicorn.pid}"

MODE="${1:-}"

# Activate venv if present
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

# Ensure Python HTTPS downloads (urllib) can verify certificates on macOS.
# undetected_chromedriver internally uses urllib for some downloads.
CERT_FILE="$(python -c "import certifi; print(certifi.where())" 2>/dev/null || true)"
if [[ -n "${CERT_FILE:-}" ]]; then
  export SSL_CERT_FILE="${SSL_CERT_FILE:-$CERT_FILE}"
  export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$CERT_FILE}"
fi

# Minimal-UC mode (mimics the stable forexfactory-scraper approach)
export HEADLESS="${HEADLESS:-0}"
export USE_UNDETECTED_CHROME="${USE_UNDETECTED_CHROME:-1}"
export MINIMAL_UC_MODE="${MINIMAL_UC_MODE:-1}"

# Use a dedicated Chrome profile dir to avoid conflicts with your real Chrome session
export CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-$HOME/Library/Application Support/FFScraperChrome}"
export CHROME_PROFILE_DIRECTORY="${CHROME_PROFILE_DIRECTORY:-Default}"

# Stop any existing uvicorn
if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" || true)"
  if [[ -n "${old_pid:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Stopping existing uvicorn (pid=$old_pid)"
    kill "$old_pid" || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

# Also try to stop by process name (best-effort)
pkill -f "uvicorn app:app" 2>/dev/null || true

CMD=(python -m uvicorn app:app --host "$HOST" --port "$PORT")

echo "Starting scraper API on http://$HOST:$PORT"
echo "HEADLESS=$HEADLESS MINIMAL_UC_MODE=$MINIMAL_UC_MODE USE_UNDETECTED_CHROME=$USE_UNDETECTED_CHROME"
echo "CHROME_USER_DATA_DIR=$CHROME_USER_DATA_DIR"
echo "CHROME_PROFILE_DIRECTORY=$CHROME_PROFILE_DIRECTORY"

if [[ "$MODE" == "--bg" ]]; then
  nohup "${CMD[@]}" >"$LOG_FILE" 2>&1 &
  new_pid=$!
  echo "$new_pid" >"$PID_FILE"
  echo "Started in background (pid=$new_pid), logging to $LOG_FILE"
else
  exec "${CMD[@]}"
fi
