"""Tests for get_product_catalog's category filtering.

Real incident: endoflife.date's catalog covers far more than operating
systems -- languages, frameworks, databases, hardware devices, etc. --
distinguished only by a "category" field. Apple's "ipad" product
(category "device", tracking hardware generations) shares the bare word
"ipad" with every real-world "iPad <version>"-style os_string, and had no
alias to disambiguate it from "ipados" (category "os", the actual
software lifecycle) -- it won product resolution purely because "ipad" is
also its own slug/label. get_product_catalog now filters to category "os"
only, and _INVENTORY_PHRASE_EXTRAS maps the bare "ipad" phrase to "ipados"
now that the ambiguity is gone.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import eol_service
from eol_service import get_product_catalog, resolve_product_slug


def _fake_response(products: list[dict]) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"result": products}
    return response


class GetProductCatalogCategoryFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        get_product_catalog.cache_clear()
        eol_service.get_slug_index.cache_clear()
        self.addCleanup(get_product_catalog.cache_clear)
        self.addCleanup(eol_service.get_slug_index.cache_clear)

    def test_only_os_category_products_are_kept(self) -> None:
        products = [
            {"name": "ios", "label": "Apple iOS", "category": "os", "aliases": []},
            {"name": "ipad", "label": "Apple iPad", "category": "device", "aliases": []},
            {"name": "ipados", "label": "Apple iPadOS", "category": "os", "aliases": []},
            {"name": "python", "label": "Python", "category": "lang", "aliases": []},
        ]
        with patch.object(eol_service.requests, "get", return_value=_fake_response(products)):
            catalog = get_product_catalog()
        self.assertEqual({p["name"] for p in catalog}, {"ios", "ipados"})

    def test_a_product_missing_category_entirely_is_excluded(self) -> None:
        """Every real product on endoflife.date has a category (verified
        against the live catalog: 462/462 as of this writing) -- if one
        somehow didn't, excluding it is the safe default (never assume an
        uncategorized product is actually an OS)."""
        products = [{"name": "mystery", "label": "Mystery Product", "aliases": []}]
        with patch.object(eol_service.requests, "get", return_value=_fake_response(products)):
            catalog = get_product_catalog()
        self.assertEqual(catalog, ())


class IpadResolvesToIpadosTests(unittest.TestCase):
    """End-to-end: with the hardware "ipad" product excluded, the bare
    "ipad" inventory alias (_INVENTORY_PHRASE_EXTRAS) can safely point to
    "ipados" without colliding with anything."""

    CATALOG = [
        {"name": "ios", "label": "Apple iOS", "category": "os", "aliases": []},
        {"name": "ipados", "label": "Apple iPadOS", "category": "os", "aliases": []},
    ]

    def setUp(self) -> None:
        get_product_catalog.cache_clear()
        eol_service.get_slug_index.cache_clear()
        self.addCleanup(get_product_catalog.cache_clear)
        self.addCleanup(eol_service.get_slug_index.cache_clear)

    def test_bare_ipad_os_string_resolves_to_ipados(self) -> None:
        with patch.object(eol_service.requests, "get", return_value=_fake_response(self.CATALOG)):
            valid = eol_service.get_valid_slugs()
            for os_string in ["iPad 10.0.2", "iPad 11.4.1", "iPad 10.2"]:
                with self.subTest(os_string=os_string):
                    self.assertEqual(resolve_product_slug(os_string, valid), "ipados")

    def test_ipados_still_resolves_when_spelled_out(self) -> None:
        with patch.object(eol_service.requests, "get", return_value=_fake_response(self.CATALOG)):
            valid = eol_service.get_valid_slugs()
            self.assertEqual(resolve_product_slug("iPadOS 10.0.2", valid), "ipados")


class StaleNormalizedFieldNamingTheWrongAppleProductTests(unittest.TestCase):
    """Real incident: a row's os_string is "iPad 10.3.4", but its
    normalized_os/normalized_os_detailed_name was previously (manually, or
    from before the ipad/ipados fix existed) set to "Apple iOS 10" -- a
    real, valid Apple product, just the WRONG one. vendors_compatible only
    catches cross-VENDOR mismatches (both "iPad ..." and "Apple iOS 10" are
    "apple" vendor), so lookup_os_eol would otherwise confidently query
    with the stale "Apple iOS 10" value and pull iOS's own (wrong) EOL/EOAS
    dates instead of iPadOS's. lookup_os_eol must detect that the raw
    os_string independently resolves to "ipados" (a product with a
    deliberate _INVENTORY_PHRASE_EXTRAS entry) and retry with os_string
    instead of trusting the stale field."""

    CATALOG = [
        {"name": "ios", "label": "Apple iOS", "category": "os", "aliases": []},
        {"name": "ipados", "label": "Apple iPadOS", "category": "os", "aliases": []},
    ]
    IOS_PRODUCT = {"result": {"label": "Apple iOS", "releases": [{"name": "10", "label": "iOS 10", "eolFrom": "2017-09-19"}]}}
    IPADOS_PRODUCT = {
        "result": {"label": "Apple iPadOS", "releases": [{"name": "10.3.4", "label": "iPadOS 10.3.4", "eolFrom": "2024-01-01"}]}
    }

    def setUp(self) -> None:
        get_product_catalog.cache_clear()
        eol_service.get_slug_index.cache_clear()
        self.addCleanup(get_product_catalog.cache_clear)
        self.addCleanup(eol_service.get_slug_index.cache_clear)
        with patch.object(eol_service.requests, "get", return_value=_fake_response(self.CATALOG)):
            self.valid_slugs = eol_service.get_valid_slugs()

    def _fetch_product(self, slug: str) -> dict:
        return self.IOS_PRODUCT if slug == "ios" else self.IPADOS_PRODUCT

    def test_stale_ios_normalized_field_is_overridden_by_the_real_os_string(self) -> None:
        with patch.object(eol_service, "fetch_product", side_effect=self._fetch_product):
            result = eol_service.lookup_os_eol(
                os_string="iPad 10.3.4",
                normalized_os_detailed_name="Apple iOS 10",
                normalized_os="Apple iOS 10",
                valid_slugs=self.valid_slugs,
                product_cache={},
            )
        self.assertEqual(result["product_slug"], "ipados")
        self.assertEqual(result["normalized_os"], "Apple iPadOS 10.3.4")
        # The iPadOS 10.3.4 date, not iOS 10's -- the whole point of the fix.
        self.assertNotEqual(result["eol_date"], eol_service.iso_date_to_epoch("2017-09-19"))
        self.assertEqual(result["eol_date"], eol_service.iso_date_to_epoch("2024-01-01"))

    def test_a_genuinely_correct_ios_normalization_is_not_touched(self) -> None:
        """A real iPhone row correctly normalized to iOS must NOT be
        redirected -- the override only fires when the raw os_string
        itself independently resolves to a DIFFERENT alias-covered
        product (here, it doesn't: "iPhone ..." has no "ipad" in it)."""
        with patch.object(eol_service, "fetch_product", side_effect=self._fetch_product):
            result = eol_service.lookup_os_eol(
                os_string="iPhone 10 (iOS 10)",
                normalized_os_detailed_name="Apple iOS 10",
                normalized_os="Apple iOS 10",
                valid_slugs=self.valid_slugs,
                product_cache={},
            )
        self.assertEqual(result["product_slug"], "ios")
        self.assertEqual(result["normalized_os"], "Apple iOS 10")


class IpadBelowIpadosFloorFallsBackToIosTests(unittest.TestCase):
    """Real incident: "ipados" as a distinct endoflife.date product only
    tracks major version 12 and up -- Apple didn't introduce "iPadOS" as a
    separate product name until 2019 (what would otherwise have been "iOS
    13"). A real device running an earlier version, e.g. "iPad 10.0.2",
    correctly resolves to product "ipados" (via the alias), but "ipados"
    has NO release for major 10 or 11 at all -- so the ordinary hint-scoring
    pass and the prior-value fallback both come up empty, and the row used
    to fall all the way through to the local vendor cascade (eosl.date) for
    a lookup endoflife.date could actually answer directly, just under its
    OLDER "ios" product name for that version range.
    _PRODUCT_RELEASE_FALLBACK_SLUGS now retries against "ios" -- still
    within the direct endoflife.date path -- whenever "ipados" resolves but
    has nothing at all to offer."""

    CATALOG = [
        {"name": "ios", "label": "Apple iOS", "category": "os", "aliases": []},
        {"name": "ipados", "label": "Apple iPadOS", "category": "os", "aliases": []},
    ]
    IOS_PRODUCT = {
        "result": {
            "label": "Apple iOS",
            "releases": [
                {"name": "11", "label": "iOS 11", "eolFrom": "2018-10-08"},
                {"name": "10", "label": "iOS 10", "eolFrom": "2019-07-22"},
            ],
        }
    }
    IPADOS_PRODUCT = {
        "result": {
            "label": "Apple iPadOS",
            # Deliberately starts at 12 -- no release covers 10.x/11.x at all.
            "releases": [{"name": "12", "label": "iPadOS 12", "eolFrom": "2026-01-26"}],
        }
    }

    def setUp(self) -> None:
        get_product_catalog.cache_clear()
        eol_service.get_slug_index.cache_clear()
        self.addCleanup(get_product_catalog.cache_clear)
        self.addCleanup(eol_service.get_slug_index.cache_clear)
        with patch.object(eol_service.requests, "get", return_value=_fake_response(self.CATALOG)):
            self.valid_slugs = eol_service.get_valid_slugs()

    def _fetch_product(self, slug: str) -> dict:
        return self.IOS_PRODUCT if slug == "ios" else self.IPADOS_PRODUCT

    def test_pre_ipados_version_falls_back_to_ios_within_the_direct_api_path(self) -> None:
        with patch.object(eol_service, "fetch_product", side_effect=self._fetch_product):
            result = eol_service.lookup_os_eol(
                os_string="iPad 10.0.2",
                normalized_os_detailed_name="",
                normalized_os="",
                valid_slugs=self.valid_slugs,
                product_cache={},
            )
        self.assertEqual(result["product_slug"], "ios")
        self.assertEqual(result["normalized_os"], "Apple iOS 10")
        self.assertEqual(result["eol_date"], eol_service.iso_date_to_epoch("2019-07-22"))
        self.assertEqual(result["api_note"], "")

    def test_major_11_also_falls_back(self) -> None:
        with patch.object(eol_service, "fetch_product", side_effect=self._fetch_product):
            result = eol_service.lookup_os_eol(
                os_string="iPad 11.4.1",
                normalized_os_detailed_name="",
                normalized_os="",
                valid_slugs=self.valid_slugs,
                product_cache={},
            )
        self.assertEqual(result["product_slug"], "ios")
        self.assertEqual(result["normalized_os"], "Apple iOS 11")

    def test_a_version_ipados_genuinely_covers_is_not_redirected(self) -> None:
        """Sanity check: once "ipados" actually has a matching release,
        the fallback must never even be considered."""
        with patch.object(eol_service, "fetch_product", side_effect=self._fetch_product):
            result = eol_service.lookup_os_eol(
                os_string="iPad 12.0",
                normalized_os_detailed_name="",
                normalized_os="",
                valid_slugs=self.valid_slugs,
                product_cache={},
            )
        self.assertEqual(result["product_slug"], "ipados")
        self.assertEqual(result["normalized_os"], "Apple iPadOS 12")


if __name__ == "__main__":
    unittest.main()
