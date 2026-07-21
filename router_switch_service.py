"""Scrape Router-Switch.com EOL/EOSL checker into a local SQLite cache.

Used by Vendor Lookups (viewer/sync) and optionally by Refresh EOL/EOAS when enabled.
Requires curl_cffi (Chrome TLS impersonation) because the site is behind Cloudflare.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from eol_service import (
    extract_version_hints,
    iso_date_to_epoch,
    pick_api_os_value_with_field,
    resolve_lifecycle_status,
)
from normalization_service import vendors_compatible
from version_match import score_release_against_hint

SOURCE_URL = "https://www.router-switch.com/eol-eosl-checker/"
BASE_URL = "https://www.router-switch.com"
REQUEST_DELAY_SECONDS = 0.2
IMPERSONATE = "chrome"

# Manufacturer path slug → display name (from the checker dropdown / list links).
MANUFACTURERS: tuple[tuple[str, str], ...] = (
    ("arista", "Arista"),
    ("aruba", "Aruba"),
    ("cisco", "Cisco"),
    ("dell", "Dell"),
    ("fortinet", "Fortinet"),
    ("h3c", "H3C"),
    ("hpe", "HPE"),
    ("juniper", "Juniper"),
    ("mellanox", "Mellanox"),
    ("palo-alto-networks", "Palo Alto Networks"),
    ("ruckus", "Ruckus"),
)


def list_manufacturers() -> list[dict[str, str]]:
    """Same manufacturer set as the site dropdown."""
    return [{"slug": slug, "label": name} for slug, name in MANUFACTURERS]


def manufacturers_from_slugs(
    slugs: list[str] | tuple[str, ...] | None,
) -> tuple[tuple[str, str], ...]:
    """Resolve request slugs to ``(slug, label)`` pairs.

    Empty / None → all manufacturers. Unknown slugs are ignored; if none remain,
    raises ``ValueError``.
    """
    if not slugs:
        return MANUFACTURERS
    wanted = {str(slug or "").strip().lower() for slug in slugs}
    wanted.discard("")
    selected = tuple(
        (slug, name) for slug, name in MANUFACTURERS if slug in wanted
    )
    if not selected:
        raise ValueError(
            "No valid manufacturers selected. Choose from: "
            + ", ".join(slug for slug, _ in MANUFACTURERS)
        )
    return selected


_PAGER_RE = re.compile(
    r"Total Products:\s*([\d,]+).*?Page\s+(\d+)\s+of\s+(\d+)",
    re.I | re.S,
)
_PAGE_LINK_RE = re.compile(r"[?&]page=(\d+)", re.I)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent / "_data" / "router_switch_os.db"


def _default_prefs_path() -> Path:
    """Shared on-disk selection (repo file under `_data/`; not browser storage)."""
    return Path(__file__).resolve().parent / "_data" / "router_switch_sync.json"


def load_selected_manufacturers(
    prefs_path: Path | None = None,
) -> list[str]:
    """Return saved manufacturer slugs (defaults to all if missing/invalid)."""
    path = prefs_path or _default_prefs_path()
    default = [slug for slug, _ in MANUFACTURERS]
    if not path.is_file():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    raw = payload.get("manufacturers") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return default
    try:
        return [slug for slug, _ in manufacturers_from_slugs([str(item) for item in raw])]
    except ValueError:
        return default


def save_selected_manufacturers(
    slugs: list[str] | tuple[str, ...] | None,
    prefs_path: Path | None = None,
) -> list[str]:
    """Persist manufacturer selection for everyone using this codebase/server."""
    selected = manufacturers_from_slugs(slugs)
    saved = [slug for slug, _ in selected]
    path = prefs_path or _default_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manufacturers": saved,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return saved


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
                category TEXT NOT NULL DEFAULT 'hardware',
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
        "source_url": SOURCE_URL,
        "category": "router-switch",
        "source_id": "router-switch",
        "source_label": "Router-Switch EOL",
        "selected_manufacturers": load_selected_manufacturers(),
        "manufacturers": list_manufacturers(),
    }


def _parse_us_date(value: str) -> str:
    text = _clean(value)
    if not text or text.lower() in {"n/a", "na", "-", "tbd", "unknown"}:
        return ""
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _is_supported(eoas_iso: str, today: str) -> bool:
    if not eoas_iso:
        return False
    return eoas_iso >= today


def _manufacturer_list_url(slug: str, page: int = 1) -> str:
    path = f"/eol-eosl-checker/{slug}/"
    if page > 1:
        path = f"{path}?page={page}"
    return urljoin(BASE_URL, path)


def _fetch_html(session: curl_requests.Session, url: str) -> str:
    response = session.get(url, impersonate=IMPERSONATE, timeout=90)
    response.raise_for_status()
    text = response.text or ""
    if "Just a moment" in text and "cf-" in text.lower():
        raise RuntimeError(f"Cloudflare challenge blocked fetch for {url}")
    return text


def _parse_page_count(html: str) -> tuple[int, int, int]:
    """Return (total_products, current_page, total_pages)."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = _PAGER_RE.search(text)
    if match:
        total = int(match.group(1).replace(",", ""))
        current = int(match.group(2))
        pages = int(match.group(3))
        return total, current, pages

    page_nums = [int(n) for n in _PAGE_LINK_RE.findall(html)]
    if page_nums:
        return 0, 1, max(page_nums)
    return 0, 1, 1


