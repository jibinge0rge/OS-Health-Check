"""Startup hook (see entrypoint.sh): if Postgres has no 'data' rows yet,
load them in from the image-baked _data/eol_lookup.csv + evidence sidecar.
Idempotent -- a no-op on every later restart once the DB has data (imported
here, or published through the app).

Also retires any pre-cutover single-global-draft rows into a backups
snapshot (AUTH_MULTITENANCY_PLAN.md §9) -- also idempotent, a no-op once
there's nothing left in the legacy location.

Importing app here also enforces its DATABASE_URL/LOOKUP_DB_ENABLED and
DEPLOYMENT_ID/KEYCLOAK_* checks -- this script fails loudly (non-zero exit)
before uvicorn ever starts if the deployment is misconfigured.
"""

import app  # noqa: F401 -- imported for its startup config check
import lookup_db

lookup_db.import_from_files_if_empty()
lookup_db.migrate_legacy_global_draft_if_present()
