"""Scrape OS lifecycle data from eosl.date into a local SQLite cache."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from eol_service import (
    extract_version_hints,
    iso_date_to_epoch,
    pick_api_os_value_with_field,
    resolve_lifecycle_status,
)
from normalization_service import vendors_compatible


BASE_URL = "https://eosl.date"
EOL_INDEX_URL = f"{BASE_URL}/eol/"
HEADERS = {
    "User-Agent": "OS-Health-Check/1.0 (+local eosl scraper; internal tool)"
}
REQUEST_DELAY_SECONDS = 0.45
OS_CATEGORY = "os"

# Columns that are never a support-lifecycle date column.
NON_SUPPORT_LABELS = (
    "release",
    "released",
    "latest",
    "lts",
    "codename",
    "version",
)
# "Still supported / no fixed end date" markers seen in support columns.
ACTIVE_MARKERS = ("active", "supported", "yes", "tbd", "n/a", "-", "")

# Ultra-generic product pages that must not absorb vague "Other … Linux" strings.
_GENERIC_FAMILY_SLUGS = frozenset({"linux", "windows", "unix"})

_VAGUE_OS_RE = re.compile(
    r"\b(?:other|unknown|various|any|unspecified)\b|\bor later\b|\bor earlier\b",
    re.I,
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent / "_data" / "eosl_os.db"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(db_path: Path | None = None) -> None:
    with _connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'os',
                url TEXT NOT NULL,
                scraped_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_slug TEXT NOT NULL,
                release_name TEXT NOT NULL,
                released_date TEXT NOT NULL DEFAULT '',
                eol_date TEXT NOT NULL DEFAULT '',
                eoas_date TEXT NOT NULL DEFAULT '',
                latest_raw TEXT NOT NULL DEFAULT '',
                is_supported INTEGER NOT NULL DEFAULT 0,
                UNIQUE(product_slug, release_name),
                FOREIGN KEY (product_slug) REFERENCES products(slug)
            );

            CREATE INDEX IF NOT EXISTS idx_releases_product
                ON releases(product_slug);
            """
        )
        connection.commit()


def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_status(db_path: Path | None = None) -> dict[str, object]:
    init_db(db_path)
    with _connect(db_path) as connection:
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        product_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        release_count = connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    return {
        "last_updated": meta.get("last_updated", ""),
        "last_sync_status": meta.get("last_sync_status", ""),
        "last_sync_message": meta.get("last_sync_message", ""),
        "product_count": int(product_count),
        "release_count": int(release_count),
        "source_url": EOL_INDEX_URL,
        "category": OS_CATEGORY,
    }


