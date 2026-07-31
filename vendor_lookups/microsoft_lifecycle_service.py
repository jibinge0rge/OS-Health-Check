"""Scrape Microsoft product lifecycle data into a PostgreSQL vendor schema.

Unlike eosl/suse (HTML table scraping), the search page at
https://learn.microsoft.com/en-us/lifecycle/products/ is backed by a plain
JSON API (``/api/contentbrowser/search/lifecycles``) that already returns one
row per named product (title, start, end, product family) with no HTML
parsing required. The API caps ``$top`` at 30 results per page, so the full
~800-product catalog is paginated via ``$skip``.

Product/release mapping:
  product family (e.g. "windows", "sql-server") -> products.slug/name
  named product (e.g. "SQL Server 2025")        -> releases.release_name
  start / end (date only, UTC)                  -> released_date / eol_date

The API's ``end`` already matches the Extended End Date shown on each
product's own detail page (verified against several products), so no
detail-page scraping is needed. Only one end date is available per row, so
``eoas_date`` is left blank (same "single date -> eol_date" convention eosl
uses for products with a single lifecycle column).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urljoin

import psycopg
import requests

from .db import connection_for, init_source_schema, set_metadata
from eol_service import (
    extract_version_hints,
    iso_date_to_epoch,
    join_labels,
    pick_api_os_value_with_field,
    resolve_lifecycle_status,
)
from normalization_service import vendors_compatible
from version_match import score_release_against_hint

SOURCE_ID = "microsoft-lifecycle"

BASE_URL = "https://learn.microsoft.com"
PRODUCTS_INDEX_URL = f"{BASE_URL}/en-us/lifecycle/products/"
SEARCH_API_URL = f"{BASE_URL}/api/contentbrowser/search/lifecycles"
HEADERS = {
    "User-Agent": "OS-Health-Check/1.0 (+local microsoft lifecycle scraper; internal tool)"
}
# The API 400s above $top=30; confirmed empirically (30 ok, 35 rejected).
PAGE_SIZE = 30
REQUEST_DELAY_SECONDS = 0.2
CATEGORY = "microsoft"

_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _clean(value: object) -> str:
    return str(value or "").strip()


def init_db(schema_name: str | None = None) -> None:
    init_source_schema(SOURCE_ID, schema_name)


@contextmanager
def _connect(schema_name: str | None = None) -> Iterator[psycopg.Connection[Any]]:
    with connection_for(SOURCE_ID, schema_override=schema_name) as connection:
        yield connection


def _set_metadata(connection: psycopg.Connection[Any], key: str, value: str) -> None:
    set_metadata(connection, key, value)


def get_status(schema_name: str | None = None) -> dict[str, object]:
    init_db(schema_name)
    with _connect(schema_name) as connection:
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        product_count = connection.execute(
            "SELECT COUNT(*) AS count FROM products"
        ).fetchone()["count"]
        release_count = connection.execute(
            "SELECT COUNT(*) AS count FROM releases"
        ).fetchone()["count"]
    return {
        "last_updated": meta.get("last_updated", ""),
        "last_sync_status": meta.get("last_sync_status", ""),
        "last_sync_message": meta.get("last_sync_message", ""),
        "product_count": int(product_count),
        "release_count": int(release_count),
        "source_url": PRODUCTS_INDEX_URL,
        "category": CATEGORY,
        "source_id": SOURCE_ID,
        "source_label": "Microsoft Lifecycle",
    }


def _iso_date_only(value: object) -> str:
    """First 10 chars (YYYY-MM-DD) of an API timestamp.

    The API's start/end timestamps are UTC-normalized Pacific Time cutoffs
    (e.g. ``...T06:59:59.999Z``), so the calendar date shown here can be one
    day off from the Pacific date on learn.microsoft.com. That's acceptable
    for EOL/EOAS tracking at day granularity.
    """
    match = _ISO_DATE_RE.match(_clean(value))
    return match.group(1) if match else ""


def _fetch_page(skip: int, top: int = PAGE_SIZE) -> dict[str, Any]:
    params: dict[str, str] = {
        "locale": "en-us",
        "facet": "products",
        "$orderBy": "start desc",
        "$top": str(top),
        "fuzzySearch": "false",
    }
    if skip:
        params["$skip"] = str(skip)
    response = requests.get(SEARCH_API_URL, params=params, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Microsoft Lifecycle API response was not an object.")
    return payload


def collect_products(
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Paginate the search API and return every raw result item.

    ``max_pages`` bounds the number of requests (used by tests / a quick
    partial sync); leave it unset to walk the whole catalog.
    """
    items: list[dict[str, Any]] = []
    skip = 0
    total: int | None = None
    page = 0

    while True:
        if cancel_event is not None and cancel_event.is_set():
            break
        if max_pages is not None and page >= max_pages:
            break
        payload = _fetch_page(skip)
        if total is None:
            count = payload.get("count")
            total = int(count) if isinstance(count, int) else None
        batch = payload.get("results")
        if not isinstance(batch, list) or not batch:
            break
        items.extend(entry for entry in batch if isinstance(entry, dict))
        page += 1
        if progress_callback:
            total_pages = max(1, -(-(total or len(items)) // PAGE_SIZE))
            if max_pages is not None:
                total_pages = min(total_pages, max_pages)
            progress_callback(f"page {page}", page, total_pages)
        skip += len(batch)
        if total is not None and skip >= total:
            break
        if not _clean(payload.get("@nextLink")):
            break
        if max_pages is not None and page >= max_pages:
            break
        time.sleep(REQUEST_DELAY_SECONDS)

    return items


def _family_slug_and_name(item: dict[str, Any]) -> tuple[str, str]:
    products = item.get("products")
    slug = _clean(products[0]) if isinstance(products, list) and products else ""
    display = item.get("display_products")
    name = _clean(display[0]) if isinstance(display, list) and display else ""
    if not slug:
        title = _clean(item.get("title"))
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "microsoft"
    if not name:
        name = _clean(item.get("title")) or slug
    return slug, name


def build_release_rows(
    items: list[dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Group raw API results into (family slug -> name, list of release rows)."""
    products: dict[str, str] = {}
    releases: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    today = date.today().isoformat()

    for item in items:
        slug, name = _family_slug_and_name(item)
        if not slug:
            continue
        products.setdefault(slug, name)

        title = _clean(item.get("title"))
        if not title:
            continue
        key = (slug, title.lower())
        if key in seen:
            continue
        seen.add(key)

        released_date = _iso_date_only(item.get("start"))
        eol_date = _iso_date_only(item.get("end"))
        url_path = _clean(item.get("url"))
        full_url = urljoin(BASE_URL, url_path) if url_path else ""
        is_supported = 1 if (not eol_date or eol_date >= today) else 0

        releases.append(
            {
                "product_slug": slug,
                "release_name": title,
                "released_date": released_date,
                "eol_date": eol_date,
                "eoas_date": "",
                "latest_raw": full_url,
                "is_supported": str(is_supported),
            }
        )

    return products, releases


def sync_microsoft_lifecycle_database(
    schema_name: str | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
    max_pages: int | None = None,
) -> dict[str, object]:
    init_db(schema_name)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if cancel_event is not None and cancel_event.is_set():
        return {
            "ok": False,
            "product_count": 0,
            "release_count": 0,
            "started": started,
            "finished": started,
            "source_url": PRODUCTS_INDEX_URL,
            "cancelled": True,
        }

    items = collect_products(
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        max_pages=max_pages,
    )
    cancelled = cancel_event is not None and cancel_event.is_set()

    # Cancelled before the catalog finished paginating: `items` is a partial
    # slice of the whole ~800-product list, never a valid replacement for
    # what's already stored. Leave the existing (possibly complete) dataset
    # untouched rather than committing a partial one over it.
    if cancelled:
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _connect(schema_name) as connection:
            product_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM products").fetchone()["count"]
            )
            release_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM releases").fetchone()["count"]
            )
            _set_metadata(connection, "last_sync_started", started)
            _set_metadata(connection, "last_sync_status", "cancelled")
            _set_metadata(
                connection,
                "last_sync_message",
                f"Cancelled after fetching {len(items)} raw entries; "
                "existing data left unchanged.",
            )
        return {
            "ok": False,
            "product_count": product_count,
            "release_count": release_count,
            "started": started,
            "finished": finished,
            "source_url": PRODUCTS_INDEX_URL,
            "cancelled": True,
        }

    if not items:
        raise ValueError("Microsoft Lifecycle API returned zero products.")

    products, releases = build_release_rows(items)

    if progress_callback:
        progress_callback("store", 1, 1)

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(schema_name) as connection:
        connection.execute("DELETE FROM releases")
        connection.execute("DELETE FROM products")
        for slug, name in products.items():
            connection.execute(
                """
                INSERT INTO products(slug, name, category, url, scraped_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (slug, name, CATEGORY, f"{PRODUCTS_INDEX_URL}?products={slug}", scraped_at),
            )
        for release in releases:
            connection.execute(
                """
                INSERT INTO releases(
                    product_slug, release_name, released_date,
                    eol_date, eoas_date, latest_raw, is_supported
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    release["product_slug"],
                    release["release_name"],
                    release.get("released_date", ""),
                    release.get("eol_date", ""),
                    release.get("eoas_date", ""),
                    release.get("latest_raw", ""),
                    int(release.get("is_supported", "0") or 0),
                ),
            )
        _set_metadata(connection, "last_updated", scraped_at)
        _set_metadata(connection, "last_sync_started", started)
        _set_metadata(connection, "last_sync_status", "ok")
        _set_metadata(
            connection,
            "last_sync_message",
            f"Synced {len(products)} products / {len(releases)} releases "
            "from Microsoft Lifecycle.",
        )

    return {
        "ok": True,
        "product_count": len(products),
        "release_count": len(releases),
        "started": started,
        "finished": scraped_at,
        "source_url": PRODUCTS_INDEX_URL,
        "cancelled": False,
    }


# Numbers that look like versions but are almost always architecture / bitness.
_NON_VERSION_HINTS = frozenset({"16", "32", "64", "86", "128", "256"})
_MIN_RELEASE_SCORE = 80

# The "windows" family is deliberately never matched here. endoflife.date's
# own dedicated "windows" and "windows-server" products already cover this
# ground far more precisely (raw NT build via latest.name, edition-aware
# tie-breaking -- see eol_service.py's pick_release) and are always tried
# first in the Refresh cascade. Microsoft Lifecycle's own "windows" family,
# by contrast, mixes real OS releases with unrelated tools (PowerShell,
# FSLogix, Windows Defender Exploit Guard, Microsoft Robotics, ...) under one
# family, and every OS release's own name is a slug like "10-1709-w" whose
# extracted numeric tokens include the bare major ("10") shared by EVERY
# Windows 10/11 release. A bare-major hint (e.g. from a normalized_os value
# with no build number) then scores an exact-string-match 100 against that
# shared token for dozens of unrelated releases at once -- there is no
# genuine signal left to pick among them, so a real incident matched a
# 2017 build (10.0.16299) to "Windows 10 IoT Enterprise LTSC 2021" (a 2021
# release) purely because both names happen to start with "10". The data is
# still scraped/stored for reference in the Vendor Lookups viewer; it is
# only excluded from automated matching.
_EXCLUDED_FAMILIES = frozenset({"windows"})


def _version_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"\d+(?:\.\d+)*", text or "")
        if token not in _NON_VERSION_HINTS
    ]


def _release_score(release_name: str, hint: str) -> int:
    best = 0
    for candidate in _version_tokens(release_name):
        best = max(best, score_release_against_hint(candidate, hint))
    return best


def _pick_release(
    releases: list[Mapping[str, Any]], hints: list[str]
) -> Mapping[str, Any] | None:
    """Conservative on ties as well as on weak scores: if two or more
    releases score exactly the same best score, there is no real signal
    telling them apart -- refuse to guess rather than silently picking
    whichever one happened to sort first."""
    if not releases or not hints:
        return None
    best: list[Mapping[str, Any]] = []
    best_score = 0
    for release in releases:
        name = _clean(release["release_name"])
        score = max((_release_score(name, hint) for hint in hints), default=0)
        if score > best_score:
            best_score = score
            best = [release]
        elif score == best_score and score > 0:
            best.append(release)
    if best_score < _MIN_RELEASE_SCORE or len(best) != 1:
        return None
    return best[0]


def _resolve_product_slug(query: str, products: list[Mapping[str, Any]]) -> str | None:
    lowered = query.lower()
    if not lowered:
        return None

    best_slug = None
    best_score = 0.0
    for product in products:
        slug = _clean(product["slug"])
        if slug in _EXCLUDED_FAMILIES:
            continue
        name = _clean(product["name"])
        slug_text = slug.replace("-", " ")
        score = 0.0
        if name and name.lower() in lowered:
            score = max(score, 95.0)
        if slug_text and (slug_text in lowered or slug in lowered):
            score = max(score, 85.0)
        if lowered in name.lower():
            score = max(score, 80.0)
        if score:
            # Prefer the most specific (longest) matching family name on ties.
            score += min(len(name), 40) / 100.0
        if score > best_score:
            best_score = score
            best_slug = slug

    return best_slug if best_score >= 60 else None


def lookup_os_microsoft_lifecycle(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
    reference_date: str | None = None,
    schema_name: str | None = None,
) -> dict[str, str]:
    today = reference_date or date.today().isoformat()
    cleaned_name, query_field = pick_api_os_value_with_field(
        os_string, normalized_os_detailed_name, normalized_os
    )

    empty = {
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
        "source": SOURCE_ID,
    }

    if not cleaned_name:
        empty["api_note"] = "No OS value available"
        return empty

    init_db(schema_name)
    with _connect(schema_name) as connection:
        release_count = connection.execute(
            "SELECT COUNT(*) AS count FROM releases"
        ).fetchone()["count"]
        if not release_count:
            empty["api_note"] = (
                "Local Microsoft Lifecycle database is empty. "
                "Run Update under Vendor Lookups first."
            )
            return empty

        products = list(connection.execute("SELECT slug, name FROM products ORDER BY name"))
        product_slug = _resolve_product_slug(cleaned_name, products)
        if not product_slug and cleaned_name != _clean(os_string):
            product_slug = _resolve_product_slug(_clean(os_string), products)
        if not product_slug:
            empty["api_note"] = "No matching Microsoft product in local database"
            return empty

        product = connection.execute(
            "SELECT slug, name FROM products WHERE slug = %s", (product_slug,)
        ).fetchone()
        product_name = _clean(product["name"]) if product else product_slug
        if _clean(os_string) and not vendors_compatible(os_string, product_name):
            empty["product_slug"] = product_slug
            empty["api_note"] = (
                f"Microsoft Lifecycle product '{product_name}' does not match OS vendor"
            )
            return empty

        releases = list(
            connection.execute(
                """
                SELECT release_name, released_date, eol_date, eoas_date,
                       latest_raw, is_supported
                FROM releases
                WHERE product_slug = %s
                ORDER BY released_date DESC, release_name DESC
                """,
                (product_slug,),
            )
        )
        selected = _pick_release(releases, extract_version_hints(cleaned_name))
        if not selected and cleaned_name != _clean(os_string):
            selected = _pick_release(releases, extract_version_hints(_clean(os_string)))
        if not selected:
            empty["product_slug"] = product_slug
            empty["api_note"] = "No matching Microsoft Lifecycle release in local database"
            return empty

        eol_iso = _clean(selected["eol_date"])
        eoas_iso = _clean(selected["eoas_date"])
        release_name = _clean(selected["release_name"])
        # Not a plain f-string join -- many release titles already start
        # with (or embed) the family name (e.g. family "Windows" + release
        # "Windows Mobile 6"/"Windows Server 2025"), which would otherwise
        # duplicate into "Windows Windows Mobile 6".
        label = join_labels(product_name, release_name)

        return {
            "eol_date": iso_date_to_epoch(eol_iso),
            "eol_status": resolve_lifecycle_status(eol_iso, None, today),
            "eoas_date": iso_date_to_epoch(eoas_iso),
            "eoas_status": resolve_lifecycle_status(eoas_iso, None, today),
            "normalized_os_detailed_name": "",
            "normalized_os": "",
            "api_note": "",
            "query_used": cleaned_name,
            "query_field": query_field,
            "product_slug": product_slug,
            "release_name": release_name,
            "release_label": label,
            "source": SOURCE_ID,
        }


def lookup_os_microsoft_lifecycle_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
    schema_name: str | None = None,
) -> list[dict[str, str]]:
    return [
        lookup_os_microsoft_lifecycle(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
            reference_date=reference_date,
            schema_name=schema_name,
        )
        for item in items
    ]


def list_all_rows(schema_name: str | None = None) -> list[dict[str, object]]:
    init_db(schema_name)
    rows: list[dict[str, object]] = []
    with _connect(schema_name) as connection:
        product_names = {
            _clean(product["slug"]): _clean(product["name"])
            for product in connection.execute("SELECT slug, name FROM products")
        }
        cursor = connection.execute(
            """
            SELECT product_slug, release_name, released_date,
                   eol_date, eoas_date, is_supported
            FROM releases
            ORDER BY is_supported DESC, product_slug ASC,
                     released_date DESC, release_name DESC
            """
        )
        for release in cursor:
            slug = _clean(release["product_slug"])
            rows.append(
                {
                    "product": product_names.get(slug, slug),
                    "release": _clean(release["release_name"]),
                    "released": _clean(release["released_date"]),
                    "eol_date": _clean(release["eol_date"]),
                    "eoas_date": _clean(release["eoas_date"]),
                    "supported": bool(release["is_supported"]),
                }
            )
    return rows
