"""Regression test: Refresh EOL/EOAS must never query a lifecycle source for
an Ambiguous OS row, and must leave such rows completely untouched.

Real incident this pins: an "Ambiguous OS" row's normalized_os_detailed_name
("Ambiguous OS") was used as the lookup query, which -- via the raw os_string
fallback inside the vendor cascade -- matched an unrelated product by
coincidental version-number overlap and silently wrote a wrong EOL date onto
a row that had been flagged specifically because its product is unclear.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# Importing app.py runs its module-level load_dotenv(), which can inject
# DATABASE_URL / LOOKUP_DB_ENABLED from .env into this process's os.environ,
# leaking into every test module collected afterward in the same pytest run
# (see tests/test_lookup_db_mirror.py for the same guard and full rationale).
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


class RefreshSkipsAmbiguousRowsTests(unittest.TestCase):
    def test_ambiguous_row_never_reaches_eol_or_vendor_lookup(self) -> None:
        ambiguous = _row("Windows 6.0.0 0", "Ambiguous OS", "Ambiguous OS")
        ordinary = _row("Ubuntu 24.04", "", "")

        with (
            patch.object(app, "lookup_os_eol_batch") as mock_eol_batch,
            patch.object(app, "lookup_vendor_batch") as mock_vendor_batch,
        ):
            mock_eol_batch.return_value = [
                {
                    "eol_date": "1234567890",
                    "eol_status": "",
                    "eoas_date": "",
                    "eoas_status": "",
                    "normalized_os_detailed_name": "Ubuntu 24.04 'Noble Numbat' (LTS)",
                    "normalized_os": "Ubuntu 24.04",
                }
            ]
            mock_vendor_batch.return_value = []

            evidence_by_os: dict = {}
            app.refresh_rows_lifecycle_chunk([ambiguous, ordinary], evidence_by_os)

        # Only the ordinary row's data was ever sent for lookup.
        (eol_items,), _kwargs = mock_eol_batch.call_args
        self.assertEqual(len(eol_items), 1)
        self.assertEqual(eol_items[0]["os_string"], "Ubuntu 24.04")

        # The ambiguous row is completely untouched -- no date, no status,
        # no evidence entry, exactly as it came in.
        self.assertEqual(ambiguous["eol_date"], "")
        self.assertEqual(ambiguous["eoas_date"], "")
        self.assertEqual(ambiguous["normalized_os_detailed_name"], "Ambiguous OS")
        self.assertNotIn("Windows 6.0.0 0", evidence_by_os)

        # The ordinary row was enriched normally.
        self.assertEqual(ordinary["eol_date"], "1234567890")
        self.assertIn("Ubuntu 24.04", evidence_by_os)

    def test_all_ambiguous_chunk_makes_no_lookup_calls_at_all(self) -> None:
        rows = [
            _row("Foo / Bar", "Ambiguous OS", "Ambiguous OS"),
            _row("Baz / Qux", "Ambiguous OS", "Ambiguous OS"),
        ]
        with (
            patch.object(app, "lookup_os_eol_batch") as mock_eol_batch,
            patch.object(app, "lookup_vendor_batch") as mock_vendor_batch,
        ):
            app.refresh_rows_lifecycle_chunk(rows, {})
            mock_eol_batch.assert_not_called()
            mock_vendor_batch.assert_not_called()

    def test_row_already_marked_ambiguous_keeps_prior_dates_unchanged(self) -> None:
        """If an ambiguous row somehow already had dates (e.g. leftover from
        before this fix), a later refresh must not touch them either way --
        it's still skipped entirely, not blanked out."""
        already_dated = _row("Windows 6.0.0 0", "Ambiguous OS", "Ambiguous OS", eol="999", eoas="888")
        with (
            patch.object(app, "lookup_os_eol_batch") as mock_eol_batch,
            patch.object(app, "lookup_vendor_batch") as mock_vendor_batch,
        ):
            app.refresh_rows_lifecycle_chunk([already_dated], {})
            mock_eol_batch.assert_not_called()
            mock_vendor_batch.assert_not_called()
        self.assertEqual(already_dated["eol_date"], "999")
        self.assertEqual(already_dated["eoas_date"], "888")


