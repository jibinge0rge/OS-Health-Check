"""Tests for the cross-instance vendor-sync guard: with multiple app
instances sharing one Postgres database, starting a vendor sync, an Add OS,
or a Refresh EOL/EOAS while a sync is already running *anywhere* sharing that
database must be blocked, not just within the same process (app.py's
in-process VENDOR_SYNC_LOCK/LOOKUP_REFRESH_LOCK only ever protected one
process). Everything that would touch a real Postgres connection
(lookup_db.db_acquire_sync_lock/db_sync_lock_status/db_release_sync_lock) is
mocked here -- see tests/test_lookup_db_sync_lock.py for the real,
DATABASE_URL-gated lock tests.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

_ENV_SNAPSHOT = dict(os.environ)
import app  # noqa: E402

os.environ.clear()
os.environ.update(_ENV_SNAPSHOT)


def _row(os_string: str = "Ubuntu 24.04") -> app.LookupRow:
    return app.LookupRow(os_string=os_string, normalized_os_detailed_name=os_string, normalized_os=os_string)


class RaiseIfVendorSyncRunningTests(unittest.TestCase):
    def test_no_lock_held_does_not_raise(self) -> None:
        with patch.object(app.lookup_db, "db_sync_lock_status", return_value=None):
            app.raise_if_vendor_sync_running()  # must not raise

    def test_lock_held_raises_409_naming_the_blocker(self) -> None:
        with patch.object(
            app.lookup_db,
            "db_sync_lock_status",
            return_value={"label": "Vendor sync: eosl.date", "heartbeat_at": "2026-01-01T00:00:00"},
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                app.raise_if_vendor_sync_running()
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Vendor sync: eosl.date", ctx.exception.detail)


class RefreshEndpointsBlockedDuringVendorSyncTests(unittest.IsolatedAsyncioTestCase):
    def _sync_running(self):
        return patch.object(
            app.lookup_db,
            "db_sync_lock_status",
            return_value={"label": "Vendor sync: junos", "heartbeat_at": "2026-01-01T00:00:00"},
        )

    async def test_single_row_refresh_blocked_and_never_runs_the_lookup(self) -> None:
        with self._sync_running(), patch.object(app, "refresh_rows_lifecycle_chunk") as chunk_mock:
            with self.assertRaises(app.HTTPException) as ctx:
                await app.refresh_lookup_row(app.RowRefreshRequest(row=_row()))
        self.assertEqual(ctx.exception.status_code, 409)
        chunk_mock.assert_not_called()

    async def test_bulk_refresh_blocked_and_never_runs_the_lookup(self) -> None:
        with self._sync_running(), patch.object(app, "refresh_rows_lifecycle_chunk") as chunk_mock:
            with self.assertRaises(app.HTTPException) as ctx:
                await app.refresh_lookup_rows(app.RowsRefreshRequest(rows=[_row()]))
        self.assertEqual(ctx.exception.status_code, 409)
        chunk_mock.assert_not_called()

    async def test_streamed_bulk_refresh_blocked_before_any_streaming_starts(self) -> None:
        """Must raise from the endpoint itself, not from inside the
        generator -- otherwise a 409 would surface only after the stream's
        "started" SSE event had already gone out, corrupting the response."""
        with self._sync_running():
            with self.assertRaises(app.HTTPException) as ctx:
                await app.refresh_lookup_rows_stream(app.RowsRefreshRequest(rows=[_row()]))
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_toolbar_refresh_stream_blocked_before_any_streaming_starts(self) -> None:
        payload = app.LookupRefreshStreamRequest(
            rows=[_row()], evidence={"by_os": {}, "updated_at": ""}, source="draft", is_partial_refresh=True
        )
        with self._sync_running():
            with self.assertRaises(app.HTTPException) as ctx:
                await app.refresh_lookup_stream(payload)
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_no_sync_running_allows_single_row_refresh_through(self) -> None:
        with (
            patch.object(app.lookup_db, "db_sync_lock_status", return_value=None),
            patch.object(app, "refresh_rows_lifecycle_chunk") as chunk_mock,
            patch.object(app, "build_evidence_entries", return_value=[]),
            patch.object(app, "_attach_matched_by"),
        ):
            result = await app.refresh_lookup_row(app.RowRefreshRequest(row=_row()))
        chunk_mock.assert_called_once()
        self.assertIn("row", result)


class VendorSyncStartBlockedByAnotherHolderTests(unittest.IsolatedAsyncioTestCase):
    def _already_held(self):
        return (
            patch.object(app.lookup_db, "db_acquire_sync_lock", return_value=None),
            patch.object(
                app.lookup_db,
                "db_sync_lock_status",
                return_value={"label": "Vendor sync: suse", "heartbeat_at": "2026-01-01T00:00:00"},
            ),
        )

    async def test_eosl_sync_blocked_and_never_runs_the_scrape(self) -> None:
        p1, p2 = self._already_held()
        with p1, p2, patch.object(app, "eosl_sync_os_database") as scrape_mock:
            with self.assertRaises(app.HTTPException) as ctx:
                await app.eosl_sync()
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Vendor sync: suse", ctx.exception.detail)
        scrape_mock.assert_not_called()

    async def test_junos_sync_blocked_and_never_runs_the_scrape(self) -> None:
        p1, p2 = self._already_held()
        with p1, p2, patch.object(app, "sync_junos_database") as scrape_mock:
            with self.assertRaises(app.HTTPException) as ctx:
                await app.junos_sync()
        self.assertEqual(ctx.exception.status_code, 409)
        scrape_mock.assert_not_called()

    async def test_suse_sync_blocked_and_never_runs_the_scrape(self) -> None:
        p1, p2 = self._already_held()
        with p1, p2, patch.object(app, "sync_suse_database") as scrape_mock:
            with self.assertRaises(app.HTTPException) as ctx:
                await app.suse_sync()
        self.assertEqual(ctx.exception.status_code, 409)
        scrape_mock.assert_not_called()

    async def test_generic_vendor_sync_blocked_and_never_runs_the_scrape(self) -> None:
        p1, p2 = self._already_held()
        with p1, p2, patch.object(app, "vendor_sync_source") as scrape_mock:
            with self.assertRaises(app.HTTPException) as ctx:
                await app.vendor_lookup_sync("eosl", None)
        self.assertEqual(ctx.exception.status_code, 409)
        scrape_mock.assert_not_called()

    async def test_streaming_vendor_sync_blocked_before_any_streaming_starts(self) -> None:
        """Must raise from vendor_lookup_sync_stream itself (before the
        StreamingResponse is even constructed) -- raising this same 409
        inside vendor_lookup_sync_events instead would fire only after its
        "started" SSE event had already gone out."""
        p1, p2 = self._already_held()
        with p1, p2:
            with self.assertRaises(app.HTTPException) as ctx:
                await app.vendor_lookup_sync_stream("eosl", None)
        self.assertEqual(ctx.exception.status_code, 409)


class VendorSyncReleasesLockOnCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_sync_releases_the_lock_it_acquired(self) -> None:
        with (
            patch.object(app.lookup_db, "db_acquire_sync_lock", return_value="holder-token-123"),
            patch.object(app.lookup_db, "db_release_sync_lock") as release_mock,
            patch.object(app, "eosl_sync_os_database", return_value={"added": 1}),
            patch.object(app, "eosl_get_status", return_value={}),
        ):
            await app.eosl_sync()
        release_mock.assert_called_once_with("holder-token-123")

    async def test_failed_sync_still_releases_the_lock(self) -> None:
        with (
            patch.object(app.lookup_db, "db_acquire_sync_lock", return_value="holder-token-456"),
            patch.object(app.lookup_db, "db_release_sync_lock") as release_mock,
            patch.object(app, "eosl_sync_os_database", side_effect=RuntimeError("scrape failed")),
        ):
            with self.assertRaises(app.HTTPException) as ctx:
                await app.eosl_sync()
        self.assertEqual(ctx.exception.status_code, 502)
        release_mock.assert_called_once_with("holder-token-456")


if __name__ == "__main__":
    unittest.main()
