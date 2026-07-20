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
PRODUCT_V1_URL = f"{BASE_URL}/v1/products"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
EOL_FETCH_WORKERS = 8

# Insert spaces at letter↔digit boundaries (Linux8.2 → linux 8.2).
_LETTER_DIGIT_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")

# Regex overrides run before the API phrase index (disambiguation only).
_SLUG_PRIORITY_OVERRIDES: list[tuple[str, str]] = [
    (r"windows[\s-]?server", "windows-server"),
    (r"cisco[\s-]?ios[\s-]?xe|\bios[\s-]?xe\b", "cisco-ios-xe"),
    (r"centos[\s-]?stream", "centos-stream"),
    (
        r"\brhel\b|"
        r"(?:red\s*hat|redhat)(?:\s+enterprise[s]?)?\s+linux\b|"
        r"(?:red\s*hat|redhat)\s+linux\b",
        "rhel",
    ),
    (r"\bopenshift\b|\bred[\s-]?hat[\s-]?openshift\b", "red-hat-openshift"),
]

# Inventory phrases not present as API labels/aliases (longest-match index).
_INVENTORY_PHRASE_EXTRAS: dict[str, tuple[str, ...]] = {
    "rhel": ("red hat linux", "redhat linux"),
    "sles": ("suse linux enterprise",),
    "amazon-linux": ("amzn",),
}

# Ignore very short slug phrases that cause false positives in free text.
_PHRASE_BLOCKLIST = frozenset({"go", "r", "xl", "z", "io", "os"})

# Common inventory typos / glued product tokens → spaced phrases.
_GLUED_PHRASE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"ubuntulinux", "ubuntu linux"),
    (r"redhatlinux", "red hat linux"),
    (r"rockylinux", "rocky linux"),
    (r"almalinux", "alma linux"),
    (r"oraclelinux", "oracle linux"),
    (r"amazonlinux", "amazon linux"),
    (r"windowsserver", "windows server"),
    (r"centosstream", "centos stream"),
    (r"suselinux", "suse linux"),
)

# Cached slug index entry: (phrase_length, slug, phrase, priority).
SlugIndexEntry = tuple[int, str, str, int]


def _normalize_phrase(value: str) -> str:
    text = _clean(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_for_slug_lookup(os_name: str) -> str:
    """Normalize messy inventory strings for product slug detection."""
    text = _clean(os_name).lower()
    text = text.replace("_", " ").replace("/", " ")
    for pattern, replacement in _GLUED_PHRASE_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.I)
    text = _LETTER_DIGIT_BOUNDARY_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(phrase.strip().lower())
    if " " in phrase:
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.I)
    return re.compile(rf"\b{escaped}\b", re.I)


def _add_index_phrase(
    entries: dict[tuple[str, str], SlugIndexEntry],
    slug: str,
    phrase: str,
    *,
    priority: int = 0,
) -> None:
    normalized = _normalize_phrase(phrase)
    if not normalized or normalized in _PHRASE_BLOCKLIST:
        return
    if len(normalized) < 3 and " " not in normalized:
        return
    key = (slug, normalized)
    candidate: SlugIndexEntry = (len(normalized), slug, normalized, priority)
    existing = entries.get(key)
    if existing is None or candidate[3] > existing[3]:
        entries[key] = candidate


def build_slug_index(products: list[dict[str, Any]]) -> tuple[SlugIndexEntry, ...]:
    """Build phrase → slug index from endoflife.date v1 product catalog."""
    entries: dict[tuple[str, str], SlugIndexEntry] = {}

    for product in products:
        slug = _clean(product.get("name"))
        if not slug:
            continue

        _add_index_phrase(entries, slug, slug.replace("-", " "))
        label = _clean(product.get("label"))
        if label:
            _add_index_phrase(entries, slug, label)

        for alias in product.get("aliases") or []:
            cleaned_alias = _clean(alias)
            if not cleaned_alias:
                continue
            _add_index_phrase(entries, slug, cleaned_alias)
            if "-" in cleaned_alias:
                _add_index_phrase(entries, slug, cleaned_alias.replace("-", " "))

    for slug, phrases in _INVENTORY_PHRASE_EXTRAS.items():
        for phrase in phrases:
            _add_index_phrase(entries, slug, phrase, priority=10)

    return tuple(sorted(entries.values(), key=lambda item: (-item[0], -item[3], item[1])))


@lru_cache(maxsize=1)
def get_product_catalog() -> tuple[dict[str, Any], ...]:
    response = requests.get(PRODUCT_V1_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Product catalog response was not an object.")
    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError("Product catalog result was not a list.")
    products: list[dict[str, Any]] = []
    for item in result:
        if isinstance(item, dict) and _clean(item.get("name")):
            products.append(item)
    return tuple(products)


@lru_cache(maxsize=1)
def get_slug_index() -> tuple[SlugIndexEntry, ...]:
    return build_slug_index(list(get_product_catalog()))


def _match_slug_from_index(
    text: str,
    valid_slugs: frozenset[str],
    slug_index: tuple[SlugIndexEntry, ...] | None = None,
) -> str | None:
    index = slug_index if slug_index is not None else get_slug_index()
    best: SlugIndexEntry | None = None
    for entry in index:
        slug = entry[1]
        if slug not in valid_slugs:
            continue
        phrase = entry[2]
        if not _phrase_pattern(phrase).search(text):
            continue
        if best is None or (entry[0], entry[3], entry[1]) > (best[0], best[3], best[1]):
            best = entry
    return best[1] if best else None


def resolve_product_slug(
    os_name: str,
    valid_slugs: frozenset[str],
    slug_index: tuple[SlugIndexEntry, ...] | None = None,
) -> str | None:
    normalized = _normalize_for_slug_lookup(os_name)
    if not normalized:
        return None

    for pattern, slug in _SLUG_PRIORITY_OVERRIDES:
        if slug in valid_slugs and re.search(pattern, normalized, re.IGNORECASE):
            return slug

    matched = _match_slug_from_index(normalized, valid_slugs, slug_index=slug_index)
    if matched:
        return matched

    hyphenated = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if hyphenated in valid_slugs:
        return hyphenated

    return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@lru_cache(maxsize=1)
def get_valid_slugs() -> frozenset[str]:
    return frozenset(product["name"] for product in get_product_catalog())


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