def _parse_table_rows(html: str, manufacturer_slug: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []

    header_cells = [
        re.sub(r"\s+", " ", _clean(cell.get_text(" ", strip=True))).lower()
        for cell in table.find_all("th")
    ]
    if not header_cells:
        first = table.find("tr")
        if first:
            header_cells = [
                re.sub(r"\s+", " ", _clean(cell.get_text(" ", strip=True))).lower()
                for cell in first.find_all("td")
            ]

    col: dict[str, int] = {}
    for index, label in enumerate(header_cells):
        if "part" in label and "number" in label:
            col["part"] = index
        elif "product" in label and "name" in label:
            col["name"] = index
        elif "eol" in label and "announce" in label:
            col["eol"] = index
        elif "end of sale" in label or label == "eos":
            col["eos"] = index
        elif "end of service" in label or "eosl" in label:
            col["eosl"] = index

    if "part" not in col or "eol" not in col:
        return []

    rows: list[dict[str, str]] = []
    body_rows = table.find_all("tr")
    for tr in body_rows:
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        part = _clean(cells[col["part"]].get_text(" ", strip=True))
        if not part:
            continue
        name = (
            _clean(cells[col["name"]].get_text(" ", strip=True))
            if "name" in col and col["name"] < len(cells)
            else ""
        )
        eol = (
            _parse_us_date(cells[col["eol"]].get_text(" ", strip=True))
            if "eol" in col and col["eol"] < len(cells)
            else ""
        )
        eos = (
            _parse_us_date(cells[col["eos"]].get_text(" ", strip=True))
            if "eos" in col and col["eos"] < len(cells)
            else ""
        )
        eosl = (
            _parse_us_date(cells[col["eosl"]].get_text(" ", strip=True))
            if "eosl" in col and col["eosl"] < len(cells)
            else ""
        )
        link = cells[col["part"]].find("a")
        detail_url = ""
        if link and link.get("href"):
            detail_url = urljoin(BASE_URL, str(link.get("href")))
        rows.append(
            {
                "product_slug": manufacturer_slug,
                "release_name": part,
                "released_date": eos,
                "eol_date": eol,
                "eoas_date": eosl,
                "latest_raw": name or detail_url,
            }
        )
    return rows


def sync_router_switch_database(
    db_path: Path | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    manufacturers: tuple[tuple[str, str], ...] | None = None,
    max_pages_per_manufacturer: int | None = None,
) -> dict[str, object]:
    """Scrape manufacturer EOL list pages into SQLite.

    ``max_pages_per_manufacturer`` is for tests / smoke runs; leave ``None`` for full sync.
    """
    init_db(db_path)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    finished = started
    today = date.today().isoformat()
    vendors = manufacturers or manufacturers_from_slugs(load_selected_manufacturers())
    # Persist the selection used for this sync so all clients share it.
    save_selected_manufacturers([slug for slug, _ in vendors])
    selected_slugs = [slug for slug, _ in vendors]
    errors: list[dict[str, str]] = []
    release_total = 0
    page_total_estimate = len(vendors)  # refined as we learn page counts
    pages_done = 0

    session = curl_requests.Session()
    product_count = 0

    try:
        with _connect(db_path) as connection:
            # Replace only the selected manufacturers; leave others intact.
            placeholders = ",".join("?" * len(selected_slugs))
            connection.execute(
                f"DELETE FROM releases WHERE product_slug IN ({placeholders})",
                selected_slugs,
            )
            connection.execute(
                f"DELETE FROM products WHERE slug IN ({placeholders})",
                selected_slugs,
            )

            for vendor_index, (slug, name) in enumerate(vendors, start=1):
                list_url = _manufacturer_list_url(slug, 1)
                stage = f"{name} ({vendor_index}/{len(vendors)})"
                if progress_callback:
                    progress_callback(stage, pages_done, max(page_total_estimate, 1))

                try:
                    html = _fetch_html(session, list_url)
                    time.sleep(REQUEST_DELAY_SECONDS)
                except (curl_requests.RequestsError, OSError, RuntimeError) as exc:
                    errors.append({"slug": slug, "error": str(exc)})
                    continue

                _total_products, _current, total_pages = _parse_page_count(html)
                if max_pages_per_manufacturer is not None:
                    total_pages = min(total_pages, max_pages_per_manufacturer)
                remaining_vendors = len(vendors) - vendor_index
                page_total_estimate = pages_done + total_pages + remaining_vendors

                scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO products(slug, name, category, url, scraped_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (slug, name, "hardware", list_url, scraped_at),
                )

                for page in range(1, total_pages + 1):
                    if page > 1:
                        page_url = _manufacturer_list_url(slug, page)
                        try:
                            html = _fetch_html(session, page_url)
                            time.sleep(REQUEST_DELAY_SECONDS)
                        except (curl_requests.RequestsError, OSError, RuntimeError) as exc:
                            errors.append(
                                {"slug": f"{slug}?page={page}", "error": str(exc)}
                            )
                            break

                    rows = _parse_table_rows(html, slug)
                    for row in rows:
                        connection.execute(
                            """
                            INSERT INTO releases(
                                product_slug, release_name, released_date,
                                eol_date, eoas_date, latest_raw, is_supported
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(product_slug, release_name) DO UPDATE SET
                                released_date = excluded.released_date,
                                eol_date = excluded.eol_date,
                                eoas_date = excluded.eoas_date,
                                latest_raw = excluded.latest_raw,
                                is_supported = excluded.is_supported
                            """,
                            (
                                row["product_slug"],
                                row["release_name"],
                                row["released_date"],
                                row["eol_date"],
                                row["eoas_date"],
                                row["latest_raw"],
                                int(_is_supported(row["eoas_date"], today)),
                            ),
                        )
                        release_total += 1

                    pages_done += 1
                    if progress_callback:
                        progress_callback(
                            f"{name} page {page}/{total_pages}",
                            pages_done,
                            max(page_total_estimate, pages_done),
                        )

                connection.commit()

            finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            product_count = int(
                connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            )
            _set_metadata(connection, "last_updated", finished)
            _set_metadata(connection, "last_sync_started", started)
            _set_metadata(
                connection,
                "last_sync_status",
                "error" if errors and release_total == 0 else "ok",
            )
            message = (
                f"Synced {', '.join(name for _, name in vendors)}: "
                f"{release_total} products ({pages_done} pages). "
                f"DB now has {product_count} manufacturers."
            )
            if errors:
                message += f" {len(errors)} page error(s)."
            _set_metadata(connection, "last_sync_message", message)
            connection.commit()
    finally:
        session.close()

    return {
        "ok": release_total > 0,
        "product_count": product_count,
        "release_count": release_total,
        "pages_scraped": pages_done,
        "manufacturers": [slug for slug, _ in vendors],
        "manufacturer_labels": [name for _, name in vendors],
        "started": started,
        "finished": finished,
        "source_url": SOURCE_URL,
        "errors": errors[:20],
    }


def list_all_rows(db_path: Path | None = None) -> list[dict[str, object]]:
    """Rows for the Vendor Lookups viewer.

    product → Product Name, release → Part Number,
    released → End of Sale (EOS), eol_date → EOL Announcement,
    eoas_date → End of Service Life (EOSL).
    """
    init_db(db_path)
    rows: list[dict[str, object]] = []
    with _connect(db_path) as connection:
        manufacturer_names = {
            _clean(product["slug"]): _clean(product["name"])
            for product in connection.execute("SELECT slug, name FROM products")
        }
        cursor = connection.execute(
            """
            SELECT product_slug, release_name, released_date,
                   eol_date, eoas_date, latest_raw, is_supported
            FROM releases
            ORDER BY is_supported DESC, product_slug ASC,
                     eol_date DESC, release_name ASC
            """
        )
        for release in cursor:
            slug = _clean(release["product_slug"])
            product_name = _clean(release["latest_raw"])
            if not product_name:
                product_name = manufacturer_names.get(slug, slug)
            elif manufacturer_names.get(slug) and not product_name.lower().startswith(
                manufacturer_names[slug].lower()
            ):
                product_name = f"{manufacturer_names[slug]} — {product_name}"
            rows.append(
                {
                    "product": product_name,
                    "release": _clean(release["release_name"]),
                    "released": _clean(release["released_date"]),
                    "eol_date": _clean(release["eol_date"]),
                    "eoas_date": _clean(release["eoas_date"]),
                    "supported": bool(release["is_supported"]),
                }
            )
    return rows


_MIN_RELEASE_SCORE = 80
_VERSION_IN_TEXT_RE = re.compile(r"\d+(?:\.\d+)+(?:[a-zA-Z]+\d*)?")


def _field_version_tokens(value: str) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    tokens = extract_version_hints(text)
    if tokens:
        return tokens
    return [match.group(0) for match in _VERSION_IN_TEXT_RE.finditer(text)]


def _score_router_switch_row(
    part_number: str,
    product_name: str,
    query: str,
    hints: list[str],
) -> int:
    best = 0
    query_key = re.sub(r"\s+", " ", query.lower()).strip()
    for field in (part_number, product_name):
        field_key = re.sub(r"\s+", " ", field.lower()).strip()
        if not field_key:
            continue
        if field_key == query_key:
            return 100
        if query_key and (query_key in field_key or field_key in query_key):
            # Require a shared multi-segment version when both sides expose one.
            query_versions = _field_version_tokens(query)
            field_versions = _field_version_tokens(field)
            if query_versions and field_versions:
                version_hit = max(
                    (
                        score_release_against_hint(field_ver, query_ver)
                        for field_ver in field_versions
                        for query_ver in query_versions
                    ),
                    default=0,
                )
                if version_hit >= _MIN_RELEASE_SCORE:
                    best = max(best, 92)
            elif not query_versions:
                best = max(best, 85)
        for hint in hints:
            for token in _field_version_tokens(field):
                best = max(best, score_release_against_hint(token, hint))
            # Exact part-number style hint.
            if field_key == hint.lower():
                best = max(best, 100)
    return best


def lookup_os_router_switch(
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
        "source": "router-switch",
    }

    init_db(db_path)
    with _connect(db_path) as connection:
        release_count = connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        if not release_count:
            empty["api_note"] = (
                "Local Router-Switch database is empty. "
                "Run Update under Vendor Lookups first."
            )
            return empty

        manufacturer_names = {
            _clean(row["slug"]): _clean(row["name"])
            for row in connection.execute("SELECT slug, name FROM products")
        }
        hints = extract_version_hints(cleaned_name)
        if cleaned_name != _clean(os_string):
            for hint in extract_version_hints(_clean(os_string)):
                if hint not in hints:
                    hints.append(hint)

        best_row = None
        best_score = 0
        cursor = connection.execute(
            """
            SELECT product_slug, release_name, released_date,
                   eol_date, eoas_date, latest_raw
            FROM releases
            """
        )
        for row in cursor:
            part = _clean(row["release_name"])
            product_name = _clean(row["latest_raw"]) or part
            manufacturer = manufacturer_names.get(_clean(row["product_slug"]), "")
            if manufacturer and _clean(os_string):
                if not vendors_compatible(os_string, f"{manufacturer} {product_name}"):
                    continue
            score = _score_router_switch_row(part, product_name, cleaned_name, hints)
            if cleaned_name != _clean(os_string):
                score = max(
                    score,
                    _score_router_switch_row(part, product_name, _clean(os_string), hints),
                )
            if score > best_score:
                best_score = score
                best_row = row

        if best_row is None or best_score < _MIN_RELEASE_SCORE:
            empty["api_note"] = "No matching Router-Switch product/release"
            return empty

        eol_iso = _clean(best_row["eol_date"])
        eoas_iso = _clean(best_row["eoas_date"])
        part = _clean(best_row["release_name"])
        product_name = _clean(best_row["latest_raw"]) or part
        product_slug = _clean(best_row["product_slug"])
        label = product_name if product_name == part else f"{product_name} ({part})"

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
            "release_name": part,
            "release_label": label,
            "source": "router-switch",
        }


def lookup_os_router_switch_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, str]]:
    return [
        lookup_os_router_switch(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
            reference_date=reference_date,
            db_path=db_path,
        )
        for item in items
    ]
