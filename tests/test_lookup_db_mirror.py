"""Tests for LOOKUP_DB_MIRROR_FILES: DB-mode publish optionally also writing
_data/eol_lookup.csv / _data/eol_lookup_evidence.json / _data/.revision.

Everything that would touch a real Postgres connection (lookup_db.db_publish,
db_draft_based_on_revision) is mocked -- this only exercises app.perform_publish's
branching and the file-writing side effect, not lookup_db.py itself (see
tests/test_lookup_db.py for that, gated on a real DATABASE_URL).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Importing app.py runs its module-level load_dotenv(), which can inject
# DATABASE_URL / LOOKUP_DB_ENABLED from .env into this process's os.environ.
# That would leak into every test module collected afterward in the same
# pytest run (e.g. flipping their `_pg_available()`-style skip guards from
# skip to "try to actually connect" against an unreachable host) -- so
# snapshot and restore the environment around the import.
_ENV_SNAPSHOT = dict(os.environ)
import app  # noqa: E402

os.environ.clear()
os.environ.update(_ENV_SNAPSHOT)


def _row(os_string: str, eol: str = "", eoas: str = "") -> app.LookupRow:
    return app.LookupRow(
        os_string=os_string,
        normalized_os_detailed_name=os_string,
        normalized_os=os_string,
        eol_date=eol,
        eol_status="",
        eoas_date=eoas,
        eoas_status="",
    )


class PerformPublishDbMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp_path = Path(self.tmp.name)

        self.data_path = tmp_path / "_data" / "eol_lookup.csv"
        self.evidence_path = tmp_path / "_data" / "eol_lookup_evidence.json"
        self.revision_path = tmp_path / "_data" / ".revision"
        self.backup_dir = tmp_path / "_backup"

        for target, value in (
            ("_USE_DB", True),
            ("DATA_PATH", self.data_path),
            ("DATA_EVIDENCE_PATH", self.evidence_path),
            ("DATA_REVISION_PATH", self.revision_path),
            ("BACKUP_DIR", self.backup_dir),
        ):
            patcher = patch.object(app, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _payload(self) -> app.LookupPayload:
        return app.LookupPayload(
            rows=[_row("Ubuntu 24.04", eol="2029-01-01")],
            evidence={"by_os": {}, "updated_at": ""},
            backup_suffix="",
        )

    def _mock_db(self, *, revision: int = 1):
        return (
            patch.object(app.lookup_db, "db_draft_based_on_revision", return_value=0),
            patch.object(
                app.lookup_db,
                "db_publish",
                return_value={
                    "data_revision": revision,
                    "row_count": 1,
                    "evidence": {"by_os": {}, "updated_at": "2026-01-01T00:00:00"},
                },
            ),
        )

    def test_mirror_disabled_leaves_files_untouched(self) -> None:
        based_on, publish = self._mock_db()
        with patch.object(app, "_MIRROR_FILES", False), based_on, publish:
            result = app.perform_publish(self._payload())
        self.assertEqual(result["backup_path"], "")
        self.assertEqual(result["evidence_backup_path"], "")
        self.assertFalse(self.data_path.exists())
        self.assertFalse(self.evidence_path.exists())
        self.assertFalse(self.revision_path.exists())

    def test_mirror_enabled_writes_csv_evidence_and_revision(self) -> None:
        based_on, publish = self._mock_db(revision=1)
        with patch.object(app, "_MIRROR_FILES", True), based_on, publish:
            result = app.perform_publish(self._payload())

        self.assertTrue(self.data_path.exists())
        self.assertTrue(self.evidence_path.exists())
        self.assertEqual(self.revision_path.read_text(encoding="utf-8"), "1")
        self.assertIn("Ubuntu 24.04", self.data_path.read_text(encoding="utf-8"))
        # First-ever mirror write: nothing existed yet to back up.
        self.assertEqual(result["backup_path"], "")
        self.assertEqual(result["evidence_backup_path"], "")

    def test_mirror_enabled_backs_up_previous_content(self) -> None:
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_path.write_text("stale content", encoding="utf-8")
        self.evidence_path.write_text('{"by_os": {}, "updated_at": ""}', encoding="utf-8")

        based_on, publish = self._mock_db(revision=2)
        with patch.object(app, "_MIRROR_FILES", True), based_on, publish:
            result = app.perform_publish(self._payload())

        self.assertNotEqual(result["backup_path"], "")
        self.assertNotEqual(result["evidence_backup_path"], "")
        self.assertTrue(Path(result["backup_path"]).exists())
        self.assertIn("Ubuntu 24.04", self.data_path.read_text(encoding="utf-8"))
        self.assertNotIn("stale content", self.data_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