class ApplyLifecycleResultCorrectsStaleNamesTests(unittest.TestCase):
    """Regression: a row already carrying a normalized name (right or wrong)
    never had that name re-examined by Refresh -- only eol/eoas dates were
    ever overwritten. A confirmed release match (e.g. resolving to "23H2
    (E)") then left the OLD, wrong release-level tag ("23H2 (W)") on the row
    forever, permanently out of sync with the just-fetched dates that
    actually belong to the newly-resolved release."""

    def test_confirmed_match_overwrites_a_stale_pre_existing_name(self) -> None:
        row = _row(
            "Windows 11 Enterprise 10.0.22631",
            detailed="Microsoft Windows 11 23H2 (W)",
            normalized="Microsoft Windows 11",
        )
        result = {
            "eol_date": "1794268800",
            "eoas_date": "1794268800",
            "normalized_os_detailed_name": "Microsoft Windows 11 23H2 (E)",
            "normalized_os": "Microsoft Windows 11 23H2 (E)",
            "source": "api",
        }
        app._apply_lifecycle_result(row, result, {})
        self.assertEqual(row["normalized_os_detailed_name"], "Microsoft Windows 11 23H2 (E)")
        self.assertEqual(row["normalized_os"], "Microsoft Windows 11 23H2 (E)")

    def test_no_match_result_leaves_existing_name_untouched(self) -> None:
        row = _row(
            "Windows 11 Enterprise 10.0.22631",
            detailed="Microsoft Windows 11 23H2 (W)",
            normalized="Microsoft Windows 11",
        )
        result = {"eol_date": "", "eoas_date": "", "normalized_os_detailed_name": "", "normalized_os": "", "source": "api"}
        app._apply_lifecycle_result(row, result, {})
        self.assertEqual(row["normalized_os_detailed_name"], "Microsoft Windows 11 23H2 (W)")
        self.assertEqual(row["normalized_os"], "Microsoft Windows 11")

    def test_confirmed_match_also_writes_detailed_and_normalized_evidence(self) -> None:
        """Previously these evidence slots only got written when the row's
        field started blank -- an already-non-blank (stale) name meant the
        drawer permanently showed "No normalized value is set." even after a
        fresh, confident match. A confirmed match must always leave evidence
        behind for both slots, matching the eol slot."""
        row = _row(
            "Windows 11 Enterprise 10.0.22631",
            detailed="Microsoft Windows 11 23H2 (W)",
            normalized="Microsoft Windows 11",
        )
        result = {
            "eol_date": "1794268800",
            "eoas_date": "1794268800",
            "normalized_os_detailed_name": "Microsoft Windows 11 23H2 (E)",
            "normalized_os": "Microsoft Windows 11 23H2 (E)",
            "source": "api",
        }
        evidence_by_os: dict = {}
        app._apply_lifecycle_result(row, result, evidence_by_os)
        entry = evidence_by_os["Windows 11 Enterprise 10.0.22631"]
        self.assertEqual(entry["detailed"]["method"], "api")
        self.assertEqual(entry["normalized"]["method"], "api")


