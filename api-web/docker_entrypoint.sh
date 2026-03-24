#!/bin/sh
set -eu

STARTUP_CHECK_ENABLED="${STARTUP_CHECK_ENABLED:-1}"
STARTUP_CHECK_ROLE="${STARTUP_CHECK_ROLE:-api-web}"
STARTUP_GATE_COMPLETED="${STARTUP_GATE_COMPLETED:-0}"

is_truthy() {
	case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
	1|true|yes|on)
		return 0
		;;
	*)
		return 1
		;;
	esac
}

if [ "$STARTUP_GATE_COMPLETED" = "1" ]; then
	echo "[ENTRYPOINT] Startup gate already completed for role: ${STARTUP_CHECK_ROLE}"
elif is_truthy "$STARTUP_CHECK_ENABLED"; then
	echo "[ENTRYPOINT] Running startup gate for role: ${STARTUP_CHECK_ROLE}"
	python /app/startup_check.py
	export STARTUP_GATE_COMPLETED=1
	echo "[ENTRYPOINT] Startup gate passed for role: ${STARTUP_CHECK_ROLE}"
else
	echo "[ENTRYPOINT] Startup gate disabled for role: ${STARTUP_CHECK_ROLE}"
fi

exec "$@"
