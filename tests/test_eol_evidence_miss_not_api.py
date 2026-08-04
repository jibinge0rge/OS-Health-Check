"""Real, reported incident: a completely fictional os_string (nothing could
possibly resolve it -- no product, no vendor fallback, nothing) showed
"Matched by: endoflife.date" with evidence reading "Filled from the
endoflife.date lookup -- query: ...", as if it had genuinely resolved
something.

Root cause: `build_eol_evidence_slot` defaulted `method` to "api" whenever
`result.get("source")` was empty -- and the endoflife.date direct-API path
NEVER sets a "source" key at all (only the vendor cascade does, e.g.
"eosl"/"junos"), so this default fired unconditionally on every miss too,
not just every hit. `app.py::_apply_lifecycle_result` writes this slot
after the endoflife.date attempt regardless of hit or miss (only the
*vendor cascade* skips writing on ITS OWN miss) -- so a row that missed
absolutely everywhere kept the wrongly-defaulted "api" method from that
first, unconditional write, and `row_matched_by` read it as a genuine
"endoflife.date" match instead of "No match".
"""

from __future__ import annotations

import unittest

from lookup_extras import build_eol_evidence_slot, row_matched_by

_EMPTY_MISS_RESULT = {
    "eol_date": "",
    "eol_status": "",
    "eoas_date": "",
    "eoas_status": "",
    "normalized_os_detailed_name": "",
    "normalized_os": "",
    "api_note": "Product not found in endoflife.date registry",
    "query_used": "Totally Fictional OS Vendor Zephyr 42",
    "query_field": "os_string",
    "product_slug": "",
    "release_name": "",
    "release_label": "",
}


class BuildEolEvidenceSlotMissTests(unittest.TestCase):
    def test_a_complete_miss_gets_no_method_not_api(self) -> None:
        slot = build_eol_evidence_slot(_EMPTY_MISS_RESULT)
        self.assertEqual(slot["method"], "")

    def test_a_complete_miss_reports_no_match_overall(self) -> None:
        slot = build_eol_evidence_slot(_EMPTY_MISS_RESULT)
        self.assertEqual(row_matched_by({"eol": slot}, {}), "No match")

    def test_a_genuine_endoflife_hit_still_gets_method_api(self) -> None:
        """Sanity check the fix doesn't over-correct: a real hit (a
        resolved date, still no explicit "source" key -- the direct API
        path never sets one) must still report as an "api" match."""
        hit_result = dict(_EMPTY_MISS_RESULT, eol_date="1735689600", api_note="")
        slot = build_eol_evidence_slot(hit_result)
        self.assertEqual(slot["method"], "api")
        self.assertEqual(row_matched_by({"eol": slot}, {}), "endoflife.date")

    def test_a_hit_with_only_a_normalized_name_and_no_date_still_counts(self) -> None:
        """A confirmed release match can carry only a normalized name with
        no date at all yet still be a genuine hit -- e.g. a release with
        isEol/isEoas resolved as an explicit status stored elsewhere, or
        (as here) just a name filled in without dates yet available."""
        hit_result = dict(_EMPTY_MISS_RESULT, normalized_os="Ubuntu 22.04", api_note="")
        slot = build_eol_evidence_slot(hit_result)
        self.assertEqual(slot["method"], "api")

    def test_a_vendor_hit_still_uses_its_own_source_not_api(self) -> None:
        """Sanity check: the vendor cascade's own explicit "source" (e.g.
        "eosl") must still take priority over the "api" default -- this
        fix only changes what happens when source is empty."""
        vendor_result = dict(_EMPTY_MISS_RESULT, eol_date="1735689600", api_note="", source="eosl")
        slot = build_eol_evidence_slot(vendor_result)
        self.assertEqual(slot["method"], "eosl")
        self.assertEqual(row_matched_by({"eol": slot}, {}), "eosl.date")

    def test_a_vendor_result_with_explicit_empty_source_and_no_data_is_still_no_match(self) -> None:
        """The vendor cascade's own empty-result template sets source=""
        explicitly (vendor_lookup_service._empty_vendor_result) -- must be
        treated identically to a missing source key, not accidentally
        treated as "confirmed empty-string source"."""
        vendor_miss = dict(_EMPTY_MISS_RESULT, source="")
        slot = build_eol_evidence_slot(vendor_miss)
        self.assertEqual(slot["method"], "")
        self.assertEqual(row_matched_by({"eol": slot}, {}), "No match")


if __name__ == "__main__":
    unittest.main()
