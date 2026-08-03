"""Tests for lookup_db's cross-instance vendor-sync lock, gated on
DATABASE_URL exactly like tests/test_lookup_db.py -- skipped entirely when no
database is configured.

Real motivation: with multiple app instances sharing one Postgres database,
a plain in-process asyncio.Lock (app.py's VENDOR_SYNC_LOCK) only protects the
instance running a vendor sync -- another instance has no idea it's
happening and can start its own sync, an Add OS, or a Refresh EOL/EOAS at the
same time, racing against the sync's per-product delete+insert. These tests
pin the Postgres-backed lock that makes every instance sharing the database
see the same "is a sync running" state.
"""

from __future__ import annotations

import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta

import lookup_db


def _pg_available() -> bool:
    return bool(str(os.environ.get("DATABASE_URL") or "").strip())


def _temp_schema(prefix: str) -> str:
    return f"test_{prefix}_{uuid.uuid4().hex[:12]}"


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class SyncLockTests(unittest.TestCase):
    def setUp(self) -> None:
        from vendor_lookups.db import drop_schema

        self.schema = _temp_schema("synclock")
        self.drop_schema = drop_schema
        lookup_db.ensure_schema(self.schema)

    def tearDown(self) -> None:
        self.drop_schema(self.schema)

    def test_acquire_returns_a_holder_token_when_free(self) -> None:
        holder = lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        self.assertIsNotNone(holder)

    def test_second_acquire_fails_while_first_is_live(self) -> None:
        first = lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        self.assertIsNotNone(first)
        second = lookup_db.db_acquire_sync_lock("Vendor sync: junos", schema=self.schema)
        self.assertIsNone(second, "a live lock must not be acquirable by a second instance")

    def test_status_reports_the_current_holder_label(self) -> None:
        lookup_db.db_acquire_sync_lock("Vendor sync: suse", schema=self.schema)
        status = lookup_db.db_sync_lock_status(schema=self.schema)
        self.assertIsNotNone(status)
        self.assertEqual(status["label"], "Vendor sync: suse")

    def test_status_is_none_when_no_lock_held(self) -> None:
        self.assertIsNone(lookup_db.db_sync_lock_status(schema=self.schema))

    def test_release_frees_the_lock_for_others(self) -> None:
        holder = lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        lookup_db.db_release_sync_lock(holder, schema=self.schema)
        self.assertIsNone(lookup_db.db_sync_lock_status(schema=self.schema))
        second = lookup_db.db_acquire_sync_lock("Vendor sync: junos", schema=self.schema)
        self.assertIsNotNone(second)

    def test_release_by_a_holder_that_no_longer_owns_it_is_a_noop(self) -> None:
        """A stale holder that eventually tries to clean up after being
        stolen from (see the staleness test below) must not clear the NEW
        owner's lock out from under it."""
        first = lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        # Simulate the first holder going stale and being superseded.
        with lookup_db._connect(self.schema) as connection:
            lookup_db._set_meta(
                connection,
                lookup_db._SYNC_LOCK_HEARTBEAT_KEY,
                (datetime.now() - timedelta(seconds=lookup_db._SYNC_LOCK_STALE_AFTER_SECONDS + 30)).isoformat(
                    timespec="seconds"
                ),
            )
        second = lookup_db.db_acquire_sync_lock("Vendor sync: junos", schema=self.schema)
        self.assertIsNotNone(second)

        lookup_db.db_release_sync_lock(first, schema=self.schema)
        status = lookup_db.db_sync_lock_status(schema=self.schema)
        self.assertIsNotNone(status, "the stale first holder's release must not clear the new owner's lock")
        self.assertEqual(status["label"], "Vendor sync: junos")

    def test_heartbeat_keeps_the_lock_alive_and_reports_true_for_the_owner(self) -> None:
        holder = lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        self.assertTrue(lookup_db.db_heartbeat_sync_lock(holder, schema=self.schema))

    def test_heartbeat_reports_false_for_a_non_owner(self) -> None:
        lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        self.assertFalse(lookup_db.db_heartbeat_sync_lock("not-the-real-holder", schema=self.schema))

    def test_stale_lock_can_be_stolen_by_another_instance(self) -> None:
        """Pins crash recovery: if a holder dies mid-sync without releasing,
        its heartbeat stops advancing, and once it's older than the
        staleness timeout, another instance may take over the lock."""
        first = lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        self.assertIsNotNone(first)

        with lookup_db._connect(self.schema) as connection:
            lookup_db._set_meta(
                connection,
                lookup_db._SYNC_LOCK_HEARTBEAT_KEY,
                (datetime.now() - timedelta(seconds=lookup_db._SYNC_LOCK_STALE_AFTER_SECONDS + 30)).isoformat(
                    timespec="seconds"
                ),
            )

        second = lookup_db.db_acquire_sync_lock("Vendor sync: junos", schema=self.schema)
        self.assertIsNotNone(second, "a stale (crashed-holder) lock must be stealable")
        self.assertNotEqual(first, second)
        status = lookup_db.db_sync_lock_status(schema=self.schema)
        self.assertEqual(status["label"], "Vendor sync: junos")

    def test_recent_heartbeat_is_not_considered_stale(self) -> None:
        lookup_db.db_acquire_sync_lock("Vendor sync: eosl.date", schema=self.schema)
        second = lookup_db.db_acquire_sync_lock("Vendor sync: junos", schema=self.schema)
        self.assertIsNone(second, "a freshly-acquired lock must not look stale")

    def test_concurrent_acquire_exactly_one_wins(self) -> None:
        """Mirrors LookupDbPublishTests's concurrent-publish test: several
        threads (standing in for several app instances) racing to acquire
        the same lock must produce exactly one winner."""
        results: list[str | None] = []
        lock = threading.Lock()

        def attempt(label: str) -> None:
            holder = lookup_db.db_acquire_sync_lock(label, schema=self.schema)
            with lock:
                results.append(holder)

        threads = [threading.Thread(target=attempt, args=(f"Vendor sync: racer-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(len(results), 5)
        winners = [holder for holder in results if holder is not None]
        self.assertEqual(len(winners), 1, f"expected exactly one winner, got: {results}")


if __name__ == "__main__":
    unittest.main()
