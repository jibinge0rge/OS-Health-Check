"""Scrape Layer23-Switch EOL/EOSL pages into a local SQLite cache.

Used by Vendor Lookups (viewer/sync) and optionally by Refresh EOL/EOAS.
Uses curl_cffi (Chrome TLS impersonation) because the site is behind Cloudflare.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
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
from router_switch_service import (
    IMPERSONATE,
    MANUFACTURERS,
    MAX_SYNC_WORKERS,
    REQUEST_DELAY_SECONDS,
    _MIN_RELEASE_SCORE,
    _clean,
    _parse_page_count,
    _score_router_switch_row,
)

SOURCE_URL = "https://www.layer23-switch.com/eol-eosl-tool/"
BASE_URL = "https://www.layer23-switch.com"


def list_manufacturers() -> list[dict[str, str]]:
    return [{"slug": slug, "label": name} for slug, name in MANUFACTURERS]


def manufacturers_from_slugs(
    slugs: list[str] | tuple[str, ...] | None,
) -> tuple[tuple[str, str], ...]:
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


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent / "_data" / "layer23_switch_os.db"


def _default_prefs_path() -> Path:
    return Path(__file__).resolve().parent / "_data" / "layer23_switch_sync.json"


def load_selected_manufacturers(
    prefs_path: Path | None = None,
) -> list[str]:
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


@contextmanager
def _connect(db_path: Path | None = None):
    path = db_path or _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


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
        "category": "layer23-switch",
        "source_id": "layer23-switch",
        "source_label": "Layer23-Switch EOL",
        "selected_manufacturers": load_selected_manufacturers(),
        "manufacturers": list_manufacturers(),
    }


def _parse_iso_date(value: str) -> str:
    text = _clean(value)
    if not text or text.lower() in {"not announced", "n/a", "na", "-", "tbd", "unknown"}:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
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
    path = f"/eol-eosl-tool/{slug}/"
    if page > 1:
        path = f"{path}?page={page}"
    return urljoin(BASE_URL, path)


def _fetch_html(session: curl_requests.Session, url: str) -> str:
    response = session.get(url, impersonate=IMPERSONATE, timeout=90)
    response.raise_for_status()
    text = response.text or ""
    if "Just a moment" in text and "cf-" in text.lower():
        raise RuntimeError(f"Cloudflare challenge blocked fetch for {url}")
    if "Sorry, you have been blocked" in text and "Cloudflare" in text[:4000]:
        raise RuntimeError(f"Cloudflare blocked fetch for {url}")
    return text


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
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        part_cell = cells[col["part"]]
        part = _clean(part_cell.get_text(" ", strip=True))
        if not part:
            continue
        name = (
            _clean(cells[col["name"]].get_text(" ", strip=True))
            if "name" in col and col["name"] < len(cells)
            else ""
        )
        eol = (
            _parse_iso_date(cells[col["eol"]].get_text(" ", strip=True))
            if col["eol"] < len(cells)
            else ""
        )
        eos = (
            _parse_iso_date(cells[col["eos"]].get_text(" ", strip=True))
            if "eos" in col and col["eos"] < len(cells)
            else ""
        )
        eosl = (
            _parse_iso_date(cells[col["eosl"]].get_text(" ", strip=True))
            if "eosl" in col and col["eosl"] < len(cells)
            else ""
        )
        link = part_cell.find("a")
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


def _fetch_page_worker(slug: str, page: int) -> tuple[int, list[dict[str, str]], str]:
    """Runs in a worker thread: fetch + parse one listing page.

    Each worker opens its own session (curl_cffi sessions are not shared safely
    across threads) and paces itself so N workers together still look like a
    handful of polite, roughly-serial clients rather than a burst.
    """
    session = curl_requests.Session()
    try:
        html = _fetch_html(session, _manufacturer_list_url(slug, page))
        time.sleep(REQUEST_DELAY_SECONDS)
        return page, _parse_table_rows(html, slug), ""
    except (curl_requests.RequestsError, OSError, RuntimeError) as exc:
        return page, [], str(exc)
    finally:
        session.close()


def sync_layer23_switch_database(
    db_path: Path | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    manufacturers: tuple[tuple[str, str], ...] | None = None,
    max_pages_per_manufacturer: int | None = None,
    cancel_event: threading.Event | None = None,
    max_workers: int = MAX_SYNC_WORKERS,
) -> dict[str, object]:
    """Scrape manufacturer EOL list pages into SQLite.

    Remaining pages within a manufacturer (after page 1) are fetched concurrently
    (bounded by ``max_workers``) since large catalogs like Cisco can span
    thousands of pages.
    """
    init_db(db_path)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    finished = started
    today = date.today().isoformat()
    vendors = manufacturers or manufacturers_from_slugs(load_selected_manufacturers())
    save_selected_manufacturers([slug for slug, _ in vendors])
    selected_slugs = [slug for slug, _ in vendors]
    errors: list[dict[str, str]] = []
    release_total = 0
    page_total_estimate = len(vendors)
    pages_done = 0
    cancelled = False

    session = curl_requests.Session()
    product_count = 0

    def is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    try:
        with _connect(db_path) as connection:
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
                if is_cancelled():
                    cancelled = True
                    break

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

                def _store_page(page: int, rows: list[dict[str, str]], error: str) -> None:
                    nonlocal release_total
                    if error:
                        errors.append({"slug": f"{slug}?page={page}", "error": error})
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

                # Page 1 was already fetched above (needed to learn total_pages).
                _store_page(1, _parse_table_rows(html, slug), "")
                pages_done += 1
                if progress_callback:
                    progress_callback(
                        f"{name} page 1/{total_pages}",
                        pages_done,
                        max(page_total_estimate, pages_done),
                    )

                if total_pages > 1 and not is_cancelled():
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        future_to_page = {
                            executor.submit(_fetch_page_worker, slug, page): page
                            for page in range(2, total_pages + 1)
                        }
                        cancel_requested = False
                        for future in as_completed(future_to_page):
                            if not cancel_requested and is_cancelled():
                                cancel_requested = True
                                for pending in future_to_page:
                                    pending.cancel()
                            if future.cancelled():
                                continue
                            page, rows, error = future.result()
                            _store_page(page, rows, error)
                            pages_done += 1
                            if progress_callback:
                                progress_callback(
                                    f"{name} page {page}/{total_pages}",
                                    pages_done,
                                    max(page_total_estimate, pages_done),
                                )
                    if is_cancelled():
                        cancelled = True

                connection.commit()
                if cancelled:
                    break

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
                f"{'Cancelled after syncing' if cancelled else 'Synced'} "
                f"{', '.join(name for _, name in vendors)}: "
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
        "cancelled": cancelled,
    }


def list_all_rows(db_path: Path | None = None) -> list[dict[str, object]]:
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


def lookup_os_layer23_switch(
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
        "source": "layer23-switch",
    }

    init_db(db_path)
    with _connect(db_path) as connection:
        release_count = connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        if not release_count:
            empty["api_note"] = (
                "Local Layer23-Switch database is empty. "
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
            empty["api_note"] = "No matching Layer23-Switch product/release"
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
            "source": "layer23-switch",
        }


def lookup_os_layer23_switch_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, str]]:
    return [
        lookup_os_layer23_switch(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
            reference_date=reference_date,
            db_path=db_path,
        )
        for item in items
    ]
