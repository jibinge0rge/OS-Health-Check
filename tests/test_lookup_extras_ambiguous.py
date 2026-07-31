"""Tests for lookup_extras.is_ambiguous_row and row_matched_by."""

from __future__ import annotations

import unittest

from lookup_extras import is_ambiguous_row, row_matched_by


class IsAmbiguousRowTests(unittest.TestCase):
    def test_marked_ambiguous_row(self) -> None:
        row = {
            "os_string": "Foo / Bar",
            "normalized_os_detailed_name": "Ambiguous OS",
            "normalized_os": "Ambiguous OS",
        }
        self.assertTrue(is_ambiguous_row(row))

    def test_case_insensitive(self) -> None:
        row = {"normalized_os_detailed_name": "ambiguous os"}
        self.assertTrue(is_ambiguous_row(row))
        row2 = {"normalized_os_detailed_name": "  AMBIGUOUS OS  "}
        self.assertTrue(is_ambiguous_row(row2))

    def test_ordinary_row_is_not_ambiguous(self) -> None:
        row = {
            "os_string": "Ubuntu 24.04",
            "normalized_os_detailed_name": "Ubuntu 24.04 'Noble Numbat' (LTS)",
            "normalized_os": "Ubuntu 24.04",
        }
        self.assertFalse(is_ambiguous_row(row))

    def test_blank_or_missing_field(self) -> None:
        self.assertFalse(is_ambiguous_row({}))
        self.assertFalse(is_ambiguous_row({"normalized_os_detailed_name": ""}))
        self.assertFalse(is_ambiguous_row({"normalized_os_detailed_name": None}))

    def test_contains_but_does_not_equal_is_not_ambiguous(self) -> None:
        # Must be an exact match, not a substring -- a real product name
        # that happens to contain "ambiguous" (unlikely, but be strict).
        row = {"normalized_os_detailed_name": "Ambiguous OS Something Else"}
        self.assertFalse(is_ambiguous_row(row))


class RowMatchedByAmbiguousTests(unittest.TestCase):
    def test_ambiguous_row_reports_ambiguous_even_with_no_evidence(self) -> None:
        """No code path actually writes a method="ambiguous" evidence entry
        (it's set directly on the row, not via a lookup) -- without checking
        the row itself, this would misreport "No match" instead."""
        row = {"normalized_os_detailed_name": "Ambiguous OS", "normalized_os": "Ambiguous OS"}
        self.assertEqual(row_matched_by(None, row), "Ambiguous")
        self.assertEqual(row_matched_by({}, row), "Ambiguous")

    def test_ambiguous_row_reports_ambiguous_even_with_stale_evidence(self) -> None:
        """A row later marked ambiguous must report Ambiguous even if it
        still carries a leftover evidence entry from before that (e.g. a
        prior successful api match) -- ambiguous status always wins."""
        row = {"normalized_os_detailed_name": "Ambiguous OS", "normalized_os": "Ambiguous OS"}
        stale_evidence = {"eol": {"method": "api"}}
        self.assertEqual(row_matched_by(stale_evidence, row), "Ambiguous")

    def test_ordinary_row_uses_evidence_as_before(self) -> None:
        row = {"normalized_os_detailed_name": "Ubuntu 24.04 (LTS)", "normalized_os": "Ubuntu 24.04"}
        self.assertEqual(row_matched_by({"eol": {"method": "api"}}, row), "endoflife.date")
        self.assertEqual(row_matched_by(None, row), "No match")

    def test_no_row_argument_still_works(self) -> None:
        """row is optional -- existing call sites that don't have a row
        handy keep working exactly as before."""
        self.assertEqual(row_matched_by({"eol": {"method": "eosl"}}), "eosl.date")
        self.assertEqual(row_matched_by(None), "No match")


if __name__ == "__main__":
    unittest.main()
