"""Registry for local vendor lifecycle lookup sources (eosl.date, Junos, …)."""

from __future__ import annotations

from typing import Any, Callable

from eosl_service import (
    get_status as eosl_get_status,
    list_all_rows as eosl_list_all_rows,
    lookup_os_eosl,
    lookup_os_eosl_batch,
    sync_os_database as eosl_sync_os_database,
)
from junos_service import (
    get_status as junos_get_status,
    list_all_rows as junos_list_all_rows,
    lookup_os_junos,
    lookup_os_junos_batch,
    query_matches_junos,
    sync_junos_database,
)


VendorSource = dict[str, Any]

VENDOR_SOURCES: dict[str, VendorSource] = {
    "eosl": {
        "id": "eosl",
        "label": "eosl.date",
        "description": "OS lifecycle from eosl.date",
        "get_status": eosl_get_status,
        "list_rows": eosl_list_all_rows,
        "sync": eosl_sync_os_database,
        "lookup_batch": lookup_os_eosl_batch,
        "viewer_headers": {
            "product": "Product",
            "release": "Release",
            "released": "Released",
            "eol_date": "EOL",
            "eoas_date": "EOAS",
            "supported": "Supported",
        },
        "supported_as_type": False,
    },
    "junos": {
        "id": "junos",
        "label": "Juniper Junos",
        "description": "Junos OS Dates & Milestones (Juniper)",
        "get_status": junos_get_status,
        "list_rows": junos_list_all_rows,
        "sync": sync_junos_database,
        "lookup_batch": lookup_os_junos_batch,
        "viewer_headers": {
            "product": "Product",
            "release": "Release",
            "released": "FRS",
            "eol_date": "EOE (EOL)",
            "eoas_date": "EOS (EOAS)",
            "supported": "Supported",
        },
        "supported_as_type": False,
    },
}


def list_sources() -> list[dict[str, object]]:
    return [
        {
            "id": source["id"],
            "label": source["label"],
            "description": source["description"],
            "viewer_headers": source["viewer_headers"],
        }
        for source in VENDOR_SOURCES.values()
    ]


def get_source(source_id: str) -> VendorSource:
    source = VENDOR_SOURCES.get(source_id)
    if source is None:
        raise KeyError(source_id)
    return source


def get_status(source_id: str) -> dict[str, object]:
    source = get_source(source_id)
    status = dict(source["get_status"]())
    status.setdefault("source_id", source["id"])
    status.setdefault("source_label", source["label"])
    status["viewer_headers"] = source["viewer_headers"]
    return status


def list_rows(source_id: str) -> list[dict[str, object]]:
    return list(get_source(source_id)["list_rows"]())


def sync_source(
    source_id: str,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, object]:
    sync_fn = get_source(source_id)["sync"]
    if progress_callback is not None:
        return sync_fn(progress_callback=progress_callback)
    return sync_fn()


def _has_lifecycle_data(result: dict[str, str]) -> bool:
    return any(
        str(result.get(key) or "").strip()
        for key in ("eol_date", "eol_status", "eoas_date", "eoas_status")
    )


def lookup_vendor_batch(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Vendor fallback: Junos first for Juniper rows, then eosl.date; else eosl only."""
    results: list[dict[str, str]] = []
    for item in items:
        os_string = item.get("os_string", "")
        detailed = item.get("normalized_os_detailed_name", "")
        normalized = item.get("normalized_os", "")
        if query_matches_junos(os_string, detailed, normalized):
            junos_result = lookup_os_junos(os_string, detailed, normalized)
            if _has_lifecycle_data(junos_result):
                results.append(junos_result)
                continue
            eosl_result = lookup_os_eosl(os_string, detailed, normalized)
            if _has_lifecycle_data(eosl_result):
                results.append(eosl_result)
                continue
            # Prefer the Junos miss note when both miss (still the primary route).
            combined = dict(junos_result)
            eosl_note = str(eosl_result.get("api_note") or "").strip()
            if eosl_note:
                junos_note = str(combined.get("api_note") or "").strip()
                combined["api_note"] = (
                    f"{junos_note} eosl.date: {eosl_note}".strip()
                    if junos_note
                    else f"eosl.date: {eosl_note}"
                )
            results.append(combined)
        else:
            results.append(lookup_os_eosl(os_string, detailed, normalized))
    return results
