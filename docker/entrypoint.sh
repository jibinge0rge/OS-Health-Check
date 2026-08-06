#!/bin/sh
set -e

mkdir -p /app/_data /app/_config

# Run from the image's own baked-in copy (/import_if_empty.py), NOT
# /app/docker/import_if_empty.py -- kept outside /app so this startup hook
# is never affected by anything ever mounted over /app (bit us once before
# a bind mount was removed from docker-compose.yml for good; keeping the
# script here is cheap insurance against a repeat, not a sign one is
# expected). PYTHONPATH=/app so it can still import app/lookup_db normally.
PYTHONPATH=/app python /import_if_empty.py

PORT="${APP_PORT:-8000}"
RELOAD_ARGS=""

case "${UVICORN_RELOAD:-false}" in
  1|true|TRUE|yes|YES)
    RELOAD_ARGS="--reload"
    ;;
esac

exec uvicorn app:app --host 0.0.0.0 --port "${PORT}" ${RELOAD_ARGS}
