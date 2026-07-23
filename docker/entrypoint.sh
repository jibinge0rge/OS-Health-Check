#!/bin/sh
set -e

mkdir -p /app/_data /app/_draft /app/_backup /app/_config

PORT="${APP_PORT:-8000}"
RELOAD_ARGS=""

case "${UVICORN_RELOAD:-false}" in
  1|true|TRUE|yes|YES)
    RELOAD_ARGS="--reload"
    ;;
esac

exec uvicorn app:app --host 0.0.0.0 --port "${PORT}" ${RELOAD_ARGS}
