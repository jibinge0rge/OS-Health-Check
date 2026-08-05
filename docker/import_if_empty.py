"""Startup hook (see entrypoint.sh): if Postgres has no 'data' rows yet,
load them in from the image-baked _data/eol_lookup.csv + evidence sidecar.
Idempotent -- a no-op on every later restart once the DB has data (imported
here, or published through the app).

Importing app here also enforces its DATABASE_URL/LOOKUP_DB_ENABLED check --
this script fails loudly (non-zero exit) before uvicorn ever starts if the
deployment is misconfigured.
"""

import app  # noqa: F401 -- imported for its startup config check
import lookup_db

lookup_db.import_from_files_if_empty()
