"""Persistent Vendor Lookup Refresh settings (enable flags + family keywords).

endoflife.date is always first and is not configured here.
Local fallback order is fixed: eosl → junos → suse → layer23-switch → router-switch.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Fixed Refresh order after endoflife.date (not user-configurable).
VENDOR_FALLBACK_ORDER: tuple[str, ...] = (
    "eosl",
    "junos",
    "suse",
    "layer23-switch",
    "router-switch",
)

_DEFAULT_SOURCES: dict[str, dict[str, Any]] = {
    "eosl": {
        "enabled": True,
        # Empty keywords → always eligible when enabled (general fallback).
        "keywords": [],
    },
    "junos": {
        "enabled": True,
        "keywords": ["junos", "juniper"],
    },
    "suse": {
        "enabled": True,
        "keywords": ["suse", "sles", "opensuse"],
    },
    "layer23-switch": {
        # Wired into Refresh but off by default (large hardware catalog).
        "enabled": False,
        "keywords": [
            "cisco",
            "arista",
            "aruba",
            "dell",
            "fortinet",
            "h3c",
            "hpe",
            "juniper",
            "mellanox",
            "palo alto",
            "palo-alto",
            "pan-os",
            "panos",
            "ruckus",
            "ios-xe",
            "ios xe",
            "ios-xr",
            "ios xr",
            "nx-os",
            "nxos",
        ],
    },
    "router-switch": {
        # Wired into Refresh but off by default (hardware-heavy catalog).
        "enabled": False,
        "keywords": [
            "cisco",
            "arista",
            "aruba",
            "dell",
            "fortinet",
            "h3c",
            "hpe",
            "juniper",
            "mellanox",
            "palo alto",
            "palo-alto",
            "pan-os",
            "panos",
            "ruckus",
            "ios-xe",
            "ios xe",
            "ios-xr",
            "ios xr",
            "nx-os",
            "nxos",
        ],
    },
}


def _default_settings() -> dict[str, Any]:
    return {
        "sources": deepcopy(_DEFAULT_SOURCES),
        "updated_at": "",
    }


def default_prefs_path() -> Path:
    return Path(__file__).resolve().parent / "_data" / "vendor_lookup_settings.json"


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_keyword_blob(value: object) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_source_entry(source_id: str, raw: object) -> dict[str, Any]:
    defaults = deepcopy(_DEFAULT_SOURCES.get(source_id, {"enabled": False, "keywords": []}))
    if not isinstance(raw, dict):
        return defaults
    enabled = raw.get("enabled", defaults["enabled"])
    defaults["enabled"] = bool(enabled)
    keywords_raw = raw.get("keywords", defaults["keywords"])
    if not isinstance(keywords_raw, list):
        keywords_raw = defaults["keywords"]
    keywords: list[str] = []
    seen: set[str] = set()
    for item in keywords_raw:
        word = _clean(item)
        key = word.lower()
        if not word or key in seen:
            continue
        seen.add(key)
        keywords.append(word)
    defaults["keywords"] = keywords
    return defaults


def load_settings(prefs_path: Path | None = None) -> dict[str, Any]:
    path = prefs_path or default_prefs_path()
    settings = _default_settings()
    if not path.is_file():
        return settings
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(payload, dict):
        return settings
    sources_raw = payload.get("sources")
    if not isinstance(sources_raw, dict):
        sources_raw = {}
    merged_sources: dict[str, dict[str, Any]] = {}
    for source_id in VENDOR_FALLBACK_ORDER:
        merged_sources[source_id] = _normalize_source_entry(
            source_id, sources_raw.get(source_id)
        )
    settings["sources"] = merged_sources
    settings["updated_at"] = _clean(payload.get("updated_at"))
    return settings


def save_settings(
    settings: dict[str, Any],
    prefs_path: Path | None = None,
) -> dict[str, Any]:
    path = prefs_path or default_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _default_settings()
    sources_in = settings.get("sources") if isinstance(settings, dict) else {}
    if not isinstance(sources_in, dict):
        sources_in = {}
    for source_id in VENDOR_FALLBACK_ORDER:
        normalized["sources"][source_id] = _normalize_source_entry(
            source_id, sources_in.get(source_id)
        )
    normalized["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    return normalized


def update_source_settings(
    source_id: str,
    *,
    enabled: bool | None = None,
    keywords: list[str] | None = None,
    prefs_path: Path | None = None,
) -> dict[str, Any]:
    if source_id not in VENDOR_FALLBACK_ORDER:
        raise KeyError(source_id)
    current = load_settings(prefs_path)
    entry = dict(current["sources"][source_id])
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if keywords is not None:
        entry["keywords"] = list(keywords)
    current["sources"][source_id] = entry
    return save_settings(current, prefs_path)


def source_is_enabled(source_id: str, settings: dict[str, Any] | None = None) -> bool:
    cfg = settings or load_settings()
    entry = cfg.get("sources", {}).get(source_id) or {}
    return bool(entry.get("enabled"))


def source_keywords(source_id: str, settings: dict[str, Any] | None = None) -> list[str]:
    cfg = settings or load_settings()
    entry = cfg.get("sources", {}).get(source_id) or {}
    raw = entry.get("keywords") or []
    return [str(item) for item in raw if str(item).strip()]


def query_matches_keywords(
    keywords: list[str],
    *values: object,
) -> bool:
    """True when keywords is empty, or any value contains any keyword as a token/phrase."""
    cleaned_keywords = [_normalize_keyword_blob(word) for word in keywords]
    cleaned_keywords = [word for word in cleaned_keywords if word]
    if not cleaned_keywords:
        return True

    blobs = [_normalize_keyword_blob(value) for value in values]
    blobs = [blob for blob in blobs if blob]
    if not blobs:
        return False

    for keyword in cleaned_keywords:
        pattern = (
            r"(?<![a-z0-9])"
            + re.escape(keyword).replace(r"\ ", r"\s+")
            + r"(?![a-z0-9])"
        )
        compiled = re.compile(pattern, re.I)
        if any(compiled.search(blob) for blob in blobs):
            return True
    return False


def source_matches_query(
    source_id: str,
    *values: object,
    settings: dict[str, Any] | None = None,
) -> bool:
    cfg = settings or load_settings()
    if not source_is_enabled(source_id, cfg):
        return False
    return query_matches_keywords(source_keywords(source_id, cfg), *values)
