"""Tests for the generic Windows/Linux fallback name written when a
product genuinely resolves (we know the vendor/family) but no specific
release can be pinned down -- see _generic_family_fallback_name,
lookup_os_eol, and the round-trip guard in pick_api_os_value_with_field.

Real incidents this closes:
- "Windows Vista / Windows 2008 / Windows 7 / Windows 2012" (four genuine
  Windows generations, no single one confirmable -- and, since the
  ambiguous-OS heuristic now exempts same-family lists, this reaches
  lookup_os_eol as a normal row) used to leave normalized_os/detailed_name
  blank on refusal.
- "Windows 4.0.8 8" / "Windows 4.0.5 5" (Windows NT 4.0 isn't tracked in
  the catalog at all) -- same blank-on-refusal outcome.

Policy (explicitly confirmed): Windows + Linux only, never overwrites a
row that already has SOME normalized value (even a wrong/stale one) --
only fills in rows that are currently blank.
"""

from __future__ import annotations

import unittest

from eol_service import (
    _GENERIC_FAMILY_FALLBACK_NAMES,
    _generic_family_fallback_name,
    lookup_os_eol,
    pick_api_os_value_with_field,
)

WINDOWS_PRODUCT = {"label": "Microsoft Windows", "tags": ["microsoft", "os", "windows"]}
WINDOWS_SERVER_PRODUCT = {"label": "Microsoft Windows Server", "tags": ["microsoft", "os", "windows"]}
UBUNTU_PRODUCT = {"label": "Ubuntu", "tags": ["linux-distribution", "os"]}
LINUX_KERNEL_PRODUCT = {"label": "Linux Kernel", "tags": ["linux-foundation", "os"]}
MACOS_PRODUCT = {"label": "Apple macOS", "tags": ["apple", "os"]}


class GenericFamilyFallbackNameTests(unittest.TestCase):
    def test_windows_tagged_products_get_microsoft_windows(self) -> None:
        self.assertEqual(_generic_family_fallback_name(WINDOWS_PRODUCT), "Microsoft Windows")
        self.assertEqual(_generic_family_fallback_name(WINDOWS_SERVER_PRODUCT), "Microsoft Windows")

    def test_linux_tagged_products_get_linux_os(self) -> None:
        """Covers both catalog conventions: distros are tagged
        "linux-distribution", the kernel project itself is tagged
        "linux-foundation" -- neither is literally "linux"."""
        self.assertEqual(_generic_family_fallback_name(UBUNTU_PRODUCT), "Linux OS")
        self.assertEqual(_generic_family_fallback_name(LINUX_KERNEL_PRODUCT), "Linux OS")

    def test_unrelated_families_get_no_fallback(self) -> None:
        self.assertIsNone(_generic_family_fallback_name(MACOS_PRODUCT))

    def test_missing_or_malformed_tags_get_no_fallback(self) -> None:
        self.assertIsNone(_generic_family_fallback_name({"label": "Something"}))
        self.assertIsNone(_generic_family_fallback_name({"label": "Something", "tags": "not-a-list"}))


