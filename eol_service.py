"""endoflife.date lookup helpers for UI-added operating systems."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

import requests

from normalization_service import vendors_compatible

BASE_URL = "https://endoflife.date/api"
PRODUCTS_URL = f"{BASE_URL}/all.json"
PRODUCT_V1_URL = f"{BASE_URL}/v1/products"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
EOL_FETCH_WORKERS = 8

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


def join_labels(*parts: object) -> str:
    """Join product/release labels without duplicating a shared product prefix.

    endoflife.date sometimes returns release labels that already include the
    product name (e.g. product 'AlmaLinux OS' + release 'AlmaLinux OS 9').
    """
    cleaned = [part for part in (_clean(value) for value in parts) if part]
    if not cleaned:
        return ""

    result = cleaned[0]
    for part in cleaned[1:]:
        lower_result = result.lower()
        lower_part = part.lower()
        if lower_part == lower_result or lower_part.startswith(f"{lower_result} "):
            result = part
        elif lower_result == lower_part or lower_result.startswith(f"{lower_part} "):
            continue
        else:
            result = f"{result} {part}"
    return result


def pick_api_os_value(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
) -> str:
    value, _field = pick_api_os_value_with_field(
        os_string, normalized_os_detailed_name, normalized_os
    )
    return value


def pick_api_os_value_with_field(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
) -> tuple[str, str]:
    """Prefer normalized fields, but never query EOL with a cross-vendor value.

    If Normalized OS was wrongly set (e.g. AlmaLinux for Oracle Linux), fall
    back to the raw OS string so the correct product slug is resolved.
    """
    normalized = _clean(normalized_os)
    detailed = _clean(normalized_os_detailed_name)
    source = _clean(os_string)

    candidates: list[tuple[str, str]] = []
    if normalized:
        candidates.append((normalized, "normalized_os"))
    if detailed:
        candidates.append((detailed, "normalized_os_detailed_name"))
    if source:
        candidates.append((source, "os_string"))

    for value, field in candidates:
        if source and field != "os_string" and not vendors_compatible(source, value):
            continue
        return value, field

    if source:
        return source, "os_string"
    return "", ""


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


# Numbers that look like versions but are almost always architecture / bitness.
_NON_VERSION_HINTS = frozenset({"16", "32", "64", "86", "128", "256"})

# Accept only strong release matches (exact or multi-segment prefix).
_MIN_RELEASE_SCORE = 80


def extract_version_hints(os_name: str) -> list[str]:
    """Numeric version tokens suitable for release matching.

    Drops architecture bitness and lone service-pack / update markers
    (``SP3``, ``R2``, ``U1``) so they cannot drive a false release pick.
    """
    text = str(os_name or "")
    hints: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\d+(?:\.\d+)*", text):
        value = match.group()
        if value in seen or value in _NON_VERSION_HINTS:
            continue
        # "3.x or later" is a range, not version 3.
        if re.search(rf"(?<!\d){re.escape(value)}\.x\b", text, re.I):
            continue
        # Lone digit after SP / R / U is a pack marker, not a product version.
        if "." not in value:
            prefix = text[max(0, match.start() - 4) : match.start()]
            if re.search(r"(?:^|[^A-Za-z0-9])(?:SP|R|U)\s*$", prefix, re.I):
                continue
        seen.add(value)
        hints.append(value)
    return hints


def _version_parts(value: str) -> list[str]:
    return [part for part in str(value or "").split(".") if part != ""]


def _release_score(release_name: str, hint: str) -> int:
    """Score a release name against a version hint (dot-aware, no weak majors)."""
    if not release_name or not hint:
        return 0
    if release_name == hint:
        return 100

    rel_parts = _version_parts(release_name)
    hint_parts = _version_parts(hint)
    if not rel_parts or not hint_parts:
        return 0

    shorter = min(len(rel_parts), len(hint_parts))
    if rel_parts[:shorter] == hint_parts[:shorter]:
        # Bare major like "11" must not prefix-match "11.4".
        if len(hint_parts) == 1 and len(rel_parts) > 1:
            return 0
        # Hint "11.4.1" against release "11.4" (hint longer) — still a solid family hit.
        return 90

    # Shared major only when both sides are multi-part (e.g. 8.6 vs 8.4) — too weak alone.
    if len(hint_parts) > 1 and len(rel_parts) > 1 and rel_parts[0] == hint_parts[0]:
        return 55
    return 0


def pick_release(releases: list[dict[str, Any]], hints: list[str]) -> dict[str, Any]:
    """Pick a release only when version evidence is strong.

    - No version hints → no match (never guess the first/latest release).
    - Best score must be >= ``_MIN_RELEASE_SCORE``.
    """
    if not releases or not hints:
        return {}

    best_release: dict[str, Any] = {}
    best_score = 0
    for release in releases:
        release_name = str(release.get("name", "") or "")
        score = max((_release_score(release_name, hint) for hint in hints), default=0)
        if score > best_score:
            best_score = score
            best_release = release
    return best_release if best_score >= _MIN_RELEASE_SCORE else {}


def fetch_product(slug: str) -> dict[str, Any]:
    response = requests.get(f"{PRODUCT_V1_URL}/{slug}", headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Product response was not an object.")
    return payload


def has_api_date(date_value: Any) -> bool:
    if date_value in (None, "", False, True):
        return False
    cleaned = _clean(date_value)
    if not cleaned:
        return False
    try:
        datetime.strptime(cleaned, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def resolve_lifecycle_status(
    date_value: Any,
    api_status: Any,
    reference_date: str | None = None,
) -> str:
    """
    Status rules:
    - Date present -> leave status blank (date is enough)
    - Date missing and API status true -> "true"
    - Date missing and API status false -> "false"
    - Date missing and API status missing -> blank
    """
    if has_api_date(date_value):
        return ""

    if api_status is True:
        return "true"
    if api_status is False:
        return "false"
    return ""


def iso_date_to_epoch(iso_value: Any) -> str:
    cleaned = _clean(iso_value)
    if not cleaned:
        return ""
    try:
        parsed = datetime.strptime(cleaned, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp()))
    except ValueError:
        return ""


def build_normalization_from_product(
    product_result: dict[str, Any],
    release: dict[str, Any],
) -> dict[str, str]:
    product_label = _clean(product_result.get("label"))
    release_label = _clean(release.get("label"))
    release_name = _clean(release.get("name"))

    return {
        "normalized_os_detailed_name": join_labels(product_label, release_label),
        "normalized_os": join_labels(product_label, release_name),
    }


def lookup_os_eol(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
    valid_slugs: frozenset[str],
    product_cache: dict[str, dict[str, Any]],
    reference_date: str | None = None,
) -> dict[str, str]:
    today = reference_date or date.today().isoformat()
    cleaned_name, query_field = pick_api_os_value_with_field(
        os_string, normalized_os_detailed_name, normalized_os
    )

    empty_result = {
        "eol_date": "",
        "eol_status": "",
        "eoas_date": "",
        "eoas_status": "",
        "normalized_os_detailed_name": "",
        "normalized_os": "",
        "api_note": "",
        "query_used": cleaned_name,
        "query_field": query_field,
        "product_slug": "",
        "release_name": "",
        "release_label": "",
    }

    if not cleaned_name:
        empty_result["api_note"] = "No OS value available"
        return empty_result

    slug = resolve_product_slug(cleaned_name, valid_slugs)
    if not slug:
        empty_result["api_note"] = "Product not found in endoflife.date registry"
        return empty_result

    empty_result["product_slug"] = slug

    try:
        if slug not in product_cache:
            product_cache[slug] = fetch_product(slug)
        product_payload = product_cache[slug]
    except (requests.RequestException, ValueError) as exc:
        empty_result["api_note"] = f"API error: {exc}"
        return empty_result

    product_result = product_payload.get("result")
    if not isinstance(product_result, dict):
        empty_result["api_note"] = "Product details were missing from endoflife.date"
        return empty_result

    releases = product_result.get("releases")
    if not isinstance(releases, list) or not releases:
        empty_result["api_note"] = "No releases found in endoflife.date product data"
        return empty_result

    selected_release = pick_release(releases, extract_version_hints(cleaned_name))
    if not selected_release:
        empty_result["api_note"] = "No matching release found in endoflife.date product data"
        return empty_result

    product_label = _clean(product_result.get("label"))
    source = _clean(os_string)
    if source and product_label and not vendors_compatible(source, product_label):
        # Wrong product family (e.g. AlmaLinux for Oracle Linux). Retry once with OS string.
        if query_field != "os_string" and source != cleaned_name:
            return lookup_os_eol(
                os_string,
                "",
                "",
                valid_slugs,
                product_cache,
                reference_date=today,
            )
        empty_result["api_note"] = (
            f"EOL product '{product_label}' does not match OS vendor for '{source}'"
        )
        return empty_result

    eol_from = selected_release.get("eolFrom")
    eoas_from = selected_release.get("eoasFrom")
    normalization = build_normalization_from_product(product_result, selected_release)
    release_name = _clean(selected_release.get("name"))
    release_label = _clean(selected_release.get("label"))

    # Never push cross-vendor normalized names even if slug matched loosely.
    if source and not vendors_compatible(
        source,
        " ".join(
            [
                normalization["normalized_os_detailed_name"],
                normalization["normalized_os"],
            ]
        ),
    ):
        normalization = {
            "normalized_os_detailed_name": "",
            "normalized_os": "",
        }

    eol_date = iso_date_to_epoch(eol_from)
    eoas_date = iso_date_to_epoch(eoas_from)
    eol_status = resolve_lifecycle_status(eol_from, selected_release.get("isEol"), today)
    eoas_status = resolve_lifecycle_status(eoas_from, selected_release.get("isEoas"), today)

    return {
        "eol_date": eol_date,
        "eol_status": eol_status,
        "eoas_date": eoas_date,
        "eoas_status": eoas_status,
        "normalized_os_detailed_name": normalization["normalized_os_detailed_name"],
        "normalized_os": normalization["normalized_os"],
        "api_note": "",
        "query_used": cleaned_name,
        "query_field": query_field,
        "product_slug": slug,
        "release_name": release_name,
        "release_label": release_label,
    }


def lookup_os_eol_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
) -> list[dict[str, str]]:
    valid_slugs = get_valid_slugs()
    product_cache: dict[str, dict[str, Any]] = {}
    fetch_errors: dict[str, Exception] = {}

    slugs_needed: set[str] = set()
    for item in items:
        cleaned_name = pick_api_os_value(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
        )
        if not cleaned_name:
            continue
        slug = resolve_product_slug(cleaned_name, valid_slugs)
        if slug:
            slugs_needed.add(slug)

    if slugs_needed:
        workers = min(EOL_FETCH_WORKERS, len(slugs_needed))

        def fetch_slug(slug: str) -> tuple[str, dict[str, Any] | None, Exception | None]:
            try:
                return slug, fetch_product(slug), None
            except (requests.RequestException, ValueError) as exc:
                return slug, None, exc

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_slug, slug) for slug in slugs_needed]
            for future in as_completed(futures):
                slug, payload, error = future.result()
                if payload is not None:
                    product_cache[slug] = payload
                elif error is not None:
                    fetch_errors[slug] = error

    results: list[dict[str, str]] = []
    for item in items:
        cleaned_name = pick_api_os_value(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
        )
        slug = resolve_product_slug(cleaned_name, valid_slugs) if cleaned_name else None
        if slug and slug not in product_cache and slug in fetch_errors:
            _value, query_field = pick_api_os_value_with_field(
                item.get("os_string", ""),
                item.get("normalized_os_detailed_name", ""),
                item.get("normalized_os", ""),
            )
            results.append(
                {
                    "eol_date": "",
                    "eol_status": "",
                    "eoas_date": "",
                    "eoas_status": "",
                    "normalized_os_detailed_name": "",
                    "normalized_os": "",
                    "api_note": f"API error: {fetch_errors[slug]}",
                    "query_used": cleaned_name,
                    "query_field": query_field,
                    "product_slug": slug,
                    "release_name": "",
                    "release_label": "",
                }
            )
            continue

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
