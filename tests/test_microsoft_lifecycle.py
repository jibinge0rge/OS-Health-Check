"""Tests for the Microsoft Lifecycle vendor lookup scraper."""

from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from vendor_lookups.microsoft_lifecycle_service import (
    _iso_date_only,
    _pick_release,
    _resolve_product_slug,
    build_release_rows,
    collect_products,
    get_status,
    init_db,
    list_all_rows,
    lookup_os_microsoft_lifecycle,
    sync_microsoft_lifecycle_database,
    _connect,
)


SAMPLE_ITEMS = [
    {
        "title": "SQL Server 2025",
        "summary": "SQL Server 2025 follows the Fixed Lifecycle Policy.",
        "start": "2025-11-18T08:00:00Z",
        "end": "2036-01-07T06:59:59.999Z",
        "products": ["sql-server"],
        "display_products": ["SQL Server"],
        "url": "/lifecycle/products/sql-server-2025",
    },
    {
        "title": "SQL Server 2022",
        "summary": "SQL Server 2022 follows the Fixed Lifecycle Policy.",
        "start": "2022-11-16T08:00:00Z",
        "end": "2033-01-11T06:59:59.999Z",
        "products": ["sql-server"],
        "display_products": ["SQL Server"],
        "url": "/lifecycle/products/sql-server-2022",
    },
    {
        "title": "Windows Server 2025 Datacenter",
        "summary": "Windows Server 2025 Datacenter follows the Modern Lifecycle Policy.",
        "start": "2024-09-10T08:00:00Z",
        "end": None,
        "products": ["windows"],
        "display_products": ["Windows"],
        "url": "/lifecycle/products/windows-server-2025-datacenter",
    },
    # Duplicate title for the same family - must be de-duplicated.
    {
        "title": "SQL Server 2025",
        "summary": "SQL Server 2025 follows the Fixed Lifecycle Policy.",
        "start": "2025-11-18T08:00:00Z",
        "end": "2036-01-07T06:59:59.999Z",
        "products": ["sql-server"],
        "display_products": ["SQL Server"],
        "url": "/lifecycle/products/sql-server-2025",
    },
]


def _pg_available() -> bool:
    return bool(str(os.environ.get("DATABASE_URL") or "").strip())


def _temp_schema(prefix: str) -> str:
    return f"test_{prefix}_{uuid.uuid4().hex[:12]}"


class IsoDateOnlyTests(unittest.TestCase):
    def test_truncates_to_calendar_date(self) -> None:
        self.assertEqual(_iso_date_only("2036-01-07T06:59:59.999Z"), "2036-01-07")
        self.assertEqual(_iso_date_only("2025-11-18T08:00:00Z"), "2025-11-18")

    def test_blank_or_missing_values(self) -> None:
        self.assertEqual(_iso_date_only(None), "")
        self.assertEqual(_iso_date_only(""), "")
        self.assertEqual(_iso_date_only("not a date"), "")


class BuildReleaseRowsTests(unittest.TestCase):
    def test_groups_by_family_and_dedupes(self) -> None:
        products, releases = build_release_rows(SAMPLE_ITEMS)

        self.assertEqual(products, {"sql-server": "SQL Server", "windows": "Windows"})
        # The duplicate "SQL Server 2025" entry must not create a second row.
        self.assertEqual(len(releases), 3)

        by_title = {row["release_name"]: row for row in releases}
        sql_2025 = by_title["SQL Server 2025"]
        self.assertEqual(sql_2025["product_slug"], "sql-server")
        self.assertEqual(sql_2025["released_date"], "2025-11-18")
        self.assertEqual(sql_2025["eol_date"], "2036-01-07")
        self.assertEqual(sql_2025["eoas_date"], "")
        self.assertEqual(sql_2025["is_supported"], "1")
        self.assertTrue(sql_2025["latest_raw"].endswith("/lifecycle/products/sql-server-2025"))

        sql_2022 = by_title["SQL Server 2022"]
        self.assertEqual(sql_2022["eol_date"], "2033-01-11")

        modern = by_title["Windows Server 2025 Datacenter"]
        self.assertEqual(modern["eol_date"], "")
        self.assertEqual(modern["is_supported"], "1")  # no end date -> still supported


