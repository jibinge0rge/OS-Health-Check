"""Scrape SUSE product lifecycle data from suse.com/lifecycle into SQLite."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import requests
from bs4 import BeautifulSoup

from eol_service import (
    extract_version_hints,
    iso_date_to_epoch,
    pick_api_os_value_with_field,
    resolve_lifecycle_status,
)
from normalization_service import vendors_compatible
from version_match import score_release_against_hint


SOURCE_URL = "https://www.suse.com/lifecycle/"
HEADERS = {
    "User-Agent": (
        "OS-Health-Check/1.0 (+local suse scraper; internal tool)"
    )
}

# Whole-token match for SUSE / SLES / openSUSE.
_SUSE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:suse|sles|opensuse)(?![A-Za-z0-9])",
    re.I,
)
_SP_IN_TEXT_RE = re.compile(r"(?<!\d)(\d+)\s*SP\s*(\d+)\b", re.I)
_PRODUCT_RELEASE_RE = re.compile(
    r"^(?P<product>.+?)\s+(?P<major>\d+)(?:\s*SP\s*(?P<sp>\d+))?$",
    re.I,
)

_MIN_RELEASE_SCORE = 80
_NON_VERSION_HINTS = frozenset({"16", "32", "64", "86", "128", "256"})
_NA_MARKERS = frozenset(
    {
        "",
        "-",
        "—",
        "n/a",
        "na",
        "not applicable",
        "none",
        "tbd",
    }
)

def _clean(value: object) -> str:
    return str(value or "").replace("\xa0", " ").strip()


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent / "_data" / "suse_os.db"


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
        "source_url": SOURCE_URL,
        "category": "suse",
        "source_id": "suse",
        "source_label": "SUSE Lifecycle",
    }


def query_matches_suse(*values: object) -> bool:
    for value in values:
        if _SUSE_TOKEN_RE.search(_clean(value)):
            return True
    return False


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "suse"


def _normalize_sp_release(major: str, sp: str | None = None) -> str:
    major = _clean(major)
    if not major:
        return ""
    if sp:
        sp_num = re.sub(r"\D", "", _clean(sp))
        if sp_num:
            return f"{major} SP{int(sp_num)}"
    return major


def _parse_suse_date(value: str) -> str:
    text = re.sub(r"\s+", " ", _clean(value))
    if text.lower() in _NA_MARKERS:
        return ""
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    for fmt in ("%b %Y", "%B %Y"):
        try:
            return datetime.strptime(text, fmt).date().replace(day=1).isoformat()
        except ValueError:
            continue
    return ""


_MONTH_NAME_RE = re.compile(
    r"^(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)$",
    re.I,
)
_DATE_FRAGMENT_RE = re.compile(
    r"^\d{1,2}\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)$",
    re.I,
)


def _header_map(cells: list[str]) -> dict[str, int]:
    """Map columns for SUSE lifecycle tables.

    Accepts both Service Pack tables (with FCS) and the overview
    ``Product Release`` table. Nested Version/GA date rows are rejected later
    by ``_looks_like_product_release``.
    """
    mapping: dict[str, int] = {}
    for index, raw in enumerate(cells):
        label = re.sub(r"\s+", " ", _clean(raw)).lower()
        label = label.replace("*", "").strip()
        if not label:
            continue
        if "service pack" in label or label in {
            "product release",
            "clients",
            "admin server, branch server",
        }:
            mapping.setdefault("release", index)
        elif label in {"fcs date", "released"}:
            mapping.setdefault("released", index)
        elif "general" in label and "end" in label:
            mapping.setdefault("eol", index)
        elif label.startswith("ltss") and "end" in label and "core" not in label:
            mapping.setdefault("eoas", index)
    return mapping


def _looks_like_product_release(cell: str) -> bool:
    text = _clean(cell)
    if not text or len(text) > 180:
        return False
    # Nested matrix / date fragments.
    if _DATE_FRAGMENT_RE.match(text):
        return False
    if re.match(r"^\d{1,2}\s+\w+\s+\d{4}$", text):
        return False
    lowered = text.lower()
    if lowered in {"version", "ga", "eom", "eol", "product release"}:
        return False
    # Real lifecycle rows name a SUSE (or legacy Novell) product.
    if not re.search(r"\b(?:suse|sles|opensuse|novell)\b", text, re.I):
        return False
    # Must include a version / SP marker.
    if not (
        _SP_IN_TEXT_RE.search(text)
        or re.search(r"\b\d+(?:\.\d+)*\b", text)
    ):
        return False
    return True


def _split_product_release(cell: str) -> tuple[str, str]:
    text = _clean(cell)
    match = _PRODUCT_RELEASE_RE.match(text)
    if match:
        product = _clean(match.group("product"))
        major = match.group("major")
        # Reject date leftovers like product="29 Jun" major="2026".
        if _DATE_FRAGMENT_RE.match(product) or _MONTH_NAME_RE.match(product):
            return "", ""
        if len(major) == 4 and major.startswith(("19", "20")):
            # Year-only "release" from a date cell.
            return "", ""
        release = _normalize_sp_release(major, match.group("sp"))
        if not re.search(r"[A-Za-z]", product):
            return "", ""
        return product, release
    # Fallback: last SP or version token.
    sp_match = list(_SP_IN_TEXT_RE.finditer(text))
    if sp_match:
        last = sp_match[-1]
        release = _normalize_sp_release(last.group(1), last.group(2))
        product = _clean(text[: last.start()])
        if _DATE_FRAGMENT_RE.match(product) or not re.search(r"[A-Za-z]", product):
            return "", ""
        return product, release
    versions = re.findall(r"\d+(?:\.\d+)*", text)
    if versions:
        release = versions[-1]
        if len(release) == 4 and release.startswith(("19", "20")):
            return "", ""
        product = _clean(re.sub(re.escape(release) + r"\s*$", "", text))
        if _DATE_FRAGMENT_RE.match(product) or not re.search(r"[A-Za-z]", product):
            return "", ""
        return product, release
    return "", ""


def parse_suse_lifecycle_tables(html: str) -> list[dict[str, str]]:
    """Parse Service Pack lifecycle tables with General Ends + LTSS Ends.

    Mapping:
      General Ends / General Support Ends → eol_date
      LTSS Ends                           → eoas_date
      FCS Date                           → released_date
    """
    soup = BeautifulSoup(html, "html.parser")
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    today = date.today().isoformat()

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = [
            cell.get_text(" ", strip=True) for cell in rows[0].find_all(["th", "td"])
        ]
        columns = _header_map(header_cells)
        # Need a release column + General Ends. FCS / LTSS are optional
        # (overview Product Release table has no FCS; Desktop has no LTSS).
        if "release" not in columns or "eol" not in columns:
            continue
        # Skip nested Version/GA/EOM matrices (headers start with Version).
        header_join = " ".join(_clean(h).lower() for h in header_cells)
        if header_join.startswith("version") and "ga" in header_join:
            continue

        for row in rows[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
            if not cells:
                continue

            release_idx = columns["release"]
            release_cell = cells[release_idx] if release_idx < len(cells) else ""
            index_shift = 0
            # Overview table headers include a leading empty column; some body
            # rows omit it, so Product Release data lands at index 0.
            if not _looks_like_product_release(release_cell):
                for alt in range(min(3, len(cells))):
                    if _looks_like_product_release(cells[alt]):
                        index_shift = release_idx - alt
                        release_idx = alt
                        release_cell = cells[alt]
                        break
            if not _looks_like_product_release(release_cell):
                continue

            product_name, release_name = _split_product_release(release_cell)
            if not product_name or not release_name:
                continue
            if _DATE_FRAGMENT_RE.match(product_name):
                continue
            if len(release_name) == 4 and release_name.startswith(("19", "20")):
                continue

            def cell(key: str) -> str:
                pos = columns.get(key)
                if pos is None:
                    return ""
                pos = pos - index_shift
                if pos < 0 or pos >= len(cells):
                    return ""
                return cells[pos]

            frs = _parse_suse_date(cell("released"))
            eol = _parse_suse_date(cell("eol"))
            eoas = _parse_suse_date(cell("eoas"))
            # Skip rows with no usable lifecycle dates.
            if not frs and not eol and not eoas:
                continue
            support_end = eoas or eol
            is_supported = 1 if support_end and support_end >= today else 0
            slug = _slugify(product_name)

            candidate = {
                "product_slug": slug,
                "product_name": product_name,
                "release_name": release_name,
                "released_date": frs,
                "eol_date": eol,
                "eoas_date": eoas,
                "latest_raw": "",
                "is_supported": str(is_supported),
            }
            key = (slug, release_name.lower())
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = candidate
                continue
            # Prefer the row with the latest FCS / richer dates when duplicates appear.
            existing_frs = existing.get("released_date") or ""
            if (frs or "") > existing_frs:
                by_key[key] = candidate
            else:
                by_key[key] = {
                    **existing,
                    "released_date": existing_frs or frs,
                    "eol_date": existing.get("eol_date") or eol,
                    "eoas_date": existing.get("eoas_date") or eoas,
                    "is_supported": str(
                        max(
                            int(existing.get("is_supported", "0") or 0),
                            is_supported,
                        )
                    ),
                }

    return list(by_key.values())


def sync_suse_database(
    db_path: Path | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, object]:
    init_db(db_path)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if progress_callback:
        progress_callback("fetch", 1, 2)

    response = requests.get(SOURCE_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    releases = parse_suse_lifecycle_tables(response.text)
    if not releases:
        raise ValueError("SUSE lifecycle page parsed zero releases.")

    if progress_callback:
        progress_callback("store", 2, 2)

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    products: dict[str, str] = {}
    for release in releases:
        products[release["product_slug"]] = release["product_name"]

    with _connect(db_path) as connection:
        connection.execute("DELETE FROM releases")
        connection.execute("DELETE FROM products")
        for slug, name in products.items():
            connection.execute(
                """
                INSERT INTO products(slug, name, category, url, scraped_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (slug, name, "os", SOURCE_URL, scraped_at),
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
        _set_metadata(connection, "last_sync_status", "ok")
        _set_metadata(
            connection,
            "last_sync_message",
            f"Synced {len(products)} products / {len(releases)} releases from SUSE.",
        )
        _set_metadata(connection, "last_sync_started", started)
        connection.commit()

    return {
        "ok": True,
        "product_count": len(products),
        "release_count": len(releases),
        "started": started,
        "finished": scraped_at,
        "source_url": SOURCE_URL,
    }


def _normalize_release_key(value: str) -> str:
    text = _clean(value)
    sp = _SP_IN_TEXT_RE.search(text)
    if sp:
        return _normalize_sp_release(sp.group(1), sp.group(2)).lower()
    return text.lower()


def _suse_version_hints(os_name: str) -> list[str]:
    text = str(os_name or "")
    hints: list[str] = []
    seen: set[str] = set()

    for match in _SP_IN_TEXT_RE.finditer(text):
        hint = _normalize_sp_release(match.group(1), match.group(2))
        key = hint.lower()
        if key not in seen:
            seen.add(key)
            hints.append(hint)

    for hint in extract_version_hints(text):
        cleaned = _clean(hint)
        if not cleaned or cleaned in _NON_VERSION_HINTS:
            continue
        # Bare major is only useful when no SP hint exists.
        if "." not in cleaned and hints:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        hints.append(cleaned)
    return hints


def _release_score(release_name: str, hint: str) -> int:
    if not release_name or not hint:
        return 0
    rel = _normalize_release_key(release_name)
    hint_key = _normalize_release_key(hint)
    if rel == hint_key:
        return 100

    # Dotted hint "11.3" ↔ "11 SP3"
    dotted = re.match(r"^(\d+)\.(\d+)$", hint_key)
    if dotted:
        mapped = _normalize_sp_release(dotted.group(1), dotted.group(2)).lower()
        if rel == mapped:
            return 100

    rel_sp = _SP_IN_TEXT_RE.search(rel)
    hint_sp = _SP_IN_TEXT_RE.search(hint_key)
    if rel_sp and hint_sp:
        return 0  # different SP trains already failed equality
    if rel_sp and not hint_sp:
        # Bare major must not match an SP release.
        if hint_key == rel_sp.group(1):
            return 0
    if hint_sp and not rel_sp:
        if hint_sp.group(1) == rel:
            return 0

    if "sp" in rel or "sp" in hint_key:
        return 0
    return score_release_against_hint(rel, hint_key)


def _pick_release(releases: list[sqlite3.Row], hints: list[str]) -> sqlite3.Row | None:
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


def _resolve_product_slug(query: str, products: list[sqlite3.Row]) -> str | None:
    lowered = query.lower()
    if not lowered:
        return None

    best_slug = None
    best_score = 0.0
    for product in products:
        slug = _clean(product["slug"])
        name = _clean(product["name"])
        score = 0.0
        if name and name.lower() in lowered:
            score = max(score, 95.0)
        if slug.replace("-", " ") in lowered or slug in lowered:
            score = max(score, 85.0)
        if lowered in name.lower():
            score = max(score, 80.0)

        # Explicit edition keywords.
        if re.search(r"\bdesktop\b", lowered) and "desktop" in slug:
            score = max(score, 96.0)
        if re.search(r"\bsap\b", lowered) and "sap" in slug:
            score = max(score, 96.0)
        if re.search(r"\bhpc\b", lowered) and "hpc" in slug:
            score = max(score, 96.0)

        # Generic SUSE / SLES → prefer core SLES (not Desktop/SAP/HPC/…).
        is_core_sles = (
            slug == "suse-linux-enterprise-server"
            or (
                "enterprise-server" in slug
                and "sap" not in slug
                and "hpc" not in slug
                and "realtime" not in slug
            )
        )
        if is_core_sles and not re.search(
            r"\b(?:desktop|sap|hpc|realtime|jeos|micro|virtualization)\b",
            lowered,
        ):
            if re.search(r"\bsles\b", lowered):
                score = max(score, 94.0)
            elif re.search(r"\bsuse\b", lowered):
                score = max(score, 90.0)

        if score:
            score += min(len(name), 40) / 1000.0  # tiny tie-break only
        if score > best_score:
            best_score = score
            best_slug = slug
    return best_slug if best_score >= 60 else None


def lookup_os_suse(
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
        "source": "suse",
    }

    if not query_matches_suse(
        cleaned_name, os_string, normalized_os, normalized_os_detailed_name
    ):
        empty["api_note"] = "Not a SUSE/SLES OS string"
        return empty

    init_db(db_path)
    with _connect(db_path) as connection:
        release_count = connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        if not release_count:
            empty["api_note"] = (
                "Local SUSE database is empty. Run Update under Vendor Lookups first."
            )
            return empty

        products = list(connection.execute("SELECT slug, name FROM products ORDER BY name"))
        product_slug = _resolve_product_slug(cleaned_name, products)
        if not product_slug and cleaned_name != _clean(os_string):
            product_slug = _resolve_product_slug(_clean(os_string), products)
        if not product_slug:
            empty["api_note"] = "No matching SUSE product in local database"
            return empty

        product = connection.execute(
            "SELECT slug, name FROM products WHERE slug = ?", (product_slug,)
        ).fetchone()
        product_name = _clean(product["name"]) if product else product_slug
        if _clean(os_string) and not vendors_compatible(os_string, product_name):
            empty["product_slug"] = product_slug
            empty["api_note"] = f"SUSE product '{product_name}' does not match OS vendor"
            return empty

        releases = list(
            connection.execute(
                """
                SELECT release_name, released_date, eol_date, eoas_date,
                       latest_raw, is_supported
                FROM releases
                WHERE product_slug = ?
                ORDER BY released_date DESC, release_name DESC
                """,
                (product_slug,),
            )
        )
        selected = _pick_release(releases, _suse_version_hints(cleaned_name))
        if not selected and cleaned_name != _clean(os_string):
            selected = _pick_release(releases, _suse_version_hints(_clean(os_string)))
        if not selected:
            empty["product_slug"] = product_slug
            empty["api_note"] = "No matching SUSE release in local database"
            return empty

        eol_iso = _clean(selected["eol_date"])
        eoas_iso = _clean(selected["eoas_date"])
        release_name = _clean(selected["release_name"])
        label = f"{product_name} {release_name}".strip()

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
            "source": "suse",
        }


def lookup_os_suse_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, str]]:
    return [
        lookup_os_suse(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
            reference_date=reference_date,
            db_path=db_path,
        )
        for item in items
    ]


def list_all_rows(db_path: Path | None = None) -> list[dict[str, object]]:
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
