"""Tests for find_fuzzy_pair_match -- the local, non-AI fuzzy match against
existing normalized pairs used by Add-OS and single-row "Re-run lookup".

Real incident: the Add-OS modal's own explainer text has always claimed
"Fuzzy match against existing normalized pairs (>= 95%, vendor-gated)", but
no code ever implemented it -- a new os_string either matched an existing
pair EXACTLY or (if AI was enabled) got sent to an AI provider; anything
in between (a close variant with no AI configured) got no match at all and
fell through to a from-scratch EOL/EOAS lookup. This module closes that gap.

A first attempt reused the already-defined (but previously unused)
strict_match_percent/pair_match_percent -- a token-SUBSET-containment
check that scores a hard 0 the moment a single token doesn't appear in the
candidate's own token set, e.g. "Ubuntu 22.04.3 LTS" (has token "22.04.3")
against "Ubuntu 22.04 LTS" (only has "22.04") scores 0, not close to 95.
That's too strict to catch the near-misses this feature exists for.
pair_similarity_percent (character-level difflib.SequenceMatcher ratio,
the same technique eol_service.py's prior-value fallback already uses)
replaces it -- gated by ai_pair_acceptable (vendor/edition/version-family
checks) so a fuzzy accept is never looser than an AI accept would be.
"""

from __future__ import annotations

import unittest

from normalization_service import find_fuzzy_pair_match, pair_similarity_percent

UBUNTU_2204 = {"normalized_os_detailed_name": "Ubuntu 22.04 LTS", "normalized_os": "Ubuntu 22.04"}
UBUNTU_2004 = {"normalized_os_detailed_name": "Ubuntu 20.04 LTS", "normalized_os": "Ubuntu 20.04"}
WINDOWS_11_PRO = {"normalized_os_detailed_name": "Microsoft Windows 11 Pro", "normalized_os": "Microsoft Windows 11"}
WINDOWS_11_PRO_ENT = {
    "normalized_os_detailed_name": "Microsoft Windows 11 Pro Enterprise",
    "normalized_os": "Microsoft Windows 11",
}
ALL_PAIRS = [UBUNTU_2204, UBUNTU_2004, WINDOWS_11_PRO, WINDOWS_11_PRO_ENT]


class FindFuzzyPairMatchTests(unittest.TestCase):
    def test_case_and_whitespace_variant_matches(self) -> None:
        # Effectively identical to the existing pair once cleaned/cased --
        # this is the clear, unambiguous "should obviously match" case.
        match = find_fuzzy_pair_match("ubuntu 22.04", ALL_PAIRS)
        self.assertEqual(match, {"normalized_os_detailed_name": "Ubuntu 22.04 LTS", "normalized_os": "Ubuntu 22.04"})

    def test_a_genuinely_different_minor_version_does_not_match(self) -> None:
        """Sanity check: a real version bump (22.04 -> 22.04.3) must not
        silently reuse the OLDER pair's normalization -- it should fall
        through (to AI, or to a from-scratch lookup), not be guessed."""
        self.assertIsNone(find_fuzzy_pair_match("Ubuntu 22.04.3 LTS", ALL_PAIRS))
        self.assertIsNone(find_fuzzy_pair_match("Ubuntu 22.04.3 LTS Server", ALL_PAIRS))

    def test_a_different_major_version_never_matches_even_with_high_raw_similarity(self) -> None:
        """ai_pair_acceptable's version-family check is what actually
        protects this -- a raw character ratio alone can't reliably tell
        "22.04" from "20.04" apart (both read as "mostly the same text,
        one digit different"), so the gate must reject it regardless of
        how high pair_similarity_percent scores."""
        self.assertGreater(pair_similarity_percent("Ubuntu 20.04.3 LTS", UBUNTU_2204), 80)
        self.assertIsNone(find_fuzzy_pair_match("Ubuntu 20.04.3 LTS", [UBUNTU_2204]))

    def test_edition_drift_never_matches(self) -> None:
        """"Pro" must not silently absorb into "Pro Enterprise" or vice
        versa -- edition/SKU drift, same guard the AI step already has."""
        self.assertIsNone(find_fuzzy_pair_match("Windows 11 Pro 64-bit", ALL_PAIRS))
        self.assertIsNone(find_fuzzy_pair_match("Windows 11 Pro Enterprise 64-bit", ALL_PAIRS))

    def test_cross_vendor_never_matches(self) -> None:
        self.assertIsNone(find_fuzzy_pair_match("Rocky Linux 9.3", ALL_PAIRS))

    def test_rubbish_os_string_never_matches(self) -> None:
        self.assertIsNone(
            find_fuzzy_pair_match("4735303000b47080000000000000000000000000", ALL_PAIRS)
        )

    def test_empty_os_string_never_matches(self) -> None:
        self.assertIsNone(find_fuzzy_pair_match("", ALL_PAIRS))
        self.assertIsNone(find_fuzzy_pair_match("   ", ALL_PAIRS))

    def test_no_allowed_pairs_never_matches(self) -> None:
        self.assertIsNone(find_fuzzy_pair_match("Ubuntu 22.04", []))

    def test_threshold_is_configurable(self) -> None:
        """Sanity check the threshold parameter is actually honored --
        lowering it should let a real-but-not-95%-close variant through,
        raising it should reject even a very close one."""
        similarity = pair_similarity_percent("Ubuntu 22.04.3 LTS", UBUNTU_2204)
        self.assertLess(similarity, 95)
        self.assertIsNotNone(find_fuzzy_pair_match("Ubuntu 22.04.3 LTS", [UBUNTU_2204], threshold=similarity))
        self.assertIsNone(find_fuzzy_pair_match("ubuntu 22.04", [UBUNTU_2204], threshold=101))


if __name__ == "__main__":
    unittest.main()