def _fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def _parse_labeled_date(cell) -> str:
    if cell is None:
        return ""
    time_tag = cell.find("time")
    if time_tag and time_tag.get("datetime"):
        return _clean(time_tag["datetime"])
    text = cell.get_text(" ", strip=True)
    match = re.search(
        r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2},\s+\d{4}",
        text,
    )
    if not match:
        return ""
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(match.group(0), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _is_support_column(label: str) -> bool:
    lowered = label.strip().lower()
    if not lowered:
        return False
    return not any(skip == lowered for skip in NON_SUPPORT_LABELS)


def _parse_product_page(slug: str, html: str) -> tuple[str, list[dict[str, str]]]:
    """Parse a product page.

    Column names differ per vendor (e.g. "Security Support" / "Extended
    Support" / "Basic/Premier Support"). Rather than matching every label, we
    treat any non-metadata column that holds a date as a support-lifecycle
    column and derive EOAS = earliest end date, EOL = latest end date.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    product_name = _clean(heading.get_text(" ", strip=True) if heading else slug)
    product_name = re.sub(
        r"\s+End of Life \(EOL\) Dates and End of Support \(EOS\) Dates\s*$",
        "",
        product_name,
        flags=re.IGNORECASE,
    )

    releases: dict[str, dict[str, str]] = {}
    for table in soup.find_all("table"):
        headers = [_clean(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        if not headers or headers[0].lower() != "release":
            continue

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue

            release_name = ""
            released_date = ""
            latest_raw = ""
            support_dates: list[str] = []

            for index, cell in enumerate(cells):
                label = _clean(cell.get("data-label"))
                if not label and index < len(headers):
                    label = headers[index]

                lowered = label.lower()
                if lowered == "release":
                    release_name = cell.get_text(" ", strip=True)
                elif lowered == "released":
                    released_date = _parse_labeled_date(cell)
                elif lowered == "latest":
                    latest_raw = cell.get_text(" ", strip=True)
                elif _is_support_column(label):
                    parsed = _parse_labeled_date(cell)
                    if parsed:
                        support_dates.append(parsed)

            release_name = _clean(release_name)
            if not release_name:
                continue

            support_dates = sorted(set(support_dates))
            if len(support_dates) >= 2:
                eoas_date = support_dates[0]
                eol_date = support_dates[-1]
            elif len(support_dates) == 1:
                eoas_date = ""
                eol_date = support_dates[0]
            else:
                eoas_date = ""
                eol_date = ""

            row_classes = " ".join(row.get("class", []))
            is_supported = 1 if "release-row-supported" in row_classes else 0

            existing = releases.get(release_name, {})
            releases[release_name] = {
                "release_name": release_name,
                "released_date": released_date or existing.get("released_date", ""),
                "eol_date": eol_date or existing.get("eol_date", ""),
                "eoas_date": eoas_date or existing.get("eoas_date", ""),
                "latest_raw": latest_raw or existing.get("latest_raw", ""),
                "is_supported": str(
                    max(is_supported, int(existing.get("is_supported", "0") or 0))
                ),
            }

    return product_name, list(releases.values())


def list_os_product_slugs(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    products: list[tuple[str, str]] = []
    for anchor in soup.select('a[href*="/eol/product/"]'):
        href = _clean(anchor.get("href"))
        if not href.startswith("/eol/product/"):
            continue
        slug = href.strip("/").split("/")[-1]
        if not slug or slug in seen:
            continue
        seen.add(slug)
        products.append((slug, anchor.get_text(" ", strip=True)))
    return products


def collect_os_products() -> list[tuple[str, str]]:
    products: list[tuple[str, str]] = []
    seen: set[str] = set()
    page = 1
    while True:
        params = (
            f"?category={OS_CATEGORY}"
            if page == 1
            else f"?category={OS_CATEGORY}&page={page}"
        )
        html = _fetch_html(f"{EOL_INDEX_URL}{params}")
        batch = list_os_product_slugs(html)
        if not batch:
            break
        added = 0
        for slug, name in batch:
            if slug in seen:
                continue
            seen.add(slug)
            products.append((slug, name))
            added += 1
        soup = BeautifulSoup(html, "html.parser")
        has_next = any(
            f"page={page + 1}" in _clean(link.get("href"))
            for link in soup.select('a[href*="page="]')
        )
        if not has_next or added == 0:
            break
        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)
    return products


def sync_os_database(
    db_path: Path | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, object]:
    init_db(db_path)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errors: list[dict[str, str]] = []
    products = collect_os_products()
    total = len(products)
    release_total = 0

    with _connect(db_path) as connection:
        connection.execute("DELETE FROM releases")
        connection.execute("DELETE FROM products")

        for index, (slug, list_name) in enumerate(products, start=1):
            if progress_callback:
                progress_callback(slug, index, total)
            product_url = urljoin(BASE_URL, f"/eol/product/{slug}/")
            try:
                html = _fetch_html(product_url)
                product_name, releases = _parse_product_page(slug, html)
                if not product_name:
                    product_name = list_name
                scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO products(slug, name, category, url, scraped_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (slug, product_name, OS_CATEGORY, product_url, scraped_at),
                )
                for release in releases:
                    connection.execute(
                        """
                        INSERT INTO releases(
                            product_slug, release_name, released_date,
                            eol_date, eoas_date, latest_raw, is_supported
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            slug,
                            release["release_name"],
                            release.get("released_date", ""),
                            release.get("eol_date", ""),
                            release.get("eoas_date", ""),
                            release.get("latest_raw", ""),
                            int(release.get("is_supported", "0") or 0),
                        ),
                    )
                    release_total += 1
            except (requests.RequestException, ValueError, sqlite3.Error) as exc:
                errors.append({"slug": slug, "error": str(exc)})
            time.sleep(REQUEST_DELAY_SECONDS)

        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _set_metadata(connection, "last_updated", finished)
        _set_metadata(connection, "last_sync_started", started)
        _set_metadata(connection, "last_sync_status", "error" if errors else "ok")
        _set_metadata(
            connection,
            "last_sync_message",
            f"Synced {total - len(errors)} of {total} OS products "
            f"({release_total} releases).",
        )
        connection.commit()

    return {
        "last_updated": finished,
        "product_count": total - len(errors),
        "release_count": release_total,
        "errors": errors,
        "status": "error" if errors else "ok",
    }


