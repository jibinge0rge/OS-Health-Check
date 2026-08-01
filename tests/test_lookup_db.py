"""Tests for the Postgres-backed lookup storage (lookup_db.py), gated on
DATABASE_URL exactly like tests/test_layer23_switch.py gates its vendor-cache
DB tests -- skipped entirely when no database is configured."""

from __future__ import annotations

import os
import threading
import unittest
import uuid

import lookup_db


def _pg_available() -> bool:
    return bool(str(os.environ.get("DATABASE_URL") or "").strip())


def _temp_schema(prefix: str) -> str:
    return f"test_{prefix}_{uuid.uuid4().hex[:12]}"


def row(os_string: str, eol: str = "", eoas: str = "") -> dict:
    return {
        "os_string": os_string,
        "normalized_os_detailed_name": os_string,
        "normalized_os": os_string,
        "eol_date": eol,
        "eol_status": "",
        "eoas_date": eoas,
        "eoas_status": "",
    }


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class LookupDbRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        from vendor_lookups.db import drop_schema

        self.schema = _temp_schema("lookupdb")
        self.drop_schema = drop_schema
        lookup_db.ensure_schema(self.schema)

    def tearDown(self) -> None:
        self.drop_schema(self.schema)

    def test_rows_round_trip(self) -> None:
        rows = [row("Ubuntu 24.04", eol="2029-01-01"), row("Rocky Linux 9", eol="2032-01-01")]
        lookup_db.db_save_rows(rows, "data", schema=self.schema)
        loaded = lookup_db.db_load_rows("data", schema=self.schema)
        self.assertEqual(loaded, rows)

    def test_rows_round_trip_preserves_duplicates_and_order(self) -> None:
        # Real data has duplicate os_strings -- confirm the (source, row_order)
        # key preserves every row rather than collapsing by os_string.
        rows = [row("Ambiguous OS", eol="2020-01-01"), row("Ambiguous OS", eol="2021-01-01"), row("Zzz Last")]
        lookup_db.db_save_rows(rows, "data", schema=self.schema)
        loaded = lookup_db.db_load_rows("data", schema=self.schema)
        self.assertEqual(len(loaded), 3)
        self.assertEqual([r["eol_date"] for r in loaded if r["os_string"] == "Ambiguous OS"], ["2020-01-01", "2021-01-01"])
        self.assertEqual(loaded[-1]["os_string"], "Zzz Last")

    def test_save_rows_overwrites_previous_content_for_source(self) -> None:
        lookup_db.db_save_rows([row("First")], "draft", schema=self.schema)
        lookup_db.db_save_rows([row("Second")], "draft", schema=self.schema)
        loaded = lookup_db.db_load_rows("draft", schema=self.schema)
        self.assertEqual([r["os_string"] for r in loaded], ["Second"])

    def test_evidence_round_trip(self) -> None:
        evidence = {"by_os": {"Ubuntu 24.04": {"eol": {"method": "api"}}}, "updated_at": "2026-01-01T00:00:00"}
        saved = lookup_db.db_save_evidence(evidence, "data", schema=self.schema)
        self.assertEqual(saved["by_os"], evidence["by_os"])
        loaded = lookup_db.db_load_evidence("data", schema=self.schema)
        self.assertEqual(loaded["by_os"], evidence["by_os"])

    def test_load_missing_source_returns_empty(self) -> None:
        self.assertEqual(lookup_db.db_load_rows("draft", schema=self.schema), [])
        self.assertEqual(lookup_db.db_load_evidence("draft", schema=self.schema), {"by_os": {}, "updated_at": ""})

    def test_first_draft_save_stamps_based_on_revision(self) -> None:
        lookup_db.db_save_rows([row("Data Row")], "data", schema=self.schema)
        lookup_db.db_save_evidence({}, "data", schema=self.schema)
        # Publish once so data_revision moves off its 0 default -- makes the
        # assertion below meaningful (not just "still the initial value").
        lookup_db.db_publish([row("Data Row")], {}, expected_revision=0, schema=self.schema)
        revision_when_draft_starts = lookup_db.db_data_revision(schema=self.schema)

        lookup_db.db_save_rows([row("Data Row")], "draft", schema=self.schema)
        self.assertEqual(lookup_db.db_draft_based_on_revision(schema=self.schema), revision_when_draft_starts)

        # A second save of the SAME (still-existing) draft -- e.g. an
        # autosave picking up another edit -- must not re-stamp the base;
        # it should still reflect when the draft was first created. (Note:
        # publishing anything, even unrelated Data, always deletes the
        # draft -- see db_publish -- so there's no "draft survives a
        # publish" scenario to test here; that's covered by
        # test_publish_deletes_draft instead.)
        lookup_db.db_save_rows([row("Data Row edited")], "draft", schema=self.schema)
        self.assertEqual(lookup_db.db_draft_based_on_revision(schema=self.schema), revision_when_draft_starts)

    def test_delete_draft_clears_rows_evidence_and_base_revision(self) -> None:
        lookup_db.db_save_rows([row("Draft Row")], "draft", schema=self.schema)
        lookup_db.db_save_evidence({"by_os": {"Draft Row": {}}}, "draft", schema=self.schema)
        lookup_db.db_delete_draft(schema=self.schema)
        self.assertEqual(lookup_db.db_load_rows("draft", schema=self.schema), [])
        self.assertEqual(lookup_db.db_load_evidence("draft", schema=self.schema), {"by_os": {}, "updated_at": ""})
        self.assertEqual(lookup_db.db_draft_based_on_revision(schema=self.schema), 0)


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class LookupDbPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        from vendor_lookups.db import drop_schema

        self.schema = _temp_schema("lookuppub")
        self.drop_schema = drop_schema
        lookup_db.ensure_schema(self.schema)

    def tearDown(self) -> None:
        self.drop_schema(self.schema)

    def test_publish_bumps_revision_and_writes_data(self) -> None:
        result = lookup_db.db_publish([row("Oracle Linux 9", eol="2032-01-01")], {}, expected_revision=0, schema=self.schema)
        self.assertEqual(result["data_revision"], 1)
        self.assertEqual(lookup_db.db_data_revision(schema=self.schema), 1)
        self.assertEqual(lookup_db.db_load_rows("data", schema=self.schema)[0]["eol_date"], "2032-01-01")

    def test_publish_deletes_draft(self) -> None:
        lookup_db.db_save_rows([row("Draft Row")], "draft", schema=self.schema)
        lookup_db.db_publish([row("Data Row")], {}, expected_revision=0, schema=self.schema)
        self.assertEqual(lookup_db.db_load_rows("draft", schema=self.schema), [])

    def test_publish_snapshots_previous_data_into_backups(self) -> None:
        lookup_db.db_publish([row("First")], {}, expected_revision=0, schema=self.schema)
        lookup_db.db_publish([row("Second")], {}, expected_revision=1, schema=self.schema)
        with lookup_db._connect(self.schema) as connection:
            backups = connection.execute("SELECT rows_json FROM backups ORDER BY id").fetchall()
        self.assertEqual(len(backups), 2)
        # The second backup snapshots what was live just before that publish
        # (i.e. "First", not "Second") -- backups always lag one publish
        # behind, that's the point of a pre-write snapshot.
        self.assertEqual(backups[1]["rows_json"][0]["os_string"], "First")

    def test_stale_publish_is_rejected_and_writes_nothing(self) -> None:
        lookup_db.db_publish([row("First")], {}, expected_revision=0, schema=self.schema)
        with self.assertRaises(lookup_db.PublishConflictError) as ctx:
            lookup_db.db_publish([row("Stale attempt")], {}, expected_revision=0, schema=self.schema)
        self.assertEqual(ctx.exception.current_revision, 1)
        # Nothing from the rejected attempt should have landed.
        self.assertEqual(lookup_db.db_load_rows("data", schema=self.schema)[0]["os_string"], "First")
        self.assertEqual(lookup_db.db_data_revision(schema=self.schema), 1)

    def test_concurrent_publish_exactly_one_succeeds(self) -> None:
        lookup_db.db_save_rows([row("Base")], "data", schema=self.schema)
        results: list[object] = []
        lock = threading.Lock()

        def attempt(label: str) -> None:
            try:
                result = lookup_db.db_publish([row(label)], {}, expected_revision=0, schema=self.schema)
                with lock:
                    results.append(("ok", result))
            except lookup_db.PublishConflictError as exc:
                with lock:
                    results.append(("conflict", exc.current_revision))

        threads = [threading.Thread(target=attempt, args=(f"racer-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        outcomes = [kind for kind, _ in results]
        self.assertEqual(len(results), 5)
        self.assertEqual(outcomes.count("ok"), 1, f"expected exactly one winner, got: {results}")
        self.assertEqual(outcomes.count("conflict"), 4)
        self.assertEqual(lookup_db.db_data_revision(schema=self.schema), 1)


if __name__ == "__main__":
    unittest.main()
