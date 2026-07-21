"""Smoke tests for Layer23-Switch EOL scraper (limited pages)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from layer23_switch_service import (
    _parse_iso_date,
    _parse_table_rows,
    get_status,
    init_db,
    list_all_rows,
    list_manufacturers,
    load_selected_manufacturers,
    lookup_os_layer23_switch,
    manufacturers_from_slugs,
    save_selected_manufacturers,
    sync_layer23_switch_database,
    _connect,
)


SAMPLE_HTML = """
<html><body>
<p>Total Products: 40 | Page 1 of 2</p>
<table>
  <tr>
    <th>Part Number</th><th>Product Name</th>
    <th>EOL Announcement</th><th>End of Sale (EOS)</th>
    <th>End of Service Life (EOSL)</th>
  </tr>
  <tr>
    <td><a href="/eol-eosl-tool/c9300-24p-a/">C9300-24P-A</a></td>
    <td>Cisco Catalyst 9300 24-port data only, Network Essentials</td>
    <td>2024-01-31</td><td>2024-07-31</td><td>2029-07-31</td>
  </tr>
</table>
</body></html>
"""


class Layer23SwitchParseTests(unittest.TestCase):
    def test_parse_iso_date(self) -> None:
        self.assertEqual(_parse_iso_date("2024-01-31"), "2024-01-31")
        self.assertEqual(_parse_iso_date("Not announced"), "")

    def test_parse_table_rows(self) -> None:
        rows = _parse_table_rows(SAMPLE_HTML, "cisco")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["release_name"], "C9300-24P-A")
        self.assertEqual(rows[0]["eol_date"], "2024-01-31")
        self.assertEqual(rows[0]["released_date"], "2024-07-31")
        self.assertEqual(rows[0]["eoas_date"], "2029-07-31")
        self.assertEqual(
            rows[0]["latest_raw"],
            "Cisco Catalyst 9300 24-port data only, Network Essentials",
        )

    def test_manufacturers_list_matches_site(self) -> None:
        slugs = [item["slug"] for item in list_manufacturers()]
        self.assertIn("cisco", slugs)
        self.assertIn("dell", slugs)
        self.assertIn("palo-alto-networks", slugs)
        selected = manufacturers_from_slugs(["cisco", "dell"])
        self.assertEqual([slug for slug, _ in selected], ["cisco", "dell"])
        with self.assertRaises(ValueError):
            manufacturers_from_slugs(["not-a-vendor"])

    def test_selected_manufacturers_prefs_roundtrip(self) -> None:
        tmp = tempfile.mkdtemp()
        prefs = Path(tmp) / "layer23_switch_sync.json"
        try:
            saved = save_selected_manufacturers(["cisco", "hpe"], prefs_path=prefs)
            self.assertEqual(saved, ["cisco", "hpe"])
            loaded = load_selected_manufacturers(prefs_path=prefs)
            self.assertEqual(loaded, ["cisco", "hpe"])
        finally:
            try:
                prefs.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                Path(tmp).rmdir()
            except OSError:
                pass


class Layer23SwitchLookupTests(unittest.TestCase):
    def test_lookup_hits_matching_row_in_db(self) -> None:
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "layer23_match.db"
        try:
            init_db(db_path)
            scraped_at = "2026-01-01T00:00:00+00:00"
            with _connect(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO products (slug, name, category, url, scraped_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("cisco", "Cisco", "hardware", "https://example.com/cisco", scraped_at),
                )
                connection.execute(
                    """
                    INSERT INTO releases (
                        product_slug, release_name, released_date,
                        eol_date, eoas_date, latest_raw, is_supported
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "cisco",
                        "C9300-24P-A",
                        "2024-07-31",
                        "2024-01-31",
                        "2029-07-31",
                        "Cisco Catalyst 9300 24-port data only, Network Essentials",
                        1,
                    ),
                )
                connection.commit()

            hit = lookup_os_layer23_switch(
                "Cisco Catalyst 9300 C9300-24P-A",
                "",
                "",
                db_path=db_path,
            )
            self.assertTrue(hit["eol_date"])
            self.assertEqual(hit["release_name"], "C9300-24P-A")
            self.assertEqual(hit["source"], "layer23-switch")
        finally:
            try:
                db_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                Path(tmp).rmdir()
            except OSError:
                pass


class Layer23SwitchLiveSmokeTests(unittest.TestCase):
    def test_sync_one_cisco_page(self) -> None:
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "layer23.db"
        try:
            result = sync_layer23_switch_database(
                db_path=db_path,
                manufacturers=(("cisco", "Cisco"),),
                max_pages_per_manufacturer=1,
            )
            self.assertTrue(result["ok"], result)
            self.assertGreater(int(result["release_count"]), 0)
            status = get_status(db_path)
            self.assertEqual(status["source_id"], "layer23-switch")
            self.assertGreater(int(status["release_count"]), 0)
            rows = list_all_rows(db_path)
            self.assertGreater(len(rows), 0)
            sample = rows[0]
            self.assertIn("product", sample)
            self.assertIn("release", sample)
            self.assertIn("eol_date", sample)
            self.assertIn("eoas_date", sample)
        finally:
            try:
                db_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                Path(tmp).rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