class ResolveAndPickTests(unittest.TestCase):
    def _products(self) -> list[dict[str, str]]:
        return [
            {"slug": "sql-server", "name": "SQL Server"},
            {"slug": "windows", "name": "Windows"},
            {"slug": "vs", "name": "Visual Studio"},
        ]

    def test_resolve_product_slug_by_name(self) -> None:
        self.assertEqual(
            _resolve_product_slug("Microsoft SQL Server 2025 Enterprise", self._products()),
            "sql-server",
        )

    def test_windows_family_is_never_resolved(self) -> None:
        """The "windows" family is deliberately excluded from matching --
        endoflife.date's own dedicated windows/windows-server products
        already cover this ground far more precisely, and Microsoft
        Lifecycle's own release naming for it is too loose to safely
        disambiguate (see test_bare_major_no_longer_matches_wrong_release)."""
        self.assertIsNone(_resolve_product_slug("Windows Server 2025 Standard", self._products()))
        self.assertIsNone(_resolve_product_slug("Windows 10 Pro 10.0.16299.0", self._products()))

    def test_resolve_product_slug_no_match(self) -> None:
        self.assertIsNone(_resolve_product_slug("Ubuntu 22.04 LTS", self._products()))
        self.assertIsNone(_resolve_product_slug("", self._products()))

    def test_pick_release_requires_strong_version_match(self) -> None:
        releases = [
            {"release_name": "SQL Server 2025", "released_date": "", "eol_date": "1",
             "eoas_date": "", "latest_raw": "", "is_supported": 1},
            {"release_name": "SQL Server 2022", "released_date": "", "eol_date": "2",
             "eoas_date": "", "latest_raw": "", "is_supported": 0},
        ]
        picked = _pick_release(releases, ["2025"])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["release_name"], "SQL Server 2025")

        # A bitness-only hint must not pick a release.
        self.assertIsNone(_pick_release(releases, ["64"]))
        self.assertIsNone(_pick_release(releases, []))

    def test_pick_release_refuses_to_guess_on_a_tie(self) -> None:
        """Real incident this pins: a bare major hint ("10") scored an exact
        token match against EVERY Windows 10-family release's own shared "10"
        prefix token, so a 2017 build (10.0.16299) matched "Windows 10 IoT
        Enterprise LTSC 2021" (a 2021 release) purely because both releases'
        names started with "10" -- there was no real signal to choose among
        the tied candidates. Even outside the excluded "windows" family, a
        genuine tie must never be resolved by picking whichever sorts first."""
        releases = [
            {"release_name": "10-1709-w", "released_date": "", "eol_date": "1",
             "eoas_date": "", "latest_raw": "", "is_supported": 1},
            {"release_name": "10-24h2-iot-lts", "released_date": "", "eol_date": "2",
             "eoas_date": "", "latest_raw": "", "is_supported": 1},
        ]
        # Both release names contain the bare token "10" -- an exact-string
        # match against either candidate's extracted "10" token -- so this
        # hint ties between them.
        self.assertIsNone(_pick_release(releases, ["10"]))


