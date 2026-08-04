"""Startup hook (see entrypoint.sh): if DB mode is enabled and Postgres has
no 'data' rows yet, load them in from the bind-mounted _data/eol_lookup.csv
+ evidence sidecar. Idempotent -- a no-op on every later restart once the
DB has data (imported here, or published through the app), and a no-op in
file mode or on a fresh deployment with nothing in _data/ yet either.
"""

import app
import lookup_db

if app._USE_DB:
    lookup_db.import_from_files_if_empty()
