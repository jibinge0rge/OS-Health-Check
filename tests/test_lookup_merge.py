"""Tests for the 3-way publish merge (lookup_extras.merge_lookup_rows).

Scenario numbers in comments map to the Test Matrix in the "Shared,
conflict-safe publishing for the lookup data" plan.
"""

from __future__ import annotations

import unittest

from lookup_extras import merge_lookup_rows


def row(os_string: str, detailed: str = "", normalized: str = "", eol: str = "", eoas: str = "") -> dict:
    return {
        "os_string": os_string,
        "normalized_os_detailed_name": detailed,
        "normalized_os": normalized,
        "eol_date": eol,
        "eol_status": "",
        "eoas_date": eoas,
        "eoas_status": "",
    }


def evidence(os_string: str, method: str = "api") -> dict:
    return {"by_os": {os_string: {"eol": {"method": method}}}, "updated_at": ""}


EMPTY_EVIDENCE: dict = {"by_os": {}, "updated_at": ""}


class MergeLookupRowsTests(unittest.TestCase):
    def test_upstream_only_add_is_kept(self) -> None:
        # Scenario 1/2: a row published upstream that the draft never saw.
        base = []
        current = [row("Ubuntu 24.04", eol="2029-01-01")]
        draft = []
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual([r["os_string"] for r in result["merged_rows"]], ["Ubuntu 24.04"])

    def test_local_only_add_is_kept(self) -> None:
        base = []
        current = []
        draft = [row("Ubuntu 24.04", eol="2029-01-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual([r["os_string"] for r in result["merged_rows"]], ["Ubuntu 24.04"])

    def test_added_both_identical_no_conflict(self) -> None:
        # Scenario 3-ish for adds: both sides independently add the same content.
        base = []
        current = [row("New OS 1.0", eol="2030-01-01")]
        draft = [row("New OS 1.0", eol="2030-01-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(len(result["merged_rows"]), 1)

    def test_added_both_different_is_conflict(self) -> None:
        # Scenario 5.
        base = []
        current = [row("New OS 1.0", eol="2030-01-01")]
        draft = [row("New OS 1.0", eol="2031-06-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["kind"], "added_both")
        self.assertEqual(result["merged_rows"], [])

    def test_edited_only_upstream_is_kept(self) -> None:
        # Scenario 2.
        base = [row("Oracle Linux 9", eol="2026-01-01")]
        current = [row("Oracle Linux 9", eol="2032-01-01")]
        draft = [row("Oracle Linux 9", eol="2026-01-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["merged_rows"][0]["eol_date"], "2032-01-01")

    def test_edited_only_local_is_kept(self) -> None:
        base = [row("Oracle Linux 9", eol="2026-01-01")]
        current = [row("Oracle Linux 9", eol="2026-01-01")]
        draft = [row("Oracle Linux 9", eol="2033-01-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["merged_rows"][0]["eol_date"], "2033-01-01")

    def test_edited_both_identically_no_conflict(self) -> None:
        # Scenario 3: both refreshed and landed on the same answer.
        base = [row("Oracle Linux 9", eol="2026-01-01")]
        current = [row("Oracle Linux 9", eol="2032-06-01")]
        draft = [row("Oracle Linux 9", eol="2032-06-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["merged_rows"][0]["eol_date"], "2032-06-01")

    def test_edited_both_differently_is_conflict(self) -> None:
        # Scenario 4 / 9: two environments both refreshed, got different answers.
        base = [row("Oracle Linux 9", eol="2026-01-01")]
        current = [row("Oracle Linux 9", eol="2032-06-01")]
        draft = [row("Oracle Linux 9", eol="2032-09-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(len(result["conflicts"]), 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["kind"], "edited_both")
        self.assertEqual(conflict["theirs"]["row"]["eol_date"], "2032-06-01")
        self.assertEqual(conflict["mine"]["row"]["eol_date"], "2032-09-01")
        self.assertEqual(result["merged_rows"], [])

    def test_deleted_upstream_local_unchanged_respects_deletion(self) -> None:
        # Scenario 6.
        base = [row("Old OS 1.0", eol="2020-01-01")]
        current: list[dict] = []
        draft = [row("Old OS 1.0", eol="2020-01-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["merged_rows"], [])

    def test_deleted_upstream_local_edited_is_conflict(self) -> None:
        # Scenario 7.
        base = [row("Old OS 1.0", eol="2020-01-01")]
        current: list[dict] = []
        draft = [row("Old OS 1.0", eol="2021-01-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["kind"], "edited_local_deleted_upstream")
        self.assertEqual(result["merged_rows"], [])

    def test_deleted_local_upstream_unchanged_respects_deletion(self) -> None:
        base = [row("Old OS 1.0", eol="2020-01-01")]
        current = [row("Old OS 1.0", eol="2020-01-01")]
        draft: list[dict] = []
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["merged_rows"], [])

    def test_deleted_local_upstream_edited_is_conflict(self) -> None:
        base = [row("Old OS 1.0", eol="2020-01-01")]
        current = [row("Old OS 1.0", eol="2022-01-01")]
        draft: list[dict] = []
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["kind"], "edited_upstream_deleted_local")
        self.assertEqual(result["merged_rows"], [])

    def test_deleted_both_sides_no_conflict(self) -> None:
        base = [row("Old OS 1.0", eol="2020-01-01")]
        current: list[dict] = []
        draft: list[dict] = []
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["merged_rows"], [])

    def test_unchanged_row_passes_through(self) -> None:
        base = [row("Debian 12", eol="2028-01-01")]
        current = [row("Debian 12", eol="2028-01-01")]
        draft = [row("Debian 12", eol="2028-01-01")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(len(result["merged_rows"]), 1)

    def test_blank_os_string_passes_through_from_draft(self) -> None:
        base: list[dict] = []
        current: list[dict] = []
        draft = [row("", detailed="Ambiguous OS", normalized="Ambiguous OS")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(len(result["merged_rows"]), 1)
        self.assertEqual(result["merged_rows"][0]["normalized_os"], "Ambiguous OS")

    def test_duplicate_key_never_silently_dropped(self) -> None:
        # Scenario 10: real data has duplicate os_strings. A dict-keyed merge
        # would silently collapse them -- confirm every row survives, either
        # via the conflict list (no auto-write) rather than disappearing.
        base = [
            row("Ambiguous OS", detailed="A", normalized="A"),
            row("Ambiguous OS", detailed="B", normalized="B"),
        ]
        current = [
            row("Ambiguous OS", detailed="A2", normalized="A2"),
            row("Ambiguous OS", detailed="B", normalized="B"),
        ]
        draft = [
            row("Ambiguous OS", detailed="A", normalized="A"),
            row("Ambiguous OS", detailed="B2", normalized="B2"),
        ]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["merged_rows"], [])
        self.assertEqual(len(result["conflicts"]), 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["kind"], "ambiguous_duplicate")
        self.assertEqual(len(conflict["theirs"]["rows"]), 2)
        self.assertEqual(len(conflict["mine"]["rows"]), 2)

    def test_duplicate_only_in_current_still_flagged(self) -> None:
        # A duplicate introduced upstream only (draft/base have a single row)
        # must still be treated as ambiguous, not silently reduced to one row.
        base = [row("Weird OS", detailed="X", normalized="X")]
        current = [
            row("Weird OS", detailed="X", normalized="X"),
            row("Weird OS", detailed="Y", normalized="Y"),
        ]
        draft = [row("Weird OS", detailed="X", normalized="X")]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["kind"], "ambiguous_duplicate")

    def test_evidence_follows_winning_row_upstream(self) -> None:
        # Scenario 11: row changed only upstream -> evidence must come from
        # current_evidence, not stale draft_evidence.
        base = [row("Oracle Linux 9", eol="2026-01-01")]
        current = [row("Oracle Linux 9", eol="2032-06-01")]
        draft = [row("Oracle Linux 9", eol="2026-01-01")]
        current_ev = evidence("Oracle Linux 9", method="eosl")
        draft_ev = evidence("Oracle Linux 9", method="manual")
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, current_ev, draft_ev)
        merged_entry = result["merged_evidence"]["by_os"]["Oracle Linux 9"]
        self.assertEqual(merged_entry["eol"]["method"], "eosl")

    def test_evidence_follows_winning_row_local(self) -> None:
        base = [row("Oracle Linux 9", eol="2026-01-01")]
        current = [row("Oracle Linux 9", eol="2026-01-01")]
        draft = [row("Oracle Linux 9", eol="2033-01-01")]
        current_ev = evidence("Oracle Linux 9", method="eosl")
        draft_ev = evidence("Oracle Linux 9", method="manual")
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, current_ev, draft_ev)
        merged_entry = result["merged_evidence"]["by_os"]["Oracle Linux 9"]
        self.assertEqual(merged_entry["eol"]["method"], "manual")

    def test_three_way_chain_diffs_against_latest_current(self) -> None:
        # Scenario 8: C's base predates both A's and B's changes; merging
        # against the true-current (which already contains both) must not
        # treat A's or B's already-published rows as conflicts just because
        # C's draft never saw them.
        base = [
            row("Row A", eol="2020-01-01"),
            row("Row B", eol="2020-01-01"),
            row("Row C", eol="2020-01-01"),
        ]
        # current already reflects A's and B's published edits.
        current = [
            row("Row A", eol="2030-01-01"),  # A's edit, published
            row("Row B", eol="2031-01-01"),  # B's edit, published
            row("Row C", eol="2020-01-01"),  # untouched by A or B
        ]
        # C's draft (based on the original base) only touches Row C.
        draft = [
            row("Row A", eol="2020-01-01"),
            row("Row B", eol="2020-01-01"),
            row("Row C", eol="2099-01-01"),
        ]
        result = merge_lookup_rows(base, current, draft, EMPTY_EVIDENCE, EMPTY_EVIDENCE, EMPTY_EVIDENCE)
        self.assertEqual(result["conflicts"], [])
        by_os = {r["os_string"]: r["eol_date"] for r in result["merged_rows"]}
        self.assertEqual(by_os["Row A"], "2030-01-01")
        self.assertEqual(by_os["Row B"], "2031-01-01")
        self.assertEqual(by_os["Row C"], "2099-01-01")


if __name__ == "__main__":
    unittest.main()
