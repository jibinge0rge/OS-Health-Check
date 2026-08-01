"""Regression test for eosl.date sync: a cancelled run must never wipe
products it hasn't gotten to yet -- see vendor_lookups/eosl_service.py's
per-product delete+insert (moved from a single upfront blanket delete)."""

from __future__ import annotations

import os
import threading
import unittest
import uuid
from unittest.mock import patch

import vendor_lookups.eosl_service as eosl


def _pg_available() -> bool:
    return bool(str(os.environ.get("DATABASE_URL") or "").strip())


def _temp_schema(prefix: str) -> str:
    return f"test_{prefix}_{uuid.uuid4().hex[:12]}"


@unittest.skipUnless(_pg_available(), "DATABASE_URL not set")
class EoslCancelPreservesDataTests(unittest.TestCase):
    def test_cancel_mid_run_preserves_not_yet_reached_products(self) -> None:
        from vendor_lookups.db import drop_schema

        schema_name = _temp_schema("eoslcancel")
        eosl.init_db(schema_name)
        try:
            with eosl._connect(schema_name) as connection:
                for slug in ("alpha", "beta", "gamma", "delta"):
                    connection.execute(
                        "INSERT INTO products(slug, name, category, url, scraped_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (slug, f"OLD {slug}", "os", f"https://example.com/{slug}", "2020-01-01T00:00:00"),
                    )
                    connection.execute(
                        "INSERT INTO releases(product_slug, release_name, released_date, "
                        "eol_date, eoas_date, latest_raw, is_supported) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (slug, "old-release", "2020-01-01", "2021-01-01", "", "", 0),
                    )

            cancel_event = threading.Event()
            call_count = {"n": 0}

            def fake_fetch(url):
                call_count["n"] += 1
                if call_count["n"] == 3:
                    cancel_event.set()  # fires during product #3's own fetch
                return "<html></html>"

            def fake_parse(slug, html):
                return f"NEW {slug}", [
                    {
                        "release_name": "new-release",
                        "released_date": "2026-01-01",
                        "eol_date": "2030-01-01",
                        "eoas_date": "",
                        "latest_raw": "",
                        "is_supported": "1",
                    }
                ]

            with (
                patch.object(
                    eosl,
                    "collect_os_products",
                    return_value=[("alpha", "Alpha"), ("beta", "Beta"), ("gamma", "Gamma"), ("delta", "Delta")],
                ),
                patch.object(eosl, "_fetch_html", side_effect=fake_fetch),
                patch.object(eosl, "_parse_product_page", side_effect=fake_parse),
            ):
                result = eosl.sync_os_database(schema_name=schema_name, cancel_event=cancel_event)

            self.assertTrue(result["cancelled"])

            with eosl._connect(schema_name) as connection:
                products = {
                    r["slug"]: r["name"]
                    for r in connection.execute("SELECT slug, name FROM products")
                }
                releases = {
                    r["product_slug"]: r["release_name"]
                    for r in connection.execute("SELECT product_slug, release_name FROM releases")
                }

            # Processed before cancel was detected (start of #4's iteration).
            for slug in ("alpha", "beta", "gamma"):
                self.assertEqual(products[slug], f"NEW {slug}")
                self.assertEqual(releases[slug], "new-release")

            # Cancel was caught before delta's turn -- its prior data survives.
            self.assertEqual(products["delta"], "OLD delta")
            self.assertEqual(releases["delta"], "old-release")
        finally:
            drop_schema(schema_name)


if __name__ == "__main__":
    unittest.main()
