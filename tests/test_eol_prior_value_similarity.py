"""Regression tests for the prior-value-similarity fallback in eol_service.py.

Real incident this pins: endoflife.date's own catalog gets more precise over
time -- a SUSE Linux Enterprise Server release once tracked generically as
"15" is later split into per-service-pack releases like "15.2". A row that
was previously resolved against the old, coarser name has `normalized_os` /
`normalized_os_detailed_name` = "...15", and a refresh's extracted hints are
just a bare "15" -- which correctly scores 0 against the now-only
multi-part "15.2" release (pick_release's "bare major must not guess" rule),
so the row went permanently unresolved despite endoflife.date clearly still
tracking that exact OS, just under a more specific name.

`_pick_release_by_prior_value` is the fallback: when ordinary hint scoring
finds nothing, but exactly one release's prospective new name is a near-exact
(>=95%) match to what the row already had on record, adopt endoflife.date's
fresh (more specific) name and dates -- but refuse, same as pick_release's own
tie-breaks, when the catalog has more than one similarly-named candidate
(genuine ambiguity) or when there's no prior value to anchor to at all.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import eol_service
from eol_service import _pick_release_by_prior_value


SLES_15_2_ONLY = [
    {
        "name": "15.2",
        "label": "15.2",
        "eolFrom": "2021-12-31",
        "eoasFrom": "2022-12-31",
        "isEol": False,
        "isEoas": False,
    },
]

SLES_MULTIPLE_SERVICE_PACKS = [
    {"name": "15.1", "label": "15.1", "eolFrom": "2021-01-31"},
    {"name": "15.2", "label": "15.2", "eolFrom": "2021-12-31"},
    {"name": "15.3", "label": "15.3", "eolFrom": "2022-12-31"},
]


class PickReleaseByPriorValueTests(unittest.TestCase):
    def test_single_close_rename_is_accepted(self) -> None:
        picked = _pick_release_by_prior_value(
            SLES_15_2_ONLY,
            "SUSE Linux Enterprise Server",
            "SUSE Linux Enterprise Server 15",
            "SUSE Linux Enterprise Server 15",
        )
        self.assertEqual(picked.get("name"), "15.2")

    def test_multiple_similarly_named_candidates_refuse_rather_than_guess(self) -> None:
        """Genuine ambiguity -- several service packs are all a close
        textual match to the old bare "15" -- must not silently pick one."""
        picked = _pick_release_by_prior_value(
            SLES_MULTIPLE_SERVICE_PACKS,
            "SUSE Linux Enterprise Server",
            "SUSE Linux Enterprise Server 15",
            "SUSE Linux Enterprise Server 15",
        )
        self.assertEqual(picked, {})

    def test_no_prior_value_refuses(self) -> None:
        """A brand-new, never-matched row has nothing to anchor a rename
        to -- must go through ordinary hint scoring or nothing at all."""
        picked = _pick_release_by_prior_value(SLES_15_2_ONLY, "SUSE Linux Enterprise Server", "", "")
        self.assertEqual(picked, {})

    def test_placeholder_prior_value_refuses(self) -> None:
        picked = _pick_release_by_prior_value(SLES_15_2_ONLY, "SUSE Linux Enterprise Server", "-", "<!-- default -->")
        self.assertEqual(picked, {})

    def test_genuinely_different_prior_value_refuses(self) -> None:
        """A prior value naming an unrelated release must not be force-matched
        just because it's the only release in the list."""
        picked = _pick_release_by_prior_value(
            SLES_15_2_ONLY,
            "SUSE Linux Enterprise Server",
            "SUSE Linux Enterprise Server 11",
            "SUSE Linux Enterprise Server 11",
        )
        self.assertEqual(picked, {})

    def test_an_unrelated_version_that_merely_looks_similar_as_text_refuses(self) -> None:
        """Real, reported incident: a row's prior value was "Apple iOS 27"
        (an invalid/future version someone typed). Release "7" (iOS 7, from
        2013) scores a 95.65% *text* similarity against "Apple iOS 27" --
        purely because "Apple iOS 7" is one character shorter than "Apple
        iOS 27" (SequenceMatcher's ratio rewards the shorter total-length
        pairing) -- while every other, equally plausible release ("17",
        "20".."26") scores under 92%, comfortably below the bar. "27" and
        "7" have no genuine "15" -> "15.2"-style prefix/extension
        relationship at all; the old text-only check would have confidently
        (and wrongly) rewritten the row to iOS 7's decade-old EOL/EOAS
        dates. Confirms the fix via the real numbers: text similarity DOES
        clear 95% here, and the fallback must still refuse."""
        ios_catalog = [{"name": str(n), "label": str(n)} for n in range(4, 27)]
        similarity = eol_service._text_similarity("Apple iOS 27", "Apple iOS 7")
        self.assertGreaterEqual(similarity, 0.95)
        picked = _pick_release_by_prior_value(
            ios_catalog, "Apple iOS", "Apple iOS 27", "Apple iOS 27"
        )
        self.assertEqual(picked, {})

    def test_a_genuine_bare_to_dotted_extension_is_still_plausible(self) -> None:
        """Sanity check _is_plausible_version_extension itself: the
        genuine SUSE-style relationship ("15" is a numeric prefix of
        "15.2") must still be recognized, in either direction."""
        release = {"name": "15.2"}
        self.assertTrue(eol_service._is_plausible_version_extension("SUSE ... 15", release))
        coarser_release = {"name": "15"}
        self.assertTrue(eol_service._is_plausible_version_extension("SUSE ... 15.2", coarser_release))

    def test_two_unrelated_bare_numbers_are_not_a_plausible_extension(self) -> None:
        self.assertFalse(eol_service._is_plausible_version_extension("Apple iOS 27", {"name": "7"}))
        self.assertFalse(eol_service._is_plausible_version_extension("Apple iOS 27", {"name": "17"}))

    def test_a_non_numeric_compound_release_name_is_not_blocked(self) -> None:
        """The version-extension check only applies when the release's own
        name is cleanly numeric -- a compound slug (e.g. Windows Server's
        "2008-sp2") can't be parsed this way, so it's left to the existing
        text-similarity check alone, unaffected by this fix."""
        self.assertTrue(
            eol_service._is_plausible_version_extension("Windows Server 2008", {"name": "2008-sp2"})
        )