class ApplyLifecycleResultKeepsStaleDatesTests(unittest.TestCase):
    """Regression: a genuine no-match result used to unconditionally wipe
    eol_date/eol_status/eoas_date/eoas_status to blank, even when the row
    already carried perfectly good, previously-resolved dates -- unlike the
    normalized-name fields just below it, which already had this guard. A
    row that resolved fine on a past refresh would silently lose its dates
    the moment one later refresh run's cascade happened to find nothing
    (e.g. a vendor source temporarily missing, or endoflife.date briefly
    unreachable), with no error surfaced anywhere."""

    def test_no_match_result_leaves_existing_dates_untouched(self) -> None:
        row = _row(
            "Windows 11 Enterprise 10.0.22631",
            detailed="Microsoft Windows 11 23H2 (E)",
            normalized="Microsoft Windows 11 23H2",
            eol="1794268800",
            eoas="1700000000",
        )
        result = {
            "eol_date": "", "eol_status": "", "eoas_date": "", "eoas_status": "",
            "normalized_os_detailed_name": "", "normalized_os": "", "source": "api",
        }
        app._apply_lifecycle_result(row, result, {})
        self.assertEqual(row["eol_date"], "1794268800")
        self.assertEqual(row["eoas_date"], "1700000000")

    def test_confirmed_match_still_overwrites_dates_normally(self) -> None:
        """The fix must not turn this into a one-way ratchet -- a genuine new
        match still replaces old dates, including correcting them to blank
        when the fresh result explicitly reports a status but no date."""
        row = _row(
            "Windows 11 Enterprise 10.0.22631",
            eol="1111111111",
            eoas="1111111111",
        )
        result = {
            "eol_date": "1794268800", "eol_status": "", "eoas_date": "1700000000", "eoas_status": "",
            "normalized_os_detailed_name": "", "normalized_os": "", "source": "api",
        }
        app._apply_lifecycle_result(row, result, {})
        self.assertEqual(row["eol_date"], "1794268800")
        self.assertEqual(row["eoas_date"], "1700000000")

    def test_row_with_stale_dates_still_reaches_the_vendor_cascade(self) -> None:
        """The still_unresolved gate must key off this run's fresh
        endoflife.date result, not the row's (no-longer-wiped) current
        state -- otherwise a row that already has old dates would look
        "resolved" and never even get a chance at the vendor cascade when
        endoflife.date itself found nothing this run."""
        row = _row(
            "Windows Server 2019 Datacenter",
            eol="1111111111",
            eoas="1111111111",
        )
        with (
            patch.object(app, "lookup_os_eol_batch") as mock_eol_batch,
            patch.object(app, "lookup_vendor_batch") as mock_vendor_batch,
        ):
            mock_eol_batch.return_value = [
                {"eol_date": "", "eol_status": "", "eoas_date": "", "eoas_status": "",
                 "normalized_os_detailed_name": "", "normalized_os": "", "source": "api"}
            ]
            mock_vendor_batch.return_value = [
                {"eol_date": "1893456000", "eol_status": "", "eoas_date": "1704758400", "eoas_status": "",
                 "normalized_os_detailed_name": "Microsoft Windows Server 2019 (LTSC)",
                 "normalized_os": "Microsoft Windows Server 2019", "source": "eosl"}
            ]
            app.refresh_rows_lifecycle_chunk([row], {})

        mock_vendor_batch.assert_called_once()
        self.assertEqual(row["eol_date"], "1893456000")
        self.assertEqual(row["eoas_date"], "1704758400")

    def test_row_with_stale_dates_keeps_them_when_vendor_cascade_also_misses(self) -> None:
        row = _row(
            "Windows Server 2019 Datacenter",
            eol="1111111111",
            eoas="1111111111",
        )
        with (
            patch.object(app, "lookup_os_eol_batch") as mock_eol_batch,
            patch.object(app, "lookup_vendor_batch") as mock_vendor_batch,
        ):
            mock_eol_batch.return_value = [
                {"eol_date": "", "eol_status": "", "eoas_date": "", "eoas_status": "",
                 "normalized_os_detailed_name": "", "normalized_os": "", "source": "api"}
            ]
            mock_vendor_batch.return_value = [
                {"eol_date": "", "eol_status": "", "eoas_date": "", "eoas_status": "",
                 "normalized_os_detailed_name": "", "normalized_os": "", "api_note": "no match", "source": ""}
            ]
            app.refresh_rows_lifecycle_chunk([row], {})

        self.assertEqual(row["eol_date"], "1111111111")
        self.assertEqual(row["eoas_date"], "1111111111")


class AttachMatchedByTests(unittest.TestCase):
    """Regression: a row returned from Refresh/Add-OS had no matched_by
    attached at all, so the client kept whatever value the row had from the
    last full page load (or none, for a brand-new row) -- stale the moment
    the row's own evidence changed. This is what made the "Matched by"
    column filter look broken specifically on rows touched in the current
    Draft session."""

    def test_ordinary_row_gets_matched_by_from_its_own_fresh_evidence(self) -> None:
        row = _row("Ubuntu 24.04")
        evidence_by_os = {"Ubuntu 24.04": {"eol": {"method": "api"}}}
        app._attach_matched_by([row], evidence_by_os)
        self.assertEqual(row["matched_by"], "endoflife.date")

    def test_row_with_no_evidence_gets_no_match(self) -> None:
        row = _row("Some Unknown OS")
        app._attach_matched_by([row], {})
        self.assertEqual(row["matched_by"], "No match")

    def test_ambiguous_row_gets_ambiguous_regardless_of_evidence(self) -> None:
        row = _row("Foo / Bar", "Ambiguous OS", "Ambiguous OS")
        app._attach_matched_by([row], {})
        self.assertEqual(row["matched_by"], "Ambiguous")


if __name__ == "__main__":
    unittest.main()