def _load_products(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT slug, name FROM products ORDER BY name"))


def _load_releases(connection: sqlite3.Connection, product_slug: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT release_name, released_date, eol_date, eoas_date, latest_raw, is_supported
            FROM releases
            WHERE product_slug = ?
            ORDER BY released_date DESC, release_name DESC
            """,
            (product_slug,),
        )
    )


def _version_tokens(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)*", text or "")


def _eosl_version_hints(os_name: str) -> list[str]:
    """Version hints suitable for EOSL product+release matching.

    Uses shared ``extract_version_hints`` (drops bitness / SP pack digits / ``N.x``
    ranges). Product alone is never enough — see ``_pick_release``.
    """
    # extract_version_hints already applies the conservative filters; keep this
    # wrapper so call sites stay explicit about EOSL hint policy.
    return extract_version_hints(os_name)


def _version_match_score(release_version: str, hint: str) -> int:
    if not release_version or not hint:
        return 0
    if release_version == hint:
        return 100
    rel_parts = release_version.split(".")
    hint_parts = hint.split(".")
    shorter = min(len(rel_parts), len(hint_parts))
    if rel_parts[:shorter] == hint_parts[:shorter]:
        # Dot-aware prefix match (e.g. release "22.04" vs hint "22.04.3").
        # Require the hint to carry at least as many segments as it claims —
        # a bare major like "3" must not prefix-match "3.18".
        if len(hint_parts) == 1 and len(rel_parts) > 1:
            return 0
        return 90
    if len(hint_parts) > 1 and rel_parts[0] == hint_parts[0]:
        # Shared major only when both sides are multi-part (e.g. 8.6 vs 8.4).
        return 55
    return 0


def _release_score(release_name: str, hint: str) -> int:
    # Only score against version tokens in the release name itself. The
    # "latest" column embeds release dates (e.g. 2026-07-11) whose digits
    # would otherwise create false matches. Never use raw substring checks
    # like "3" in "6.13" — that caused Other-Linux → linux 6.13.
    best = 0
    for candidate in _version_tokens(release_name):
        best = max(best, _version_match_score(candidate, hint))
    return best


# Accept only strong version matches (exact or multi-segment prefix).
_MIN_RELEASE_SCORE = 80


def _pick_release(releases: list[sqlite3.Row], hints: list[str]) -> sqlite3.Row | None:
    """Match product releases by version hint. Product alone is not enough —
    a strong release score is required so we never guess.
    """
    if not releases or not hints:
        return None

    best = None
    best_score = 0
    for release in releases:
        name = _clean(release["release_name"])
        score = max((_release_score(name, hint) for hint in hints), default=0)
        if score > best_score:
            best_score = score
            best = release
    return best if best_score >= _MIN_RELEASE_SCORE else None


def _query_is_vague(query: str) -> bool:
    return bool(_VAGUE_OS_RE.search(query or ""))


def _query_targets_generic_family(query: str, slug: str) -> bool:
    """True when a generic family page (e.g. linux kernel) is an intentional hit."""
    lowered = (query or "").lower()
    if slug == "linux":
        return bool(
            re.search(r"\blinux\s+\d", lowered)
            or re.search(r"\bkernel\s+\d", lowered)
            or re.search(r"\d+(?:\.\d+)*\s+linux\b", lowered)
        )
    if slug == "windows":
        return bool(re.search(r"\bwindows\s+(?:\d|server|vista|xp)\b", lowered))
    if slug == "unix":
        return bool(re.search(r"\bunix\s+\d", lowered))
    return True


def _resolve_product_slug(query: str, products: list[sqlite3.Row]) -> str | None:
    lowered = query.lower()
    if not lowered:
        return None

    best_slug = None
    best_score = 0.0
    for product in products:
        slug = _clean(product["slug"])
        name = _clean(product["name"])
        slug_text = slug.replace("-", " ")
        score = 0.0
        if name and name.lower() in lowered:
            score = max(score, 95.0)
        if slug_text in lowered or slug in lowered:
            score = max(score, 85.0)
        if lowered in name.lower():
            score = max(score, 80.0)
        if slug.replace("-", "") in lowered.replace(" ", "").replace("-", ""):
            score = max(score, 70.0)
        # Prefer the most specific (longest) matching product name on ties,
        # so "Windows Server" wins over "Windows".
        if score:
            score += min(len(name), 40) / 100.0
        if score > best_score:
            best_score = score
            best_slug = slug

    if best_score < 60 or not best_slug:
        return None

    # Vague "Other … Linux" must not resolve to the generic linux kernel product.
    if best_slug in _GENERIC_FAMILY_SLUGS:
        if _query_is_vague(query) or not _query_targets_generic_family(query, best_slug):
            return None

    return best_slug


def lookup_os_eosl(
    os_string: str,
    normalized_os_detailed_name: str,
    normalized_os: str,
    reference_date: str | None = None,
    db_path: Path | None = None,
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
        "source": "eosl",
    }

    init_db(db_path)
    with _connect(db_path) as connection:
        product_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if not product_count:
            empty["api_note"] = "Local EOSL database is empty. Run Update EOSL Lookup first."
            return empty

        products = _load_products(connection)
        product_slug = _resolve_product_slug(cleaned_name, products)
        if not product_slug and cleaned_name != _clean(os_string):
            product_slug = _resolve_product_slug(_clean(os_string), products)

        if not product_slug:
            empty["api_note"] = "No matching OS product in local EOSL database"
            return empty

        product = connection.execute(
            "SELECT slug, name FROM products WHERE slug = ?", (product_slug,)
        ).fetchone()
        if product and _clean(os_string):
            if not vendors_compatible(os_string, _clean(product["name"])):
                empty["product_slug"] = product_slug
                empty["api_note"] = (
                    f"EOSL product '{product['name']}' does not match OS vendor"
                )
                return empty

        releases = _load_releases(connection, product_slug)
        selected = _pick_release(releases, _eosl_version_hints(cleaned_name))
        if not selected:
            empty["product_slug"] = product_slug
            empty["api_note"] = "No matching release in local EOSL database"
            return empty

        eol_iso = _clean(selected["eol_date"])
        eoas_iso = _clean(selected["eoas_date"])
        release_name = _clean(selected["release_name"])

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
            "release_label": release_name,
            "source": "eosl",
        }


def lookup_os_eosl_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, str]]:
    return [
        lookup_os_eosl(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
            reference_date=reference_date,
            db_path=db_path,
        )
        for item in items
    ]


def list_all_rows(db_path: Path | None = None) -> list[dict[str, object]]:
    """Return every scraped release with the database's own column shape.

    Used to render the read-only EOSL Lookup viewer in the UI. Dates are the
    raw ISO values scraped from eosl.date (empty string when unknown).
    """
    init_db(db_path)
    rows: list[dict[str, object]] = []
    with _connect(db_path) as connection:
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
