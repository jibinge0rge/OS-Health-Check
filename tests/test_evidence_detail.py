"""Tests for lookup_extras.build_evidence_entries's detail text.

Previously the drawer only ever showed a generic sentence like "Filled from
the eosl.date local vendor lifecycle database." with no way to tell which
of that source's products/releases the row actually matched, even though
build_eol_evidence_slot already captured query_used/product_slug/release_label.
These pin that the specific matched record now shows up in the detail text.
"""

from __future__ import annotations

import unittest

from lookup_extras import build_evidence_entries, row_matched_by


def _eol_entry(method: str, **extra: object) -> dict:
    slot = {
        "method": method,
        "queryUsed": "",
        "queryField": "normalized_os",
        "productSlug": "",
        "releaseName": "",
        "releaseLabel": "",
        "apiNote": "",
        "score": None,
        "got": {},
    }
    slot.update(extra)
    return {"eol": slot}


class VendorAndApiDetailShowsMatchedRecordTests(unittest.TestCase):
    def test_vendor_lookup_detail_names_the_matched_release(self) -> None:
        entry = _eol_entry(
            "microsoft-lifecycle",
            queryUsed="Ambiguous OS",
            productSlug="windows",
            releaseLabel="Windows Mobile 6",
        )
        result = build_evidence_entries(entry, {})
        detail = result["entries"][0]["detail"]
        self.assertIn("Windows Mobile 6", detail)
        self.assertIn("Ambiguous OS", detail)
        self.assertIn("Microsoft Lifecycle", detail)

    def test_api_detail_names_the_matched_release(self) -> None:
        entry = _eol_entry(
            "api",
            queryUsed="Ubuntu 24.04",
            productSlug="ubuntu",
            releaseLabel="24.04 'Noble Numbat' (LTS)",
        )
        result = build_evidence_entries(entry, {})
        detail = result["entries"][0]["detail"]
        self.assertIn("24.04 'Noble Numbat' (LTS)", detail)
        self.assertIn("Ubuntu 24.04", detail)
        self.assertIn("endoflife.date", detail)

    def test_falls_back_to_product_slug_when_no_release_label(self) -> None:
        entry = _eol_entry("eosl", queryUsed="Cisco IOS XE 17.9", productSlug="cisco-ios-xe")
        result = build_evidence_entries(entry, {})
        detail = result["entries"][0]["detail"]
        self.assertIn("cisco-ios-xe", detail)

    def test_no_extra_fields_falls_back_to_plain_sentence(self) -> None:
        entry = _eol_entry("api")
        result = build_evidence_entries(entry, {})
        detail = result["entries"][0]["detail"]
        self.assertEqual(detail, "Filled from the endoflife.date lookup.")


class LegacyLookupFallbackTests(unittest.TestCase):
    """Regression: a real incident row had eol.method == "lookup-fallback"
    (a retired mechanism -- no current code path writes it) and the drawer
    showed "No match evidence recorded" / Matched-by "No match", flatly
    contradicting the recorded fallbackFrom row right there in the same
    evidence slot. lookup-fallback must describe itself honestly and count
    as a real (if legacy) match, not "no match"."""

    def test_detail_names_the_source_row_and_flags_it_as_retired(self) -> None:
        entry = _eol_entry(
            "lookup-fallback",
            queryUsed="Microsoft Windows 10 22H2 / Microsoft Windows 10",
            fallbackFrom={"os_string": "Microsoft Windows 10 Enterprise 19045"},
        )
        result = build_evidence_entries(entry, {})
        detail = result["entries"][0]["detail"]
        self.assertIn("Microsoft Windows 10 Enterprise 19045", detail)
        self.assertIn("Retired method", detail)
        self.assertNotEqual(detail, "No match evidence recorded.")

    def test_matched_by_is_not_no_match(self) -> None:
        entry = {"eol": {"method": "lookup-fallback"}}
        self.assertEqual(row_matched_by(entry, {}), "Fuzzy")


class StaleNormalizationEvidenceTests(unittest.TestCase):
    """Regression: rows normalized before per-field evidence existed store
    {"method": "none"} for detailed/normalized even though the row itself
    has a real value -- "No normalized value is set." is then flatly false,
    since the drawer shows that same value right above the evidence list."""

    def test_none_method_with_real_row_value_is_not_reported_as_unset(self) -> None:
        entry = {"detailed": {"method": "none"}, "normalized": {"method": "none"}}
        row = {
            "normalized_os_detailed_name": "Microsoft Windows 10 22H2",
            "normalized_os": "Microsoft Windows 10",
        }
        result = build_evidence_entries(entry, row)
        details = {e["field"]: e["detail"] for e in result["entries"]}
        self.assertNotIn("No normalized value is set.", details["Normalized OS detailed name"])
        self.assertNotIn("No normalized value is set.", details["Normalized OS"])
        self.assertIn("predates evidence tracking", details["Normalized OS detailed name"])

    def test_none_method_with_blank_row_value_still_reports_unset(self) -> None:
        entry = {"detailed": {"method": "none"}}
        row = {"normalized_os_detailed_name": ""}
        result = build_evidence_entries(entry, row)
        self.assertEqual(result["entries"][0]["detail"], "No normalized value is set.")


if __name__ == "__main__":
    unittest.main()
