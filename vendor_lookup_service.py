"""Registry for local vendor lifecycle lookup sources (eosl.date, Junos, SUSE, …).

Router-Switch is registered for the Vendor Lookups viewer/sync only — it is not
part of ``lookup_vendor_batch`` / Refresh EOL/EOAS routing.
"""

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
from router_switch_service import (
    get_status as router_switch_get_status,
    list_all_rows as router_switch_list_all_rows,
    list_manufacturers as router_switch_list_manufacturers,
    manufacturers_from_slugs as router_switch_manufacturers_from_slugs,
    save_selected_manufacturers as router_switch_save_selected_manufacturers,
    sync_router_switch_database,
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
    # Viewer-only: scraped for browsing; not used by Refresh EOL/EOAS routing.
    "router-switch": {
        "id": "router-switch",
        "label": "Router-Switch EOL",
        "description": (
            "Hardware EOL/EOSL from router-switch.com (viewer only; "
            "not applied to inventory dates)"
        ),
        "get_status": router_switch_get_status,
        "list_rows": router_switch_list_all_rows,
        "sync": sync_router_switch_database,
        "manufacturers": router_switch_list_manufacturers(),
        "viewer_headers": {
            "product": "Product Name",
            "release": "Part Number",
            "released": "End of Sale (EOS)",
            "eol_date": "EOL Announcement",
            "eoas_date": "End of Service Life (EOSL)",
            "supported": "Supported",
        },
        "supported_as_type": False,
    },
}


def list_sources() -> list[dict[str, object]]:
    sources: list[dict[str, object]] = []
    for source in VENDOR_SOURCES.values():
        entry: dict[str, object] = {
            "id": source["id"],
            "label": source["label"],
            "description": source["description"],
            "viewer_headers": source["viewer_headers"],
        }
        manufacturers = source.get("manufacturers")
        if manufacturers:
            entry["manufacturers"] = manufacturers
        sources.append(entry)
    return sources


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
    manufacturers = source.get("manufacturers")
    if manufacturers and "manufacturers" not in status:
        status["manufacturers"] = manufacturers
    return status


def save_source_preferences(
    source_id: str,
    options: dict[str, object] | None = None,
) -> dict[str, object]:
    """Persist source-specific UI preferences (Router-Switch manufacturers)."""
    options = options or {}
    if source_id != "router-switch":
        raise KeyError(source_id)
    slugs = options.get("manufacturers")
    if not isinstance(slugs, list) or not slugs:
        raise ValueError("Select at least one manufacturer.")
    saved = router_switch_save_selected_manufacturers([str(s) for s in slugs])
    return {
        "source_id": source_id,
        "manufacturers": saved,
        "selected_manufacturers": saved,
    }


def list_rows(source_id: str) -> list[dict[str, object]]:
    return list(get_source(source_id)["list_rows"]())


def sync_source(
    source_id: str,
    progress_callback: Callable[[str, int, int], None] | None = None,
    options: dict[str, object] | None = None,
) -> dict[str, object]:
    sync_fn = get_source(source_id)["sync"]
    options = options or {}
    kwargs: dict[str, object] = {}
    if progress_callback is not None:
        kwargs["progress_callback"] = progress_callback
    if source_id == "router-switch":
        slugs = options.get("manufacturers")
        if slugs is not None:
            kwargs["manufacturers"] = router_switch_manufacturers_from_slugs(
                [str(s) for s in slugs]  # type: ignore[arg-type]
            )
    if kwargs:
        return sync_fn(**kwargs)
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
