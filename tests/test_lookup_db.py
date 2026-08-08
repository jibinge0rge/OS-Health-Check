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


# Fixture identity for the per-user Draft tests -- these tables key on
# (deployment_id, owner_user_id), not on the raw Keycloak sub (see iam_db.py);
# the exact strings don't matter here, only that they're stable within a test.
DEPLOYMENT_ID = "test-deployment"
USER_ID = "test-user-1"
OTHER_USER_ID = "test-user-2"


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

    def test_evidence_round_trip(self) -> None:
        evidence = {"by_os": {"Ubuntu 24.04": {"eol": {"method": "api"}}}, "updated_at": "2026-01-01T00:00:00"}
        saved = lookup_db.db_save_evidence(evidence, "data", schema=self.schema)
        self.assertEqual(saved["by_os"], evidence["by_os"])
        loaded = lookup_db.db_load_evidence("data", schema=self.schema)
        self.assertEqual(loaded["by_os"], evidence["by_os"])

    def test_load_missing_source_returns_empty(self) -> None:
        self.assertEqual(lookup_db.db_load_draft_rows(DEPLOYMENT_ID, USER_ID, schema=self.schema), [])
        self.assertEqual(
            lookup_db.db_load_draft_evidence(DEPLOYMENT_ID, USER_ID, schema=self.schema),
            {"by_os": {}, "updated_at": ""},
        )


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class LookupDbDraftTests(unittest.TestCase):
    """Per-user Draft (docs/AUTH_MULTITENANCY_PLAN.md §6) -- draft_rows/
    draft_evidence/draft_meta, keyed by (deployment_id, owner_user_id)."""

    def setUp(self) -> None:
        from vendor_lookups.db import drop_schema

        self.schema = _temp_schema("lookupdraft")
        self.drop_schema = drop_schema
        lookup_db.ensure_schema(self.schema)

    def tearDown(self) -> None:
        self.drop_schema(self.schema)

    def test_save_rows_overwrites_previous_content(self) -> None:
        lookup_db.db_save_draft_rows([row("First")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        lookup_db.db_save_draft_rows([row("Second")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        loaded = lookup_db.db_load_draft_rows(DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual([r["os_string"] for r in loaded], ["Second"])

    def test_two_users_drafts_are_isolated(self) -> None:
        lookup_db.db_save_draft_rows([row("User 1's row")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        lookup_db.db_save_draft_rows([row("User 2's row")], DEPLOYMENT_ID, OTHER_USER_ID, schema=self.schema)
        self.assertEqual(
            [r["os_string"] for r in lookup_db.db_load_draft_rows(DEPLOYMENT_ID, USER_ID, schema=self.schema)],
            ["User 1's row"],
        )
        self.assertEqual(
            [r["os_string"] for r in lookup_db.db_load_draft_rows(DEPLOYMENT_ID, OTHER_USER_ID, schema=self.schema)],
            ["User 2's row"],
        )
        lookup_db.db_delete_draft(DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual(lookup_db.db_load_draft_rows(DEPLOYMENT_ID, USER_ID, schema=self.schema), [])
        self.assertEqual(
            [r["os_string"] for r in lookup_db.db_load_draft_rows(DEPLOYMENT_ID, OTHER_USER_ID, schema=self.schema)],
            ["User 2's row"],
        )

    def test_first_draft_save_stamps_based_on_revision(self) -> None:
        lookup_db.db_save_rows([row("Data Row")], "data", schema=self.schema)
        lookup_db.db_save_evidence({}, "data", schema=self.schema)
        # Publish once so data_revision moves off its 0 default -- makes the
        # assertion below meaningful (not just "still the initial value").
        lookup_db.db_publish([row("Data Row")], {}, 0, DEPLOYMENT_ID, USER_ID, schema=self.schema)
        revision_when_draft_starts = lookup_db.db_data_revision(schema=self.schema)

        lookup_db.db_save_draft_rows([row("Data Row")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual(
            lookup_db.db_draft_based_on_revision(DEPLOYMENT_ID, USER_ID, schema=self.schema),
            revision_when_draft_starts,
        )

        # A second save of the SAME (still-existing) draft -- e.g. an
        # autosave picking up another edit -- must not re-stamp the base;
        # it should still reflect when the draft was first created. (Note:
        # publishing anything, even unrelated Data, always deletes this
        # user's draft -- see db_publish -- so there's no "draft survives a
        # publish" scenario to test here; that's covered by
        # test_publish_deletes_draft instead.)
        lookup_db.db_save_draft_rows([row("Data Row edited")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual(
            lookup_db.db_draft_based_on_revision(DEPLOYMENT_ID, USER_ID, schema=self.schema),
            revision_when_draft_starts,
        )

    def test_delete_draft_clears_rows_evidence_and_base_revision(self) -> None:
        lookup_db.db_save_draft_rows([row("Draft Row")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        lookup_db.db_save_draft_evidence({"by_os": {"Draft Row": {}}}, DEPLOYMENT_ID, USER_ID, schema=self.schema)
        lookup_db.db_delete_draft(DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual(lookup_db.db_load_draft_rows(DEPLOYMENT_ID, USER_ID, schema=self.schema), [])
        self.assertEqual(
            lookup_db.db_load_draft_evidence(DEPLOYMENT_ID, USER_ID, schema=self.schema),
            {"by_os": {}, "updated_at": ""},
        )
        self.assertEqual(lookup_db.db_draft_based_on_revision(DEPLOYMENT_ID, USER_ID, schema=self.schema), 0)

    def test_fetch_lookup_view_draft_and_data(self) -> None:
        lookup_db.db_save_rows([row("Published")], "data", schema=self.schema)
        lookup_db.db_save_evidence({"by_os": {"Published": {}}}, "data", schema=self.schema)
        lookup_db.db_save_draft_rows([row("Draft")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        lookup_db.db_save_draft_evidence({"by_os": {"Draft": {}}}, DEPLOYMENT_ID, USER_ID, schema=self.schema)

        data_view = lookup_db.db_fetch_lookup_view("data", DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual([r["os_string"] for r in data_view["rows"]], ["Published"])
        self.assertTrue(data_view["draft_exists"])
        self.assertNotIn("based_on_revision", data_view)

        draft_view = lookup_db.db_fetch_lookup_view("draft", DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual([r["os_string"] for r in draft_view["rows"]], ["Draft"])
        self.assertTrue(draft_view["draft_exists"])
        self.assertIn("based_on_revision", draft_view)

        empty = lookup_db.db_fetch_lookup_view("draft", DEPLOYMENT_ID, OTHER_USER_ID, schema=self.schema)
        self.assertEqual(empty["rows"], [])
        self.assertFalse(empty["draft_exists"])
        self.assertNotIn("based_on_revision", empty)

    def test_bulk_draft_insert_preserves_order_at_scale(self) -> None:
        # Exercises executemany chunking (_INSERT_CHUNK=500) without needing
        # a remote Azure RTT -- correctness at ~1.2k rows is enough.
        rows = [row(f"OS-{i:04d}", eol=f"2030-01-{(i % 28) + 1:02d}") for i in range(1200)]
        lookup_db.db_save_draft_rows(rows, DEPLOYMENT_ID, USER_ID, schema=self.schema)
        loaded = lookup_db.db_load_draft_rows(DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual(len(loaded), 1200)
        self.assertEqual(loaded[0]["os_string"], "OS-0000")
        self.assertEqual(loaded[500]["os_string"], "OS-0500")
        self.assertEqual(loaded[-1]["os_string"], "OS-1199")

    def test_fetch_diff_inputs_short_circuits_when_no_draft(self) -> None:
        lookup_db.db_save_rows([row("Published")], "data", schema=self.schema)
        self.assertIsNone(lookup_db.db_fetch_diff_inputs(DEPLOYMENT_ID, USER_ID, schema=self.schema))
        lookup_db.db_save_draft_rows([row("Draft")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        bundle = lookup_db.db_fetch_diff_inputs(DEPLOYMENT_ID, USER_ID, schema=self.schema)
        assert bundle is not None
        self.assertEqual([r["os_string"] for r in bundle["data_rows"]], ["Published"])
        self.assertEqual([r["os_string"] for r in bundle["draft_rows"]], ["Draft"])


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class MigrateLegacyGlobalDraftTests(unittest.TestCase):
    """The pre-cutover single-global-draft rows lived in rows/evidence
    (source='draft') -- migrate_legacy_global_draft_if_present retires them
    into a backups snapshot so nothing is silently lost (plan §9)."""

    def setUp(self) -> None:
        from vendor_lookups.db import drop_schema

        self.schema = _temp_schema("legacydraft")
        self.drop_schema = drop_schema
        lookup_db.ensure_schema(self.schema)

    def tearDown(self) -> None:
        self.drop_schema(self.schema)

    def test_migrates_legacy_draft_into_backups_and_drops_it(self) -> None:
        with lookup_db._connect(self.schema) as connection:
            connection.execute(
                "INSERT INTO rows (source, row_order, os_string) VALUES ('draft', 0, %s)", ("Legacy Draft Row",)
            )

        migrated = lookup_db.migrate_legacy_global_draft_if_present(schema=self.schema)
        self.assertTrue(migrated)
        self.assertEqual(lookup_db.db_load_rows("draft", schema=self.schema), [])

        with lookup_db._connect(self.schema) as connection:
            backups = connection.execute(
                "SELECT rows_json FROM backups WHERE suffix = 'legacy-global-draft-migration'"
            ).fetchall()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["rows_json"][0]["os_string"], "Legacy Draft Row")

    def test_is_a_no_op_when_there_is_no_legacy_draft(self) -> None:
        self.assertFalse(lookup_db.migrate_legacy_global_draft_if_present(schema=self.schema))


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
        result = lookup_db.db_publish(
            [row("Oracle Linux 9", eol="2032-01-01")], {}, 0, DEPLOYMENT_ID, USER_ID, schema=self.schema
        )
        self.assertEqual(result["data_revision"], 1)
        self.assertEqual(lookup_db.db_data_revision(schema=self.schema), 1)
        self.assertEqual(lookup_db.db_load_rows("data", schema=self.schema)[0]["eol_date"], "2032-01-01")

    def test_publish_deletes_only_the_publishing_users_draft(self) -> None:
        lookup_db.db_save_draft_rows([row("User 1's draft row")], DEPLOYMENT_ID, USER_ID, schema=self.schema)
        lookup_db.db_save_draft_rows([row("User 2's draft row")], DEPLOYMENT_ID, OTHER_USER_ID, schema=self.schema)
        lookup_db.db_publish([row("Data Row")], {}, 0, DEPLOYMENT_ID, USER_ID, schema=self.schema)
        self.assertEqual(lookup_db.db_load_draft_rows(DEPLOYMENT_ID, USER_ID, schema=self.schema), [])
        # A different user's own in-progress draft in the same deployment is
        # untouched by someone else's publish (docs/AUTH_MULTITENANCY_PLAN.md §6.3).
        self.assertEqual(
            [r["os_string"] for r in lookup_db.db_load_draft_rows(DEPLOYMENT_ID, OTHER_USER_ID, schema=self.schema)],
            ["User 2's draft row"],
        )

    def test_publish_records_who_published(self) -> None:
        lookup_db.db_publish(
            [row("First")], {}, 0, DEPLOYMENT_ID, USER_ID, published_by_user_id=USER_ID, schema=self.schema
        )
        with lookup_db._connect(self.schema) as connection:
            backup = connection.execute("SELECT published_by_user_id FROM backups ORDER BY id").fetchone()
        self.assertEqual(backup["published_by_user_id"], USER_ID)

    def test_publish_snapshots_previous_data_into_backups(self) -> None:
        lookup_db.db_publish([row("First")], {}, 0, DEPLOYMENT_ID, USER_ID, schema=self.schema)
        lookup_db.db_publish([row("Second")], {}, 1, DEPLOYMENT_ID, USER_ID, schema=self.schema)
        with lookup_db._connect(self.schema) as connection:
            backups = connection.execute("SELECT rows_json FROM backups ORDER BY id").fetchall()
        self.assertEqual(len(backups), 2)
        # The second backup snapshots what was live just before that publish
        # (i.e. "First", not "Second") -- backups always lag one publish
        # behind, that's the point of a pre-write snapshot.
        self.assertEqual(backups[1]["rows_json"][0]["os_string"], "First")

    def test_stale_publish_is_rejected_and_writes_nothing(self) -> None:
        lookup_db.db_publish([row("First")], {}, 0, DEPLOYMENT_ID, USER_ID, schema=self.schema)
        with self.assertRaises(lookup_db.PublishConflictError) as ctx:
            lookup_db.db_publish([row("Stale attempt")], {}, 0, DEPLOYMENT_ID, USER_ID, schema=self.schema)
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
                result = lookup_db.db_publish([row(label)], {}, 0, DEPLOYMENT_ID, USER_ID, schema=self.schema)
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


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class ImportFromFilesIfEmptyTests(unittest.TestCase):
    """Tests for the docker/entrypoint.sh auto-import hook -- loads
    _data/eol_lookup.csv into Postgres once, on first startup, without a
    separate manual migration step. Patches lookup_db._read_files_data_source
    (rather than touching the real _data/ directory) so these stay hermetic
    and independent of whatever's actually checked into this repo."""

    def setUp(self) -> None:
        from unittest.mock import patch

        from vendor_lookups.db import drop_schema

        self.schema = _temp_schema("importhook")
        self.drop_schema = drop_schema
        lookup_db.ensure_schema(self.schema)
        self.fake_rows = [row("Ubuntu 24.04", eol="2029-01-01")]
        self.fake_evidence = {"by_os": {"Ubuntu 24.04": {"eol": {"method": "api"}}}, "updated_at": ""}
        self.patcher = patch.object(
            lookup_db, "_read_files_data_source", return_value=(self.fake_rows, self.fake_evidence)
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def tearDown(self) -> None:
        self.drop_schema(self.schema)

    def test_imports_when_the_data_source_is_empty(self) -> None:
        imported = lookup_db.import_from_files_if_empty(schema=self.schema)
        self.assertTrue(imported)
        self.assertEqual(lookup_db.db_load_rows("data", schema=self.schema), self.fake_rows)
        self.assertEqual(lookup_db.db_load_evidence("data", schema=self.schema)["by_os"], self.fake_evidence["by_os"])

    def test_is_a_no_op_once_data_already_exists(self) -> None:
        """The core safety property: never overwrite real published/
        imported data on a later container restart."""
        lookup_db.db_save_rows([row("Already Published")], "data", schema=self.schema)
        imported = lookup_db.import_from_files_if_empty(schema=self.schema)
        self.assertFalse(imported)
        loaded = lookup_db.db_load_rows("data", schema=self.schema)
        self.assertEqual([r["os_string"] for r in loaded], ["Already Published"])

    def test_is_a_no_op_when_there_is_nothing_to_import(self) -> None:
        from unittest.mock import patch

        self.patcher.stop()
        with patch.object(lookup_db, "_read_files_data_source", return_value=([], {})):
            imported = lookup_db.import_from_files_if_empty(schema=self.schema)
        self.patcher.start()
        self.assertFalse(imported)
        self.assertEqual(lookup_db.db_load_rows("data", schema=self.schema), [])

    def test_running_it_twice_is_idempotent(self) -> None:
        self.assertTrue(lookup_db.import_from_files_if_empty(schema=self.schema))
        self.assertFalse(lookup_db.import_from_files_if_empty(schema=self.schema))
        self.assertEqual(lookup_db.db_load_rows("data", schema=self.schema), self.fake_rows)


if __name__ == "__main__":
    unittest.main()