FAKE_SLES_PRODUCT = {
    "result": {
        "label": "SUSE Linux Enterprise Server",
        "releases": SLES_15_2_ONLY,
    }
}


class LookupOsEolPriorValueFallbackIntegrationTests(unittest.TestCase):
    """End-to-end through lookup_os_eol: confirms the fallback actually wires
    up to adopt endoflife.date's fresh name/dates over the row's stale one."""

    def _patched(self):
        return (
            patch.object(eol_service, "get_valid_slugs", return_value=frozenset({"sles"})),
            patch.object(eol_service, "resolve_product_slug", return_value="sles"),
            patch.object(eol_service, "fetch_product", return_value=FAKE_SLES_PRODUCT),
        )

    def test_catalog_renamed_release_is_adopted_over_stale_stored_value(self) -> None:
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            result = eol_service.lookup_os_eol(
                os_string="SUSE Linux Enterprise Server 15",
                normalized_os_detailed_name="SUSE Linux Enterprise Server 15",
                normalized_os="SUSE Linux Enterprise Server 15",
                valid_slugs=frozenset({"sles"}),
                product_cache={},
            )

        self.assertEqual(result["normalized_os_detailed_name"], "SUSE Linux Enterprise Server 15.2")
        self.assertEqual(result["normalized_os"], "SUSE Linux Enterprise Server 15.2")
        self.assertTrue(result["eol_date"])
        self.assertEqual(result["api_note"], "")

    def test_without_a_prior_value_still_refuses_as_before(self) -> None:
        """Sanity check: a brand-new row (no prior normalized value) querying
        against the same catalog still correctly goes unresolved -- the
        fallback must never turn into "guess the only release available"."""
        p1, p2, p3 = self._patched()
        with p1, p2, p3:
            result = eol_service.lookup_os_eol(
                os_string="SUSE Linux Enterprise Server 15",
                normalized_os_detailed_name="",
                normalized_os="",
                valid_slugs=frozenset({"sles"}),
                product_cache={},
            )

        self.assertEqual(result["eol_date"], "")
        self.assertEqual(result["api_note"], "No matching release found in endoflife.date product data")


if __name__ == "__main__":
    unittest.main()
