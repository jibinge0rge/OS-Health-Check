"""Regression tests for lookup_os_eol_batch's shared product_cache.

Refresh EOL/EOAS splits a large lookup into chunks (LOOKUP_REFRESH_CHUNK_SIZE
rows each) and used to call lookup_os_eol_batch with a fresh, empty cache per
chunk -- so a common product slug (e.g. "windows") got re-fetched from
endoflife.date once per chunk that happened to contain a matching row,
instead of once for the whole refresh. These tests pin the fix: passing one
dict across multiple calls must fetch each distinct slug at most once.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import eol_service


FAKE_PRODUCT = {
    "result": {
        "label": "Microsoft Windows",
        "releases": [
            {
                "name": "11",
                "label": "11",
                "eolFrom": "2030-01-01",
                "eoasFrom": "2028-01-01",
                "isEol": False,
                "isEoas": False,
            }
        ],
    }
}


class LookupOsEolBatchSharedCacheTests(unittest.TestCase):
    def _patched(self, fetch_side_effect):
        return (
            patch.object(eol_service, "get_valid_slugs", return_value=frozenset({"windows"})),
            patch.object(eol_service, "resolve_product_slug", return_value="windows"),
            patch.object(eol_service, "fetch_product", side_effect=fetch_side_effect),
        )

    def test_shared_cache_fetches_each_slug_only_once_across_calls(self) -> None:
        """Two separate lookup_os_eol_batch calls (simulating two Refresh
        chunks) sharing one product_cache dict must only hit fetch_product
        once for a slug both chunks need."""
        call_count = {"n": 0}

        def fake_fetch_product(_slug):
            call_count["n"] += 1
            return FAKE_PRODUCT

        chunk_a = [{"os_string": "Windows 11 Pro", "normalized_os_detailed_name": "", "normalized_os": ""}]
        chunk_b = [{"os_string": "Windows 11 Enterprise", "normalized_os_detailed_name": "", "normalized_os": ""}]

        p1, p2, p3 = self._patched(fake_fetch_product)
        with p1, p2, p3:
            shared_cache: dict = {}
            result_a = eol_service.lookup_os_eol_batch(chunk_a, product_cache=shared_cache)
            result_b = eol_service.lookup_os_eol_batch(chunk_b, product_cache=shared_cache)

        self.assertEqual(call_count["n"], 1, "second call must reuse the first call's cached product")
        self.assertTrue(result_a[0]["eol_date"])
        self.assertTrue(result_b[0]["eol_date"])
        self.assertIn("windows", shared_cache)

    def test_no_cache_passed_refetches_every_call(self) -> None:
        """Documents the pre-fix default: omitting product_cache gives each
        call its own fresh cache, so the same slug is fetched again."""
        call_count = {"n": 0}

        def fake_fetch_product(_slug):
            call_count["n"] += 1
            return FAKE_PRODUCT

        chunk_a = [{"os_string": "Windows 11 Pro", "normalized_os_detailed_name": "", "normalized_os": ""}]
        chunk_b = [{"os_string": "Windows 11 Enterprise", "normalized_os_detailed_name": "", "normalized_os": ""}]

        p1, p2, p3 = self._patched(fake_fetch_product)
        with p1, p2, p3:
            eol_service.lookup_os_eol_batch(chunk_a)
            eol_service.lookup_os_eol_batch(chunk_b)

        self.assertEqual(call_count["n"], 2)

    def test_cache_not_refetched_within_a_single_call_either(self) -> None:
        """Sanity check the pre-existing within-call dedup still holds:
        several rows needing the same slug in ONE call still fetch once."""
        call_count = {"n": 0}

        def fake_fetch_product(_slug):
            call_count["n"] += 1
            return FAKE_PRODUCT

        items = [
            {"os_string": "Windows 11 Pro", "normalized_os_detailed_name": "", "normalized_os": ""},
            {"os_string": "Windows 11 Home", "normalized_os_detailed_name": "", "normalized_os": ""},
            {"os_string": "Windows 11 Enterprise", "normalized_os_detailed_name": "", "normalized_os": ""},
        ]

        p1, p2, p3 = self._patched(fake_fetch_product)
        with p1, p2, p3:
            results = eol_service.lookup_os_eol_batch(items)

        self.assertEqual(call_count["n"], 1)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["eol_date"] for r in results))


if __name__ == "__main__":
    unittest.main()
