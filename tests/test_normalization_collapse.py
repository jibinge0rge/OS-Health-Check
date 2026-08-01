"""Tests for collapse_consecutive_duplicate_words (single word + phrase)."""

from __future__ import annotations

import unittest

from normalization_service import collapse_consecutive_duplicate_words


class CollapseConsecutiveDuplicateWordsTests(unittest.TestCase):
    def test_single_word_duplicate(self) -> None:
        self.assertEqual(
            collapse_consecutive_duplicate_words("Apple macOS macOS 26 (Tahoe)"),
            "Apple macOS 26 (Tahoe)",
        )

    def test_multi_word_phrase_duplicate(self) -> None:
        self.assertEqual(
            collapse_consecutive_duplicate_words(
                "Microsoft Windows Server Windows Server 2019 (LTSC)"
            ),
            "Microsoft Windows Server 2019 (LTSC)",
        )
        self.assertEqual(
            collapse_consecutive_duplicate_words("Windows Server Windows Server 23H2 AC"),
            "Windows Server 23H2 AC",
        )

    def test_no_duplicate_is_unchanged(self) -> None:
        self.assertEqual(
            collapse_consecutive_duplicate_words("Microsoft Windows 11 26H1 (E)"),
            "Microsoft Windows 11 26H1 (E)",
        )
        self.assertEqual(
            collapse_consecutive_duplicate_words("Ubuntu 24.04 'Noble Numbat' (LTS)"),
            "Ubuntu 24.04 'Noble Numbat' (LTS)",
        )

    def test_blank_and_whitespace(self) -> None:
        self.assertEqual(collapse_consecutive_duplicate_words(""), "")
        self.assertEqual(collapse_consecutive_duplicate_words(None), "")
        self.assertEqual(collapse_consecutive_duplicate_words("   "), "")

    def test_case_insensitive_match(self) -> None:
        self.assertEqual(
            collapse_consecutive_duplicate_words("Windows Server windows server 2019"),
            "Windows Server 2019",
        )


if __name__ == "__main__":
    unittest.main()
