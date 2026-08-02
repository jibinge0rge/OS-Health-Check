"""Regression test: refreshing a selected subset of rows (bulk "Refresh
lifecycle", or the toolbar's "Refresh EOL/EOAS" with a selection active) must
merge the refreshed rows back into the existing draft/data, not replace the
entire source with just that subset.

Real incident this pins: a user filtered to "Missing normalization", selected
just those rows, ran Refresh EOL/EOAS, made some edits, and published --
5,509 rows collapsed down to ~150. lookup_refresh_events saved whatever
`rows` it was given as the *entire* new content for `source`, discarding
every row (and its evidence) that wasn't part of the selection.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

_ENV_SNAPSHOT = dict(os.environ)
import app  # noqa: E402

os.environ.clear()
os.environ.update(_ENV_SNAPSHOT)


def _row(os_string: str, detailed: str = "", normalized: str = "", eol: str = "", eoas: str = "") -> dict:
    return {
        "os_string": os_string,
        "normalized_os_detailed_name": detailed,
        "normalized_os": normalized,
        "eol_date": eol,
        "eol_status": "",
        "eoas_date": eoas,
        "eoas_status": "",
    }


class RefreshSubsetMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshing_a_selected_subset_keeps_every_other_draft_row(self) -> None:
        row_a = _row("Ubuntu 24.04", "Ubuntu 24.04 LTS", "Ubuntu 24.04")
        row_b = _row("Windows Server 2019")  # the one row actually selected for refresh
        row_c = _row("SUSE Linux Enterprise Server 15", "SUSE Linux Enterprise Server 15", "SUSE Linux Enterprise Server 15")
        existing_draft = [row_a, dict(row_b), row_c]
        existing_evidence = {
            "by_os": {
                "Ubuntu 24.04": {"eol": {"method": "api"}},
                "SUSE Linux Enterprise Server 15": {"eol": {"method": "suse"}},
            },
            "updated_at": "",
        }

        saved = {}

        def fake_save_rows(rows, source):
            saved["rows"] = [r.model_dump() for r in rows]
            saved["source"] = source

        def fake_save_evidence(evidence, source):
            saved["evidence"] = evidence
            return evidence

        def fake_lifecycle_chunk(rows, evidence_by_os, product_cache=None):
            # Simulate a real hit for row B only -- mirrors
            # refresh_rows_lifecycle_chunk's actual effect on the row dict
            # and evidence_by_os without touching real lookup services.
            for row in rows:
                if row["os_string"] == "Windows Server 2019":
                    row["eol_date"] = "1893456000"
                    row["normalized_os_detailed_name"] = "Microsoft Windows Server 2019 (LTSC)"
                    row["normalized_os"] = "Microsoft Windows Server 2019"
                    evidence_by_os["Windows Server 2019"] = {"eol": {"method": "eosl"}}

        with (
            patch.object(app, "load_rows", return_value=existing_draft),
            patch.object(app, "save_rows", side_effect=fake_save_rows),
            patch.object(app, "save_evidence", side_effect=fake_save_evidence),
            patch.object(app, "_source_exists", return_value=True),
            patch.object(app, "refresh_rows_lifecycle_chunk", side_effect=fake_lifecycle_chunk),
        ):
            events = [
                event
                async for event in app.lookup_refresh_events(
                    [row_b], existing_evidence, "draft", __import__("threading").Event()
                )
            ]

        self.assertTrue(any('"type": "complete"' in e for e in events))

        # The critical assertion: all 3 original rows survive the save, not
        # just the one that was refreshed.
        saved_os_strings = {r["os_string"] for r in saved["rows"]}
        self.assertEqual(saved_os_strings, {"Ubuntu 24.04", "Windows Server 2019", "SUSE Linux Enterprise Server 15"})

        # The refreshed row's own update actually landed.
        saved_b = next(r for r in saved["rows"] if r["os_string"] == "Windows Server 2019")
        self.assertEqual(saved_b["eol_date"], "1893456000")

        # Untouched rows are byte-for-byte the same as before.
        saved_a = next(r for r in saved["rows"] if r["os_string"] == "Ubuntu 24.04")
        self.assertEqual(saved_a["normalized_os_detailed_name"], "Ubuntu 24.04 LTS")
        saved_c = next(r for r in saved["rows"] if r["os_string"] == "SUSE Linux Enterprise Server 15")
        self.assertEqual(saved_c["normalized_os"], "SUSE Linux Enterprise Server 15")

        # Evidence for the untouched rows must also survive -- it was
        # previously pruned down to just the refreshed subset too.
        saved_by_os = saved["evidence"]["by_os"]
        self.assertIn("Ubuntu 24.04", saved_by_os)
        self.assertIn("SUSE Linux Enterprise Server 15", saved_by_os)
        self.assertIn("Windows Server 2019", saved_by_os)

    async def test_full_draft_refresh_with_no_selection_is_unaffected(self) -> None:
        """When `rows` already equals the whole draft (no selection active),
        the merge must be a no-op -- same rows in, same rows out."""
        row_a = _row("Ubuntu 24.04")
        row_b = _row("Windows Server 2019")
        whole_draft = [row_a, row_b]

        saved = {}

        with (
            patch.object(app, "load_rows", return_value=whole_draft),
            patch.object(app, "save_rows", side_effect=lambda rows, source: saved.update(rows=[r.model_dump() for r in rows])),
            patch.object(app, "save_evidence", side_effect=lambda evidence, source: evidence),
            patch.object(app, "_source_exists", return_value=True),
            patch.object(app, "refresh_rows_lifecycle_chunk"),
        ):
            async for _ in app.lookup_refresh_events(
                whole_draft, {"by_os": {}, "updated_at": ""}, "draft", __import__("threading").Event()
            ):
                pass

        self.assertEqual({r["os_string"] for r in saved["rows"]}, {"Ubuntu 24.04", "Windows Server 2019"})


if __name__ == "__main__":
    unittest.main()
