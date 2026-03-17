#!/bin/bash
# Startup script for trading bot worker
# Runs MT5 ingest TCP server + indicator scheduler in one container

set -euo pipefail

echo "================================"
echo "AI Trading Bot Worker - Starting"
echo "================================"

ENABLE_SCHEDULER="${WORKER_ENABLE_SCHEDULER:-true}"
SCHEDULER_REQUIRED="${WORKER_SCHEDULER_REQUIRED:-true}"
SCHEDULER_PID=""
SCHEDULER_RESTART_COUNT=0
SCHEDULER_BACKOFF_SECONDS="${WORKER_SCHEDULER_RESTART_INITIAL_SECONDS:-2}"
SCHEDULER_MAX_BACKOFF_SECONDS="${WORKER_SCHEDULER_RESTART_MAX_SECONDS:-60}"
SCHEDULER_MAX_RESTARTS="${WORKER_SCHEDULER_MAX_RESTARTS:-10}"
STOP_REQUESTED=0
INGEST_PID=""
FORCED_EXIT_CODE=""

start_scheduler() {
	echo "Starting indicator scheduler..."
	python -u /app/scripts/worker/data_updater_scheduler.py &
	SCHEDULER_PID=$!
}

shutdown_children() {
	set +e
	STOP_REQUESTED=1
	if [[ -n "${SCHEDULER_PID}" ]] && kill -0 "${SCHEDULER_PID}" >/dev/null 2>&1; then
		echo "Stopping scheduler (pid=${SCHEDULER_PID})..."
		kill "${SCHEDULER_PID}" >/dev/null 2>&1
	fi
	if [[ -n "${INGEST_PID}" ]] && kill -0 "${INGEST_PID}" >/dev/null 2>&1; then
		echo "Stopping MT5 ingest server (pid=${INGEST_PID})..."
		kill "${INGEST_PID}" >/dev/null 2>&1
	fi
	wait >/dev/null 2>&1
	set -e
}

trap shutdown_children TERM INT

if [[ "${ENABLE_SCHEDULER,,}" == "true" || "${ENABLE_SCHEDULER}" == "1" || "${ENABLE_SCHEDULER,,}" == "yes" ]]; then
	start_scheduler
fi

is_scheduler_required() {
	[[ "${SCHEDULER_REQUIRED,,}" == "true" || "${SCHEDULER_REQUIRED}" == "1" || "${SCHEDULER_REQUIRED,,}" == "yes" ]]
}

echo "Starting MT5 ingest server (TCP port 9001)..."
python -u /app/scripts/mt5_ingest_server.py &
INGEST_PID=$!

while kill -0 "${INGEST_PID}" >/dev/null 2>&1; do
	if [[ -n "${SCHEDULER_PID}" ]]; then
		if ! kill -0 "${SCHEDULER_PID}" >/dev/null 2>&1; then
			set +e
			wait "${SCHEDULER_PID}"
			SCHEDULER_EXIT_CODE=$?
			set -e
			SCHEDULER_PID=""

			if [[ "${STOP_REQUESTED}" == "1" ]]; then
				break
			fi

			SCHEDULER_RESTART_COUNT=$((SCHEDULER_RESTART_COUNT + 1))
			echo "Scheduler exited (code=${SCHEDULER_EXIT_CODE}); restart attempt ${SCHEDULER_RESTART_COUNT} in ${SCHEDULER_BACKOFF_SECONDS}s"

			if [[ "${SCHEDULER_MAX_RESTARTS}" -gt 0 && "${SCHEDULER_RESTART_COUNT}" -gt "${SCHEDULER_MAX_RESTARTS}" ]]; then
				if is_scheduler_required; then
					echo "Scheduler exceeded max restart attempts (${SCHEDULER_MAX_RESTARTS}) and WORKER_SCHEDULER_REQUIRED=true; terminating ingest and exiting"
					FORCED_EXIT_CODE=1
					if [[ -n "${INGEST_PID}" ]] && kill -0 "${INGEST_PID}" >/dev/null 2>&1; then
						kill "${INGEST_PID}" >/dev/null 2>&1 || true
					fi
				else
					echo "Scheduler exceeded max restart attempts (${SCHEDULER_MAX_RESTARTS}); leaving scheduler stopped while ingest continues"
				fi
				break
			fi

			sleep "${SCHEDULER_BACKOFF_SECONDS}"
			start_scheduler
			if [[ "${SCHEDULER_BACKOFF_SECONDS}" -lt "${SCHEDULER_MAX_BACKOFF_SECONDS}" ]]; then
				SCHEDULER_BACKOFF_SECONDS=$((SCHEDULER_BACKOFF_SECONDS * 2))
				if [[ "${SCHEDULER_BACKOFF_SECONDS}" -gt "${SCHEDULER_MAX_BACKOFF_SECONDS}" ]]; then
					SCHEDULER_BACKOFF_SECONDS="${SCHEDULER_MAX_BACKOFF_SECONDS}"
				fi
			fi
		else
			sleep 1
		fi
	else
		sleep 1
	fi
done

set +e
wait "${INGEST_PID}"
EXIT_CODE=$?
set -e

if [[ -n "${FORCED_EXIT_CODE}" ]]; then
	EXIT_CODE="${FORCED_EXIT_CODE}"
fi

echo "MT5 ingest server exited (code=${EXIT_CODE}). Shutting down remaining subprocesses..."
shutdown_children
exit "${EXIT_CODE}"
