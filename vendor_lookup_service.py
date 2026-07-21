"""Registry for local vendor lifecycle lookup sources + Refresh fallback routing.

Fixed order after endoflife.date (not configurable):
  eosl → junos → suse → layer23-switch → router-switch

Per-source enable flags and family keywords are persisted in
``_data/vendor_lookup_settings.json`` (see ``vendor_settings``).
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
    sync_junos_database,
)
from layer23_switch_service import (
    get_status as layer23_switch_get_status,
    list_all_rows as layer23_switch_list_all_rows,
    list_manufacturers as layer23_switch_list_manufacturers,
    lookup_os_layer23_switch,
    lookup_os_layer23_switch_batch,
    manufacturers_from_slugs as layer23_switch_manufacturers_from_slugs,
    save_selected_manufacturers as layer23_switch_save_selected_manufacturers,
    sync_layer23_switch_database,
)
from router_switch_service import (
    get_status as router_switch_get_status,
    list_all_rows as router_switch_list_all_rows,
    list_manufacturers as router_switch_list_manufacturers,
    lookup_os_router_switch,
    lookup_os_router_switch_batch,
    manufacturers_from_slugs as router_switch_manufacturers_from_slugs,
    save_selected_manufacturers as router_switch_save_selected_manufacturers,
    sync_router_switch_database,
)
from suse_service import (
    get_status as suse_get_status,
    list_all_rows as suse_list_all_rows,
    lookup_os_suse,
    lookup_os_suse_batch,
    sync_suse_database,
)
from vendor_settings import (
    VENDOR_FALLBACK_ORDER,
    load_settings,
    save_settings,
    source_is_enabled,
    source_keywords,
    source_matches_query,
    update_source_settings,
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
        "lookup_one": lookup_os_eosl,
        "uses_keywords": False,
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
        "lookup_one": lookup_os_junos,
        "uses_keywords": True,
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
        "lookup_one": lookup_os_suse,
        "uses_keywords": True,
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
    "layer23-switch": {
        "id": "layer23-switch",
        "label": "Layer23-Switch EOL",
        "description": (
            "Hardware/OS EOL/EOSL from layer23-switch.com "
            "(disabled for Refresh by default)"
        ),
        "get_status": layer23_switch_get_status,
        "list_rows": layer23_switch_list_all_rows,
        "sync": sync_layer23_switch_database,
        "lookup_batch": lookup_os_layer23_switch_batch,
        "lookup_one": lookup_os_layer23_switch,
        "uses_keywords": True,
        "manufacturers": layer23_switch_list_manufacturers(),
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
    "router-switch": {
        "id": "router-switch",
        "label": "Router-Switch EOL",
        "description": (
            "Hardware/OS EOL/EOSL from router-switch.com "
            "(disabled for Refresh by default)"
        ),
        "get_status": router_switch_get_status,
        "list_rows": router_switch_list_all_rows,
        "sync": sync_router_switch_database,
        "lookup_batch": lookup_os_router_switch_batch,
        "lookup_one": lookup_os_router_switch,
        "uses_keywords": True,
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
    settings = load_settings()
    sources: list[dict[str, object]] = []
    for source_id in VENDOR_FALLBACK_ORDER:
        source = VENDOR_SOURCES[source_id]
        entry: dict[str, object] = {
            "id": source["id"],
            "label": source["label"],
            "description": source["description"],
            "viewer_headers": source["viewer_headers"],
            "uses_keywords": bool(source.get("uses_keywords")),
            "enabled": source_is_enabled(source_id, settings),
            "keywords": source_keywords(source_id, settings),
            "fallback_order": list(VENDOR_FALLBACK_ORDER),
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
    settings = load_settings()
    status = dict(source["get_status"]())
    status.setdefault("source_id", source["id"])
    status.setdefault("source_label", source["label"])
    status["viewer_headers"] = source["viewer_headers"]
    manufacturers = source.get("manufacturers")
    if manufacturers and "manufacturers" not in status:
        status["manufacturers"] = manufacturers
    status["uses_keywords"] = bool(source.get("uses_keywords"))
    status["enabled"] = source_is_enabled(source_id, settings)
    status["keywords"] = source_keywords(source_id, settings)
    status["fallback_order"] = list(VENDOR_FALLBACK_ORDER)
    return status


def get_lookup_settings() -> dict[str, object]:
    settings = load_settings()
    return {
        "fallback_order": list(VENDOR_FALLBACK_ORDER),
        "api_first": True,
        "sources": {
            source_id: {
                "enabled": source_is_enabled(source_id, settings),
                "keywords": source_keywords(source_id, settings),
                "uses_keywords": bool(VENDOR_SOURCES[source_id].get("uses_keywords")),
                "label": VENDOR_SOURCES[source_id]["label"],
            }
            for source_id in VENDOR_FALLBACK_ORDER
        },
        "updated_at": settings.get("updated_at", ""),
    }


def save_lookup_settings(payload: dict[str, object] | None = None) -> dict[str, object]:
    """Persist enable/keywords for all vendor sources from the Settings dialog."""
    payload = payload or {}
    sources_in = payload.get("sources")
    if not isinstance(sources_in, dict):
        raise ValueError("sources object is required.")
    current = load_settings()
    merged = {"sources": dict(current.get("sources") or {})}
    for source_id in VENDOR_FALLBACK_ORDER:
        incoming = sources_in.get(source_id)
        if not isinstance(incoming, dict):
            continue
        entry = dict(merged["sources"].get(source_id) or {})
        if "enabled" in incoming:
            entry["enabled"] = bool(incoming.get("enabled"))
        if "keywords" in incoming:
            raw = incoming.get("keywords")
            if isinstance(raw, list):
                entry["keywords"] = [str(item) for item in raw]
            elif isinstance(raw, str):
                entry["keywords"] = [
                    part.strip() for part in raw.split(",") if part.strip()
                ]
        merged["sources"][source_id] = entry
    save_settings(merged)
    return get_lookup_settings()


def save_source_preferences(
    source_id: str,
    options: dict[str, object] | None = None,
) -> dict[str, object]:
    """Persist source-specific preferences including manufacturer selection."""
    options = options or {}
    get_source(source_id)
    result: dict[str, object] = {"source_id": source_id}

    enabled = options.get("enabled")
    keywords = options.get("keywords")
    if enabled is not None or keywords is not None:
        settings = update_source_settings(
            source_id,
            enabled=bool(enabled) if enabled is not None else None,
            keywords=[str(item) for item in keywords] if isinstance(keywords, list) else None,
        )
        result["enabled"] = source_is_enabled(source_id, settings)
        result["keywords"] = source_keywords(source_id, settings)

    if source_id in {"layer23-switch", "router-switch"} and "manufacturers" in options:
        slugs = options.get("manufacturers")
        if not isinstance(slugs, list) or not slugs:
            raise ValueError("Select at least one manufacturer.")
        save_selected = (
            layer23_switch_save_selected_manufacturers
            if source_id == "layer23-switch"
            else router_switch_save_selected_manufacturers
        )
        saved = save_selected([str(s) for s in slugs])
        result["manufacturers"] = saved
        result["selected_manufacturers"] = saved

    if "enabled" not in result and "manufacturers" not in result:
        raise ValueError("No preferences provided.")
    return result


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
    if source_id in {"layer23-switch", "router-switch"}:
        slugs = options.get("manufacturers")
        if slugs is not None:
            manufacturers_from_slugs = (
                layer23_switch_manufacturers_from_slugs
                if source_id == "layer23-switch"
                else router_switch_manufacturers_from_slugs
            )
            kwargs["manufacturers"] = manufacturers_from_slugs(
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


def _empty_vendor_result(
    os_string: str,
    detailed: str,
    normalized: str,
) -> dict[str, str]:
    return {
        "eol_date": "",
        "eol_status": "",
        "eoas_date": "",
        "eoas_status": "",
        "normalized_os_detailed_name": "",
        "normalized_os": "",
        "api_note": "All vendor lookups disabled or no match",
        "query_used": normalized or detailed or os_string,
        "query_field": "",
        "product_slug": "",
        "release_name": "",
        "release_label": "",
        "source": "",
    }


def lookup_vendor_batch(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Vendor fallback after endoflife.date.

    Fixed order: eosl → junos → suse → layer23-switch → router-switch.
    Specialists (junos/suse/layer23-switch/router-switch) only run when enabled and keywords match.
    eosl runs when enabled (no keyword gate).
    """
    settings = load_settings()
    results: list[dict[str, str]] = []

    for item in items:
        os_string = item.get("os_string", "")
        detailed = item.get("normalized_os_detailed_name", "")
        normalized = item.get("normalized_os", "")
        values = (os_string, detailed, normalized)

        chosen: dict[str, str] | None = None
        notes_from_misses: list[dict[str, str]] = []

        for source_id in VENDOR_FALLBACK_ORDER:
            source = VENDOR_SOURCES[source_id]
            if not source_is_enabled(source_id, settings):
                continue
            if source.get("uses_keywords") and not source_matches_query(
                source_id, *values, settings=settings
            ):
                continue

            lookup_one = source["lookup_one"]
            candidate = lookup_one(os_string, detailed, normalized)
            if _has_lifecycle_data(candidate):
                chosen = candidate
                break
            notes_from_misses.append(candidate)

        if chosen is not None:
            results.append(chosen)
            continue

        if notes_from_misses:
            combined = notes_from_misses[0]
            for miss in notes_from_misses[1:]:
                label = str(miss.get("source") or "vendor")
                combined = _with_fallback_note(combined, miss, label)
            results.append(combined)
        else:
            results.append(_empty_vendor_result(os_string, detailed, normalized))

    return results