class LookupOsEolGenericFallbackTests(unittest.TestCase):
    """End-to-end through lookup_os_eol, using a pre-populated product_cache
    so no network call happens."""

    WINDOWS_SERVER_RELEASES = [
        {"name": "2008-sp2", "label": "Windows Server 2008 SP2",
         "eolFrom": "2020-01-14", "latest": {"name": "6.0.6003"}},
        {"name": "2012", "label": "Windows Server 2012 (LTSC)",
         "eolFrom": "2023-10-10", "latest": {"name": "6.2.9200"}},
        {"name": "2012-r2", "label": "Windows Server 2012 R2 (LTSC)",
         "eolFrom": "2023-10-10", "latest": {"name": "6.3.9600"}},
    ]
    WINDOWS_CLIENT_RELEASES = [
        {"name": "8", "label": "8", "eolFrom": "2016-01-12", "latest": {"name": "6.2.9200"}},
        {"name": "5-sp3", "label": "XP SP3", "eolFrom": "2014-04-08", "latest": {"name": "5.1.2600"}},
    ]

    def _cache(self, slug: str, product_result: dict, releases: list[dict]) -> dict:
        return {slug: {"result": {**product_result, "releases": releases}}}

    def test_mixed_windows_generations_falls_back_to_microsoft_windows(self) -> None:
        """The exact reported string, once no longer marked Ambiguous OS --
        resolves to windows-server, ties two genuinely different, disjoint
        generations (2008 vs 2012), refuses to pick one, and -- since both
        incoming normalized fields are blank -- lands on the generic name."""
        cache = self._cache("windows-server", WINDOWS_SERVER_PRODUCT, self.WINDOWS_SERVER_RELEASES)
        result = lookup_os_eol(
            "Windows Vista / Windows 2008 / Windows 7 / Windows 2012",
            "",
            "",
            frozenset({"windows", "windows-server"}),
            cache,
        )
        self.assertEqual(result["normalized_os_detailed_name"], "Microsoft Windows")
        self.assertEqual(result["normalized_os"], "Microsoft Windows")
        self.assertEqual(result["eol_date"], "")

    def test_bare_hint_coincidence_falls_back_to_microsoft_windows(self) -> None:
        cache = self._cache("windows", WINDOWS_PRODUCT, self.WINDOWS_CLIENT_RELEASES)
        result = lookup_os_eol(
            "Windows 4.0.8 8", "", "", frozenset({"windows"}), cache,
        )
        self.assertEqual(result["normalized_os_detailed_name"], "Microsoft Windows")
        self.assertEqual(result["normalized_os"], "Microsoft Windows")

    def test_existing_normalized_value_is_never_overwritten(self) -> None:
        """Explicitly confirmed policy: only fill blank fields -- a row
        that already has SOME value (even a stale/wrong one from before
        this feature existed) must be left completely untouched."""
        cache = self._cache("windows-server", WINDOWS_SERVER_PRODUCT, self.WINDOWS_SERVER_RELEASES)
        result = lookup_os_eol(
            "Windows Vista / Windows 2008 / Windows 7 / Windows 2012",
            "Some Stale Prior Value",
            "Some Stale Prior Value",
            frozenset({"windows", "windows-server"}),
            cache,
        )
        self.assertEqual(result["normalized_os_detailed_name"], "")
        self.assertEqual(result["normalized_os"], "")

    def test_genuine_release_match_is_unaffected(self) -> None:
        """Sanity check: a query that DOES resolve to one specific release
        must never take the fallback path at all."""
        cache = self._cache("windows-server", WINDOWS_SERVER_PRODUCT, self.WINDOWS_SERVER_RELEASES)
        result = lookup_os_eol(
            "Windows Server 2012 R2 Standard", "", "", frozenset({"windows", "windows-server"}), cache,
        )
        self.assertEqual(result["normalized_os"], "Microsoft Windows Server 2012")
        self.assertNotEqual(result["eol_date"], "")


class GenericFallbackNeverTrustedAsQueryValueTests(unittest.TestCase):
    """The written placeholder must never be preferred as a query value on
    a LATER refresh -- "Linux OS" contains the bare word "linux", a real,
    specific product (the Linux Kernel project) in the phrase index;
    trusting it would silently re-resolve the row to the kernel's own
    release catalog instead of staying the deliberately vague placeholder
    it was written as. Every later refresh must re-derive fresh from
    os_string every time, exactly as if the field were still blank."""

    def test_microsoft_windows_placeholder_falls_through_to_os_string(self) -> None:
        value, field = pick_api_os_value_with_field(
            "Windows 4.0.8 8", "Microsoft Windows", "Microsoft Windows"
        )
        self.assertEqual((value, field), ("Windows 4.0.8 8", "os_string"))

    def test_linux_os_placeholder_falls_through_to_os_string(self) -> None:
        value, field = pick_api_os_value_with_field(
            "AlmaLinux 9.7 5 14", "Linux OS", "Linux OS"
        )
        self.assertEqual((value, field), ("AlmaLinux 9.7 5 14", "os_string"))

    def test_case_insensitive(self) -> None:
        value, field = pick_api_os_value_with_field("Windows 4.0.8 8", "linux os", "MICROSOFT WINDOWS")
        self.assertEqual(field, "os_string")

    def test_a_real_normalized_value_is_still_preferred_as_before(self) -> None:
        """Sanity check: the guard is scoped to exactly these two literal
        placeholder strings -- a real, specific normalized value must keep
        being preferred over os_string exactly as before."""
        value, field = pick_api_os_value_with_field(
            "Oracle Linux Server 9.5", "Oracle Linux 9", "Oracle Linux 9"
        )
        self.assertEqual((value, field), ("Oracle Linux 9", "normalized_os"))

    def test_fallback_names_match_the_guarded_set(self) -> None:
        """Keeps _generic_family_fallback_name's two literal return values
        in sync with the set pick_api_os_value_with_field guards against."""
        self.assertEqual(_GENERIC_FAMILY_FALLBACK_NAMES, frozenset({"microsoft windows", "linux os"}))
