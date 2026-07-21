"""Tests for vendor lookup settings and Refresh fallback order."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vendor_lookup_service import lookup_vendor_batch
from vendor_settings import (
    VENDOR_FALLBACK_ORDER,
    load_settings,
    query_matches_keywords,
    save_settings,
    source_is_enabled,
    source_matches_query,
    update_source_settings,
)


class VendorSettingsTests(unittest.TestCase):
    def test_fallback_order_fixed(self) -> None:
        self.assertEqual(
            list(VENDOR_FALLBACK_ORDER),
            ["eosl", "junos", "suse", "layer23-switch", "router-switch"],
        )

    def test_defaults_hardware_sources_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vendor_lookup_settings.json"
            settings = load_settings(path)
            self.assertTrue(source_is_enabled("eosl", settings))
            self.assertTrue(source_is_enabled("junos", settings))
            self.assertTrue(source_is_enabled("suse", settings))
            self.assertFalse(source_is_enabled("layer23-switch", settings))
            self.assertFalse(source_is_enabled("router-switch", settings))

    def test_keyword_matching(self) -> None:
        self.assertTrue(query_matches_keywords(["junos", "juniper"], "Juniper Junos 21.2"))
        self.assertTrue(query_matches_keywords(["ios xe", "cisco"], "Cisco IOS XE 17.9"))
        self.assertFalse(query_matches_keywords(["junos"], "Ubuntu 22.04"))
        self.assertTrue(query_matches_keywords([], "anything"))

    def test_update_source_settings_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vendor_lookup_settings.json"
            update_source_settings(
                "router-switch",
                enabled=True,
                keywords=["cisco", "ios-xe"],
                prefs_path=path,
            )
            settings = load_settings(path)
            self.assertTrue(source_is_enabled("router-switch", settings))
            self.assertTrue(
                source_matches_query(
                    "router-switch",
                    "Cisco IOS-XE 17.09.08",
                    settings=settings,
                )
            )

    def test_save_lookup_settings_bulk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vendor_lookup_settings.json"
            with patch("vendor_settings.default_prefs_path", return_value=path):
                from vendor_lookup_service import save_lookup_settings

                result = save_lookup_settings(
                    {
                        "sources": {
                            "eosl": {"enabled": False},
                            "router-switch": {
                                "enabled": True,
                                "keywords": ["cisco", "ios"],
                            },
                        }
                    }
                )
            sources = result["sources"]
            assert isinstance(sources, dict)
            self.assertFalse(sources["eosl"]["enabled"])  # type: ignore[index]
            self.assertTrue(sources["router-switch"]["enabled"])  # type: ignore[index]
            self.assertEqual(
                sources["router-switch"]["keywords"],  # type: ignore[index]
                ["cisco", "ios"],
            )
            settings = load_settings(path)
            self.assertFalse(source_is_enabled("eosl", settings))
            self.assertTrue(source_is_enabled("router-switch", settings))


class VendorLookupBatchOrderTests(unittest.TestCase):
    def test_eosl_before_junos_when_both_match(self) -> None:
        eosl_hit = {
            "eol_date": "1",
            "eol_status": "false",
            "eoas_date": "2",
            "eoas_status": "false",
            "source": "eosl",
            "api_note": "",
            "query_used": "Junos OS 21.2",
            "query_field": "normalized_os",
            "product_slug": "junos",
            "release_name": "21.2",
            "release_label": "21.2",
            "normalized_os_detailed_name": "",
            "normalized_os": "",
        }
        junos_hit = dict(eosl_hit, source="junos")
        junos_calls = {"n": 0}

        def eosl_lookup(*_args, **_kwargs):
            return eosl_hit

        def junos_lookup(*_args, **_kwargs):
            junos_calls["n"] += 1
            return junos_hit

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vendor_lookup_settings.json"
            save_settings(
                {
                    "sources": {
                        "eosl": {"enabled": True, "keywords": []},
                        "junos": {"enabled": True, "keywords": ["junos", "juniper"]},
                        "suse": {"enabled": True, "keywords": ["suse"]},
                        "layer23-switch": {"enabled": False, "keywords": ["cisco"]},
                        "router-switch": {"enabled": False, "keywords": ["cisco"]},
                    }
                },
                prefs_path=path,
            )

            import vendor_lookup_service as vls

            with (
                patch.object(vls, "load_settings", return_value=load_settings(path)),
                patch.dict(vls.VENDOR_SOURCES["eosl"], {"lookup_one": eosl_lookup}),
                patch.dict(vls.VENDOR_SOURCES["junos"], {"lookup_one": junos_lookup}),
                patch.dict(
                    vls.VENDOR_SOURCES["suse"],
                    {"lookup_one": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("suse"))},
                ),
                patch.dict(
                    vls.VENDOR_SOURCES["layer23-switch"],
                    {
                        "lookup_one": lambda *_a, **_k: (
                            _ for _ in ()
                        ).throw(AssertionError("layer23"))
                    },
                ),
                patch.dict(
                    vls.VENDOR_SOURCES["router-switch"],
                    {"lookup_one": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("rs"))},
                ),
            ):
                results = lookup_vendor_batch(
                    [
                        {
                            "os_string": "Juniper Junos 21.2",
                            "normalized_os_detailed_name": "",
                            "normalized_os": "Junos OS 21.2",
                        }
                    ]
                )
                self.assertEqual(results[0]["source"], "eosl")
                self.assertEqual(junos_calls["n"], 0)

    def test_disabled_router_switch_skipped(self) -> None:
        empty = {
            "eol_date": "",
            "eol_status": "",
            "eoas_date": "",
            "eoas_status": "",
            "source": "eosl",
            "api_note": "miss",
            "query_used": "Cisco IOS XE 17.9",
            "query_field": "os_string",
            "product_slug": "",
            "release_name": "",
            "release_label": "",
            "normalized_os_detailed_name": "",
            "normalized_os": "",
        }
        rs_calls = {"n": 0}

        def rs_lookup(*_args, **_kwargs):
            rs_calls["n"] += 1
            return empty

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vendor_lookup_settings.json"
            save_settings(
                {
                    "sources": {
                        "eosl": {"enabled": True, "keywords": []},
                        "junos": {"enabled": True, "keywords": ["junos"]},
                        "suse": {"enabled": True, "keywords": ["suse"]},
                        "layer23-switch": {"enabled": False, "keywords": ["cisco", "ios xe"]},
                        "router-switch": {"enabled": False, "keywords": ["cisco", "ios xe"]},
                    }
                },
                prefs_path=path,
            )
            import vendor_lookup_service as vls

            with (
                patch.object(vls, "load_settings", return_value=load_settings(path)),
                patch.dict(vls.VENDOR_SOURCES["eosl"], {"lookup_one": lambda *_a, **_k: empty}),
                patch.dict(
                    vls.VENDOR_SOURCES["junos"],
                    {"lookup_one": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("junos"))},
                ),
                patch.dict(
                    vls.VENDOR_SOURCES["suse"],
                    {"lookup_one": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("suse"))},
                ),
                patch.dict(
                    vls.VENDOR_SOURCES["layer23-switch"],
                    {"lookup_one": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("layer23"))},
                ),
                patch.dict(vls.VENDOR_SOURCES["router-switch"], {"lookup_one": rs_lookup}),
            ):
                lookup_vendor_batch(
                    [
                        {
                            "os_string": "Cisco IOS XE 17.9",
                            "normalized_os_detailed_name": "",
                            "normalized_os": "",
                        }
                    ]
                )
                self.assertEqual(rs_calls["n"], 0)

    def test_layer23_runs_before_router_switch(self) -> None:
        empty = {
            "eol_date": "",
            "eol_status": "",
            "eoas_date": "",
            "eoas_status": "",
            "source": "eosl",
            "api_note": "miss",
            "query_used": "Cisco Catalyst 9300",
            "query_field": "os_string",
            "product_slug": "",
            "release_name": "",
            "release_label": "",
            "normalized_os_detailed_name": "",
            "normalized_os": "",
        }
        layer23_hit = dict(
            empty,
            eol_date="1",
            eol_status="false",
            eoas_date="2",
            eoas_status="false",
            source="layer23-switch",
            api_note="",
            product_slug="cisco",
            release_name="C9300-24P-A",
            release_label="Cisco Catalyst 9300 (C9300-24P-A)",
        )
        layer23_calls = {"n": 0}
        router_calls = {"n": 0}

        def layer23_lookup(*_args, **_kwargs):
            layer23_calls["n"] += 1
            return layer23_hit

        def router_lookup(*_args, **_kwargs):
            router_calls["n"] += 1
            return empty

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vendor_lookup_settings.json"
            save_settings(
                {
                    "sources": {
                        "eosl": {"enabled": True, "keywords": []},
                        "junos": {"enabled": True, "keywords": ["junos"]},
                        "suse": {"enabled": True, "keywords": ["suse"]},
                        "layer23-switch": {"enabled": True, "keywords": ["cisco"]},
                        "router-switch": {"enabled": True, "keywords": ["cisco"]},
                    }
                },
                prefs_path=path,
            )
            import vendor_lookup_service as vls

            with (
                patch.object(vls, "load_settings", return_value=load_settings(path)),
                patch.dict(vls.VENDOR_SOURCES["eosl"], {"lookup_one": lambda *_a, **_k: empty}),
                patch.dict(
                    vls.VENDOR_SOURCES["junos"],
                    {"lookup_one": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("junos"))},
                ),
                patch.dict(
                    vls.VENDOR_SOURCES["suse"],
                    {"lookup_one": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("suse"))},
                ),
                patch.dict(vls.VENDOR_SOURCES["layer23-switch"], {"lookup_one": layer23_lookup}),
                patch.dict(vls.VENDOR_SOURCES["router-switch"], {"lookup_one": router_lookup}),
            ):
                results = lookup_vendor_batch(
                    [
                        {
                            "os_string": "Cisco Catalyst 9300",
                            "normalized_os_detailed_name": "",
                            "normalized_os": "",
                        }
                    ]
                )
                self.assertEqual(results[0]["source"], "layer23-switch")
                self.assertEqual(layer23_calls["n"], 1)
                self.assertEqual(router_calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
