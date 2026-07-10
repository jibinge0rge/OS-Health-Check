"""endoflife.date lookup helpers for UI-added operating systems."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

import requests

BASE_URL = "https://endoflife.date/api"
PRODUCTS_URL = f"{BASE_URL}/all.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

SLUG_RULES: list[tuple[str, str]] = [
    (r"red hat enterprise linux|\brhel\b", "rhel"),
    (r"rocky[\s-]?linux", "rocky-linux"),
    (r"ubuntu", "ubuntu"),
    (r"debian", "debian"),
    (r"windows server", "windows-server"),
    (r"\bwindows\b", "windows"),
    (r"mac os x|\bmacos\b", "macos"),
    (r"\bios\b|iphone", "ios"),
    (r"android", "android"),
    (r"vmware esxi|\besxi\b", "esxi"),
    (r"fortios|fortinet", "fortios"),
    (r"cisco ios[\s-]?xe|ios[\s-]?xe", "cisco-ios-xe"),
    (r"almalinux", "almalinux"),
    (r"centos", "centos"),
    (r"amazon linux", "amazon-linux"),
    (r"suse|sles", "sles"),
    (r"oracle linux", "oracle-linux"),
    (r"fedora", "fedora"),
]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def pick_api_os_value(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
) -> str:
    normalized = _clean(normalized_os)
    detailed = _clean(normalized_os_detailed_name)
    source = _clean(os_string)

    if normalized:
        return normalized
    if detailed:
        return detailed
    return source


@lru_cache(maxsize=1)
def get_valid_slugs() -> frozenset[str]:
    response = requests.get(PRODUCTS_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return frozenset(response.json())


def resolve_product_slug(os_name: str, valid_slugs: frozenset[str]) -> str | None:
    lowered = os_name.lower()
    for pattern, slug in SLUG_RULES:
        if slug in valid_slugs and re.search(pattern, lowered, re.IGNORECASE):
            return slug

    normalized = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if normalized in valid_slugs:
        return normalized

    return None


def extract_version_hints(os_name: str) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\d+(?:\.\d+)*", os_name):
        value = match.group()
        if value not in seen:
            seen.add(value)
            hints.append(value)
    return hints


def _cycle_score(cycle: str, hint: str) -> int:
    if cycle == hint:
        return 100
    if cycle.startswith(hint) or hint.startswith(cycle):
        return 80
    if cycle.split(".")[0] == hint.split(".")[0]:
        return 60
    if hint in cycle:
        return 40
    return 0


def pick_cycle(cycles: list[dict[str, Any]], hints: list[str]) -> dict[str, Any]:
    if not cycles:
        return {}
    if not hints:
        return cycles[0]

    best_cycle = cycles[0]
    best_score = -1
    for row in cycles:
        cycle = str(row.get("cycle", ""))
        score = max((_cycle_score(cycle, hint) for hint in hints), default=0)
        if score > best_score:
            best_score = score
            best_cycle = row
    return best_cycle


def fetch_cycles(slug: str) -> list[dict[str, Any]]:
    response = requests.get(f"{BASE_URL}/{slug}.json", headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()


def status_from_date(date_value: Any, reference_date: str) -> str:
    if date_value in (None, "", False, True):
        return ""
    return "true" if str(date_value) < reference_date else "false"


def iso_date_to_epoch(iso_value: Any) -> str:
    cleaned = _clean(iso_value)
    if not cleaned:
        return ""
    try:
        parsed = datetime.strptime(cleaned, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp()))
    except ValueError:
        return ""


def lookup_os_eol(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
    valid_slugs: frozenset[str],
    product_cache: dict[str, list[dict[str, Any]]],
    reference_date: str | None = None,
) -> dict[str, str]:
    today = reference_date or date.today().isoformat()
    cleaned_name = pick_api_os_value(os_string, normalized_os_detailed_name, normalized_os)

    empty_result = {
        "eol_date": "",
        "eol_status": "",
        "eoas_date": "",
        "eoas_status": "",
        "api_note": "",
    }

    if not cleaned_name:
        empty_result["api_note"] = "No OS value available"
        return empty_result

    slug = resolve_product_slug(cleaned_name, valid_slugs)
    if not slug:
        empty_result["api_note"] = "Product not found in endoflife.date registry"
        return empty_result

    try:
        if slug not in product_cache:
            product_cache[slug] = fetch_cycles(slug)
        cycles = product_cache[slug]
    except requests.RequestException as exc:
        empty_result["api_note"] = f"API error: {exc}"
        return empty_result

    selected_cycle = pick_cycle(cycles, extract_version_hints(cleaned_name))
    eol_value = selected_cycle.get("eol")
    extended_support = selected_cycle.get("extendedSupport")

    eol_date = iso_date_to_epoch(eol_value)
    eoas_date = iso_date_to_epoch(extended_support)
    eol_status = status_from_date(eol_value, today)
    eoas_status = status_from_date(extended_support, today)

    return {
        "eol_date": eol_date,
        "eol_status": eol_status,
        "eoas_date": eoas_date,
        "eoas_status": eoas_status,
        "api_note": "",
    }


def lookup_os_eol_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
) -> list[dict[str, str]]:
    valid_slugs = get_valid_slugs()
    product_cache: dict[str, list[dict[str, Any]]] = {}
    results: list[dict[str, str]] = []

    for item in items:
        results.append(
            lookup_os_eol(
                os_string=item.get("os_string", ""),
                normalized_os_detailed_name=item.get("normalized_os_detailed_name", ""),
                normalized_os=item.get("normalized_os", ""),
                valid_slugs=valid_slugs,
                product_cache=product_cache,
                reference_date=reference_date,
            )
        )

    return results
