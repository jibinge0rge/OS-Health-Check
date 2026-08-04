#!/bin/sh
set -e

mkdir -p /app/_data /app/_draft /app/_backup /app/_config

# Run from the image's own baked-in copy (/import_if_empty.py), NOT
# /app/docker/import_if_empty.py -- /app is bind-mounted from the host in
# dev/some deployments (see docker-compose.yml's "volumes: - ./:/app"), so
# a stale or partial checkout on the host would silently hide whatever the
# image itself has, causing exactly this: "can't open file
# '/app/docker/import_if_empty.py'" even though the image was built fine.
# The script still imports app/lookup_db with PYTHONPATH=/app, so it always
# operates against whichever app.py is actually running (image or
# bind-mounted) -- only the entrypoint script's OWN file lookup is pinned
# outside the mount.
PYTHONPATH=/app python /import_if_empty.py

PORT="${APP_PORT:-8000}"
RELOAD_ARGS=""

case "${UVICORN_RELOAD:-false}" in
  1|true|TRUE|yes|YES)
    RELOAD_ARGS="--reload"
    ;;
esac

exec uvicorn app:app --host 0.0.0.0 --port "${PORT}" ${RELOAD_ARGS}
