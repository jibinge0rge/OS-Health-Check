"""Registry for local vendor lifecycle lookup sources (eosl.date, Junos, SUSE, …)."""

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
from suse_service import (
    get_status as suse_get_status,
    list_all_rows as suse_list_all_rows,
    lookup_os_suse,
    lookup_os_suse_batch,
    query_matches_suse,
    sync_suse_database,
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
    "suse": {
        "id": "suse",
        "label": "SUSE Lifecycle",
        "description": "SUSE product support lifecycle (suse.com/lifecycle)",
        "get_status": suse_get_status,
        "list_rows": suse_list_all_rows,
        "sync": sync_suse_database,
        "lookup_batch": lookup_os_suse_batch,
        "viewer_headers": {
            "product": "Product",
            "release": "Release",
            "released": "FCS",
            "eol_date": "General Ends (EOL)",
            "eoas_date": "LTSS Ends (EOAS)",
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


def _with_fallback_note(
    primary: dict[str, str],
    fallback: dict[str, str],
    fallback_label: str,
) -> dict[str, str]:
    combined = dict(primary)
    fallback_note = str(fallback.get("api_note") or "").strip()
    if fallback_note:
        primary_note = str(combined.get("api_note") or "").strip()
        combined["api_note"] = (
            f"{primary_note} {fallback_label}: {fallback_note}".strip()
            if primary_note
            else f"{fallback_label}: {fallback_note}"
        )
    return combined


def lookup_vendor_batch(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Vendor fallback routing.

    - Junos/Juniper → junos DB, then eosl.date
    - SUSE/SLES/openSUSE → suse DB, then eosl.date
    - otherwise → eosl.date only
    """
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
            results.append(_with_fallback_note(junos_result, eosl_result, "eosl.date"))
            continue

        if query_matches_suse(os_string, detailed, normalized):
            suse_result = lookup_os_suse(os_string, detailed, normalized)
            if _has_lifecycle_data(suse_result):
                results.append(suse_result)
                continue
            eosl_result = lookup_os_eosl(os_string, detailed, normalized)
            if _has_lifecycle_data(eosl_result):
                results.append(eosl_result)
                continue
            results.append(_with_fallback_note(suse_result, eosl_result, "eosl.date"))
            continue

        results.append(lookup_os_eosl(os_string, detailed, normalized))
    return results
