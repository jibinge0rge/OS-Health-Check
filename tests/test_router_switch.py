"""Smoke tests for Router-Switch EOL scraper (limited pages)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from router_switch_service import (
    _parse_page_count,
    _parse_table_rows,
    _parse_us_date,
    get_status,
    list_all_rows,
    list_manufacturers,
    manufacturers_from_slugs,
    sync_router_switch_database,
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
    <td><a href="/eol-eosl-checker/cisco/ios-xe-16-5-1_1.html">IOS XE 16.5.1</a></td>
    <td>Cisco IOS XE 16.5.1</td>
    <td>08/31/2017</td><td>11/30/2017</td><td>11/30/2022</td>
  </tr>
</table>
</body></html>
"""


class RouterSwitchParseTests(unittest.TestCase):
    def test_parse_us_date(self) -> None:
        self.assertEqual(_parse_us_date("08/31/2017"), "2017-08-31")
        self.assertEqual(_parse_us_date("n/a"), "")

    def test_parse_page_count(self) -> None:
        total, current, pages = _parse_page_count(SAMPLE_HTML)
        self.assertEqual((total, current, pages), (40, 1, 2))

    def test_parse_table_rows(self) -> None:
        rows = _parse_table_rows(SAMPLE_HTML, "cisco")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["release_name"], "IOS XE 16.5.1")
        self.assertEqual(rows[0]["eol_date"], "2017-08-31")
        self.assertEqual(rows[0]["eoas_date"], "2022-11-30")
        self.assertEqual(rows[0]["released_date"], "2017-11-30")
        self.assertEqual(rows[0]["latest_raw"], "Cisco IOS XE 16.5.1")

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
        from router_switch_service import (
            load_selected_manufacturers,
            save_selected_manufacturers,
        )

        tmp = tempfile.mkdtemp()
        prefs = Path(tmp) / "router_switch_sync.json"
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


class RouterSwitchLiveSmokeTests(unittest.TestCase):
    def test_sync_one_cisco_page(self) -> None:
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "rs.db"
        try:
            result = sync_router_switch_database(
                db_path=db_path,
                manufacturers=(("cisco", "Cisco"),),
                max_pages_per_manufacturer=1,
            )
            self.assertTrue(result["ok"], result)
            self.assertGreater(int(result["release_count"]), 0)
            status = get_status(db_path)
            self.assertEqual(status["source_id"], "router-switch")
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