class CollectProductsPaginationTests(unittest.TestCase):
    def test_paginates_until_count_reached(self) -> None:
        page1 = {
            "results": [SAMPLE_ITEMS[0], SAMPLE_ITEMS[1]],
            "count": 3,
            "@nextLink": "/api/contentbrowser/search/lifecycles?$skip=2",
        }
        page2 = {
            "results": [SAMPLE_ITEMS[2]],
            "count": 3,
            "@nextLink": None,
        }
        responses = [page1, page2]

        def fake_get(*_args, **kwargs):
            class FakeResponse:
                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict:
                    return responses.pop(0)

            return FakeResponse()

        with patch("vendor_lookups.microsoft_lifecycle_service.requests.get", side_effect=fake_get):
            with patch("vendor_lookups.microsoft_lifecycle_service.time.sleep"):
                items = collect_products()

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "SQL Server 2025")
        self.assertEqual(items[2]["title"], "Windows Server 2025 Datacenter")

    def test_max_pages_bounds_requests(self) -> None:
        calls = {"n": 0}

        def fake_get(*_args, **kwargs):
            calls["n"] += 1

            class FakeResponse:
                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict:
                    return {
                        "results": [SAMPLE_ITEMS[0]],
                        "count": 1000,
                        "@nextLink": "/more",
                    }

            return FakeResponse()

        with patch("vendor_lookups.microsoft_lifecycle_service.requests.get", side_effect=fake_get):
            with patch("vendor_lookups.microsoft_lifecycle_service.time.sleep"):
                items = collect_products(max_pages=2)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(items), 2)


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class MicrosoftLifecycleLookupTests(unittest.TestCase):
    def test_lookup_hits_matching_row_in_db(self) -> None:
        from vendor_lookups.db import drop_schema

        schema_name = _temp_schema("mslife")
        try:
            init_db(schema_name)
            scraped_at = "2026-01-01T00:00:00+00:00"
            with _connect(schema_name) as connection:
                connection.execute(
                    """
                    INSERT INTO products (slug, name, category, url, scraped_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    ("sql-server", "SQL Server", "microsoft", "https://example.com", scraped_at),
                )
                connection.execute(
                    """
                    INSERT INTO releases (
                        product_slug, release_name, released_date,
                        eol_date, eoas_date, latest_raw, is_supported
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        "sql-server",
                        "SQL Server 2025",
                        "2025-11-18",
                        "2036-01-07",
                        "",
                        "https://learn.microsoft.com/lifecycle/products/sql-server-2025",
                        1,
                    ),
                )

            hit = lookup_os_microsoft_lifecycle(
                "Microsoft SQL Server 2025 Enterprise",
                "",
                "",
                schema_name=schema_name,
            )
            self.assertTrue(hit["eol_date"])
            self.assertEqual(hit["release_name"], "SQL Server 2025")
            self.assertEqual(hit["source"], "microsoft-lifecycle")

            rows = list_all_rows(schema_name)
            self.assertEqual(len(rows), 1)
            status = get_status(schema_name)
            self.assertEqual(status["source_id"], "microsoft-lifecycle")
            self.assertEqual(int(status["release_count"]), 1)
        finally:
            drop_schema(schema_name)


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class MicrosoftLifecycleLiveSmokeTests(unittest.TestCase):
    def test_sync_two_pages_from_live_api(self) -> None:
        from vendor_lookups.db import drop_schema

        schema_name = _temp_schema("mslifelive")
        try:
            result = sync_microsoft_lifecycle_database(schema_name=schema_name, max_pages=2)
            self.assertTrue(result["ok"], result)
            self.assertGreater(int(result["release_count"]), 0)
            rows = list_all_rows(schema_name)
            self.assertGreater(len(rows), 0)
            sample = rows[0]
            self.assertIn("product", sample)
            self.assertIn("release", sample)
            self.assertIn("eol_date", sample)
            self.assertIn("eoas_date", sample)
        finally:
            drop_schema(schema_name)

    def test_cancelled_sync_preserves_existing_data(self) -> None:
        """A sync cancelled mid-pagination must never replace a complete
        prior dataset with the partial one collected so far (regression for
        a real incident: cancelling mid-run dropped 16/825 down to 15/599)."""
        import threading

        from vendor_lookups.db import drop_schema
        import vendor_lookups.microsoft_lifecycle_service as mls

        schema_name = _temp_schema("mslifecancel")
        try:
            first = sync_microsoft_lifecycle_database(schema_name=schema_name, max_pages=2)
            self.assertTrue(first["ok"], first)
            before_products = first["product_count"]
            before_releases = first["release_count"]

            cancel_event = threading.Event()
            call_count = {"n": 0}
            orig_fetch_page = mls._fetch_page

            def fetch_then_cancel(skip, top=mls.PAGE_SIZE):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    cancel_event.set()
                return orig_fetch_page(skip, top)

            with patch.object(mls, "_fetch_page", side_effect=fetch_then_cancel):
                second = sync_microsoft_lifecycle_database(
                    schema_name=schema_name, cancel_event=cancel_event, max_pages=10
                )

            self.assertTrue(second["cancelled"])
            self.assertFalse(second["ok"])
            self.assertEqual(second["product_count"], before_products)
            self.assertEqual(second["release_count"], before_releases)

            status = get_status(schema_name)
            self.assertEqual(int(status["product_count"]), before_products)
            self.assertEqual(int(status["release_count"]), before_releases)
        finally:
            drop_schema(schema_name)


if __name__ == "__main__":
    unittest.main()
