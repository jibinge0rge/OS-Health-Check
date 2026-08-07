"""PostgreSQL-backed identity storage -- deployments (tenants) and the users
seen from each deployment's Keycloak.

Mirrors lookup_db.py's connection/ensure_schema idiom exactly (same shared
psycopg pool from vendor_lookups/db.py, same "ensure schema once per process"
pattern) but keeps its own schema, since identity is a distinct concern from
the lookup rows/evidence/draft/backups data lookup_db.py owns.

A Keycloak `sub` claim is only unique within one realm. Since different
deployments may run entirely separate Keycloak realms/instances while
sharing this same Postgres (see AUTH_MULTITENANCY_PLAN.md §5.2), the natural
user key is the composite (deployment_id, keycloak_sub) -- deployment_id
always comes from *this app instance's own* DEPLOYMENT_ID env var (auth.py),
never from the token, so this stays correct regardless of whether Keycloak
is per-deployment or one realm shared by every deployment.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql

from vendor_lookups.db import get_pool

SCHEMA = "iam"

_DDL = """
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id   TEXT PRIMARY KEY,
    keycloak_issuer TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    deployment_id  TEXT NOT NULL,
    keycloak_sub   TEXT NOT NULL,
    username       TEXT NOT NULL DEFAULT '',
    email          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    last_login_at  TEXT NOT NULL DEFAULT '',
    UNIQUE (deployment_id, keycloak_sub)
);
CREATE INDEX IF NOT EXISTS idx_iam_users_deployment ON users (deployment_id);
"""

_ensured_schemas: set[str] = set()


def ensure_schema(schema: str = SCHEMA) -> None:
    if schema in _ensured_schemas:
        return
    pool = get_pool()
    with pool.connection() as connection:
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        connection.execute(_DDL)
        connection.commit()
    _ensured_schemas.add(schema)


@contextmanager
def _connect(schema: str = SCHEMA) -> Iterator[psycopg.Connection[Any]]:
    ensure_schema(schema)
    pool = get_pool()
    with pool.connection() as connection:
        connection.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def get_or_create_deployment(deployment_id: str, keycloak_issuer: str, schema: str = SCHEMA) -> None:
    """Registers this deployment on first sight (JIT, same spirit as
    lookup_db's import_from_files_if_empty auto-seed). keycloak_issuer is
    stored for defense-in-depth / operator visibility only -- it is never
    read back to make an authorization decision; auth.py always validates
    each request's token against its own configured KEYCLOAK_ISSUER_URL."""
    with _connect(schema) as connection:
        connection.execute(
            "INSERT INTO deployments (deployment_id, keycloak_issuer, created_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (deployment_id) DO UPDATE SET keycloak_issuer = EXCLUDED.keycloak_issuer",
            (deployment_id, keycloak_issuer, datetime.now().isoformat(timespec="seconds")),
        )


def upsert_user(
    deployment_id: str,
    keycloak_sub: str,
    username: str = "",
    email: str = "",
    schema: str = SCHEMA,
) -> str:
    """JIT-provisions a user the first time this (deployment_id, sub) pair is
    seen; refreshes username/email/last_login_at on every subsequent call.
    Returns this app's own internal user id (a UUID string), which is what
    every draft table actually keys on -- never the raw Keycloak sub."""
    now = datetime.now().isoformat(timespec="seconds")
    with _connect(schema) as connection:
        row = connection.execute(
            "SELECT id FROM users WHERE deployment_id = %s AND keycloak_sub = %s",
            (deployment_id, keycloak_sub),
        ).fetchone()
        if row is not None:
            user_id = row["id"]
            connection.execute(
                "UPDATE users SET username = %s, email = %s, last_login_at = %s WHERE id = %s",
                (username, email, now, user_id),
            )
            return user_id

        user_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO users (id, deployment_id, keycloak_sub, username, email, created_at, last_login_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (user_id, deployment_id, keycloak_sub, username, email, now, now),
        )
        return user_id
