"""Tests for shared version train matching."""

from __future__ import annotations

import unittest

from eol_service import extract_version_hints, pick_release
from suse_service import _release_score
from version_match import score_release_against_hint


class VersionMatchTests(unittest.TestCase):
    def test_numeric_leading_zero_train_match(self) -> None:
        self.assertEqual(score_release_against_hint("17.9", "17.09.08"), 90)
        self.assertEqual(score_release_against_hint("17.6", "17.06.03"), 90)
        self.assertEqual(score_release_against_hint("17.3", "17.03.04"), 90)
        self.assertEqual(score_release_against_hint("11.2", "11.2.10"), 90)
        self.assertEqual(score_release_against_hint("7", "7.4"), 90)

    def test_different_trains_do_not_match(self) -> None:
        self.assertLess(score_release_against_hint("17.9", "17.10.01"), 80)
        self.assertLess(score_release_against_hint("11.2", "11.3.1"), 80)
        # Bare major hint must not match a finer API release.
        self.assertEqual(score_release_against_hint("11.4", "11"), 0)
        # Coarser API release still matches a finer OS hint.
        self.assertEqual(score_release_against_hint("11", "11.4"), 90)

    def test_exact_match(self) -> None:
        self.assertEqual(score_release_against_hint("22.04", "22.04"), 100)

    def test_ios_xe_pick_release(self) -> None:
        releases = [
            {"name": "17.9"},
            {"name": "17.6"},
            {"name": "17.3"},
            {"name": "16.12"},
        ]
        cases = {
            "Cisco IOS-XE 17.09.08": "17.9",
            "Cisco IOS-XE 17.06.05": "17.6",
            "Cisco IOS-XE 17.03.04a": "17.3",
        }
        for os_name, expected in cases.items():
            with self.subTest(os_name=os_name):
                picked = pick_release(releases, extract_version_hints(os_name))
                self.assertEqual(picked.get("name"), expected)

    def test_ios_xe_missing_legacy_trains_stay_unmatched(self) -> None:
        releases = [{"name": "17.9"}, {"name": "16.12"}]
        self.assertEqual(
            pick_release(releases, extract_version_hints("Cisco IOS-XE 03.06.03E")),
            {},
        )
        self.assertEqual(
            pick_release(releases, extract_version_hints("Cisco IOS-XE 16.06.04")),
            {},
        )

    def test_suse_sp_and_dotted_numeric_matching(self) -> None:
        self.assertEqual(_release_score("11 SP3", "11 SP3"), 100)
        self.assertEqual(_release_score("11 SP3", "11.03"), 100)
        self.assertEqual(_release_score("11 SP3", "11.3"), 100)
        self.assertEqual(_release_score("11 SP3", "11"), 0)
        self.assertEqual(_release_score("11 SP4", "11 SP3"), 0)


if __name__ == "__main__":
    unittest.main()
