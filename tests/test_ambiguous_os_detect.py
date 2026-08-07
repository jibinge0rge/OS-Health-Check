"""Tests for the local, non-AI ambiguous-OS heuristic in
detect_ambiguous_os_batch / _looks_like_multi_os_list.

Real incident #1: ambiguous-OS detection previously had NO fallback at all
when no AI provider was configured (detect_ambiguous_os_batch returned all
False the moment provider_api_key_configured() failed) -- so an os_string
as obviously ambiguous as "EulerOS / Ubuntu / Fedora" was never flagged, and
fell straight through to a normal (and likely wrong) lifecycle lookup. The
fix: a '/' with whitespace on BOTH sides is, by real-world inventory-string
convention, a deliberate "list of separate things" separator -- unlike a
'/' glued directly onto surrounding words/digits, which is virtually always
part of a single product's own name, version path, or model range. This
heuristic runs unconditionally, before AI, so detection no longer silently
does nothing without an AI key.

Real incident #2: "Windows Vista / Windows 2008 / Windows 7 / Windows 2012"
(four genuine Windows generations, no single one confirmable) was flagged
Ambiguous OS and permanently skipped from lifecycle enrichment. Unlike
"EulerOS / Ubuntu / Fedora" (three unrelated, non-interchangeable distros)
or "AIX 5.x / AIX 6.x / Sidewinder G2" (a third, unrelated vendor mixed in),
every segment here names the SAME broad family -- this isn't genuinely
ambiguous the same way, it's a real product where we just can't tell which
specific generation. So a same-family multi-segment string is deliberately
exempted and falls through to a normal lookup instead, which refuses to
pick one specific release on its own (see eol_service.py) and lands on the
generic family fallback name ("Microsoft Windows") rather than a dead-end
"Ambiguous OS" label.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from normalization_service import _looks_like_multi_os_list, detect_ambiguous_os_batch


class LooksLikeMultiOsListTests(unittest.TestCase):
    def test_genuinely_different_vendors_are_ambiguous(self) -> None:
        """These are the app's own documented positive examples (see the
        system prompt in detect_ambiguous_os_batch) -- all use a spaced
        slash AND span genuinely different, non-interchangeable
        vendors/products, so the local heuristic must catch every one of
        them too, without needing an AI call at all."""
        for os_string in (
            "AIX 5.x / AIX 6.x / Sidewinder G2",
            "Cisco IOS 12.1 / Cisco IOS 12.2",
            "EulerOS / Ubuntu / Fedora",
        ):
            with self.subTest(os_string=os_string):
                self.assertTrue(_looks_like_multi_os_list(os_string))

    def test_same_windows_family_is_not_ambiguous(self) -> None:
        """Real incident: four genuine Windows generations, no single one
        confirmable -- this used to be flagged Ambiguous OS (a dead end);
        now it's exempted so a normal lookup can fall back to the generic
        "Microsoft Windows" name instead."""
        self.assertFalse(
            _looks_like_multi_os_list("Windows Vista / Windows 2008 / Windows 7 / Windows 2012")
        )
        self.assertFalse(
            _looks_like_multi_os_list(
                "Windows Vista / Windows 2008 / Windows 7 / Windows 2012 / "
                "Windows Vista / Windows 2008 / Windows 7 / Windows 2012"
            )
        )

    def test_same_linux_family_is_not_ambiguous(self) -> None:
        self.assertFalse(_looks_like_multi_os_list("Linux 5.10 / Linux 6.1"))

    def test_mixed_families_are_still_ambiguous(self) -> None:
        """Sanity check: the same-family exemption must not swallow a
        genuinely mixed list just because ONE segment happens to start
        with a recognized keyword."""
        self.assertTrue(_looks_like_multi_os_list("Windows 10 / Linux 6.1"))
        self.assertTrue(_looks_like_multi_os_list("Windows 7 / Ubuntu 20.04"))

    def test_every_ambiguous_false_example_from_the_ai_prompt(self) -> None:
        """These are the app's own documented negative examples -- each
        uses a '/' glued directly onto surrounding words/digits (no
        whitespace on both sides), so the heuristic must never fire on any
        of them -- a false positive here would wrongly block a real,
        single-product row from ever being enriched."""
        for os_string in (
            "Debian GNU/Linux 10",
            "FreeBSD/12.2-STABLE",
            "Canon LBP245/246/248 /P",
            "EPSON 11a/b/g/n & 10/100 Print Server",
            "FUJIFILM Apeos C325/328 dw",
        ):
            with self.subTest(os_string=os_string):
                self.assertFalse(_looks_like_multi_os_list(os_string))

    def test_no_slash_at_all(self) -> None:
        self.assertFalse(_looks_like_multi_os_list("Microsoft Windows 10 22H2"))

    def test_single_spaced_slash_segment_pair_of_different_vendors_is_ambiguous(self) -> None:
        self.assertTrue(_looks_like_multi_os_list("Ubuntu 20.04 / Fedora 39"))

    def test_blank_string_is_not_ambiguous(self) -> None:
        self.assertFalse(_looks_like_multi_os_list(""))


class DetectAmbiguousOsBatchLocalHeuristicTests(unittest.TestCase):
    """detect_ambiguous_os_batch must apply the local heuristic
    unconditionally -- before, and independent of, whether an AI provider
    is configured -- so these results hold even when no API key exists."""

    def test_flags_a_genuinely_mixed_vendor_list_with_no_ai_configured(self) -> None:
        with patch("normalization_service.provider_api_key_configured", return_value=False):
            results = detect_ambiguous_os_batch(["EulerOS / Ubuntu / Fedora"])
        self.assertEqual(results, [True])

    def test_never_calls_ai_for_strings_the_heuristic_already_resolved(self) -> None:
        """No point spending an AI call on something already confidently
        determined -- and if AI were consulted and disagreed, that would
        silently undo a fix users are relying on."""
        with patch("normalization_service.provider_api_key_configured") as mock_configured:
            results = detect_ambiguous_os_batch(["EulerOS / Ubuntu / Fedora"])
            mock_configured.assert_not_called()
        self.assertEqual(results, [True])

    def test_non_ambiguous_and_slash_free_strings_stay_false_without_ai(self) -> None:
        with patch("normalization_service.provider_api_key_configured", return_value=False):
            results = detect_ambiguous_os_batch(
                [
                    "Microsoft Windows 10 22H2",
                    "Debian GNU/Linux 10",
                    "Windows Vista / Windows 2008 / Windows 7 / Windows 2012",
                ]
            )
        self.assertEqual(results, [False, False, False])

    def test_mixed_batch_only_sends_unresolved_items_to_ai(self) -> None:
        with (
            patch("normalization_service.provider_api_key_configured", return_value=True),
            patch("normalization_service.complete_json") as mock_complete,
        ):
            # item_index 1 -- the original position of "Debian GNU/Linux
            # 10", NOT re-numbered within the (single-item) AI batch.
            mock_complete.return_value = {"results": [{"item_index": 1, "ambiguous": True}]}
            results = detect_ambiguous_os_batch(
                [
                    "EulerOS / Ubuntu / Fedora",  # heuristic: True
                    "Debian GNU/Linux 10",  # not caught by heuristic -- goes to AI
                ]
            )
            # Only the second (unresolved) item is sent to the model.
            sent_items = mock_complete.call_args[0][1]
            self.assertIn('"Debian GNU/Linux 10"', sent_items)
            self.assertNotIn("EulerOS", sent_items)
        self.assertEqual(results, [True, True])
