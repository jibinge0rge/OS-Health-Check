"""Smoke tests for Router-Switch EOL scraper (limited pages)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from router_switch_service import (
    _MIN_RELEASE_SCORE,
    _parse_page_count,
    _parse_table_rows,
    _parse_us_date,
    _router_switch_product_overlap,
    _score_router_switch_row,
    get_status,
    init_db,
    list_all_rows,
    list_manufacturers,
    lookup_os_router_switch,
    manufacturers_from_slugs,
    sync_router_switch_database,
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


class RouterSwitchMatchTests(unittest.TestCase):
    def test_product_overlap_rejects_unrelated_cisco_hardware(self) -> None:
        query = "Cisco NX-OS 10.3(7)"
        part = "QVPCF-00-UL10TOP"
        product = "Cisco Ultra Traffic Optimiz Solution, 10K Sess (Failover)"
        self.assertFalse(_router_switch_product_overlap(query, part, product))

    def test_score_rejects_nxos_false_positive(self) -> None:
        query = "Cisco NX-OS 10.3(7)"
        part = "QVPCF-00-UL10TOP"
        product = "Cisco Ultra Traffic Optimiz Solution, 10K Sess (Failover)"
        score = _score_router_switch_row(part, product, query, ["10.3", "7"])
        self.assertEqual(score, 0)

    def test_score_accepts_ios_xe_overlap(self) -> None:
        query = "Cisco IOS XE 16.5.1"
        part = "IOS XE 16.5.1"
        product = "Cisco IOS XE 16.5.1"
        score = _score_router_switch_row(part, product, query, ["16.5.1"])
        self.assertGreaterEqual(score, _MIN_RELEASE_SCORE)

    def test_product_overlap_rejects_classic_ios_vs_cloud_logging(self) -> None:
        query = "Cisco IOS 12.2(50)SE5"
        part = "SEC-LOG-CL-5Y"
        product = "Cisco Cloud Logging Subscription, 5 Year"
        self.assertFalse(_router_switch_product_overlap(query, part, product))

    def test_score_rejects_classic_ios_cloud_logging_false_positive(self) -> None:
        query = "Cisco IOS 12.2(50)SE5"
        part = "SEC-LOG-CL-5Y"
        product = "Cisco Cloud Logging Subscription, 5 Year"
        score = _score_router_switch_row(part, product, query, ["12.2", "50", "5"])
        self.assertEqual(score, 0)

    def test_score_accepts_classic_ios_with_version(self) -> None:
        query = "Cisco IOS 12.2(50)SE5"
        part = "IOS 12.2(50)SE5"
        product = "Cisco IOS 12.2(50)SE5"
        score = _score_router_switch_row(part, product, query, ["12.2", "50", "5"])
        self.assertGreaterEqual(score, _MIN_RELEASE_SCORE)

    def test_lookup_rejects_nxos_false_positive_in_db(self) -> None:
        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "rs_match.db"
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
                        "QVPCF-00-UL10TOP",
                        "2020-01-01",
                        "2026-05-07",
                        "2028-11-30",
                        "Cisco Ultra Traffic Optimiz Solution, 10K Sess (Failover)",
                        0,
                    ),
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
                        "IOS XE 16.5.1",
                        "2017-11-30",
                        "2017-08-31",
                        "2022-11-30",
                        "Cisco IOS XE 16.5.1",
                        0,
                    ),
                )
                connection.commit()

            miss = lookup_os_router_switch(
                "Cisco NX-OS 10.3(7)",
                "",
                "",
                db_path=db_path,
            )
            self.assertEqual(miss["eol_date"], "")
            self.assertIn("No matching", miss["api_note"])

            hit = lookup_os_router_switch(
                "Cisco IOS XE 16.5.1",
                "",
                "",
                db_path=db_path,
            )
            self.assertTrue(hit["eol_date"])
            self.assertEqual(hit["release_name"], "IOS XE 16.5.1")
        finally:
            try:
                db_path.unlink(missing_ok=True)
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
