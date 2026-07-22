"""Scrape Junos OS lifecycle data from Juniper EOL into a local SQLite cache."""

from __future__ import annotations

import re
import sqlite3
import threading
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
from vendor_settings import query_matches_keywords, source_keywords


SOURCE_URL = "https://support.juniper.net/support/eol/software/junos/"
HEADERS = {
    "User-Agent": (
        "OS-Health-Check/1.0 (+local junos scraper; internal tool)"
    )
}
PRODUCT_SLUG = "junos"
PRODUCT_NAME = "Junos OS"
_PRODUCT_RELEASE_RE = re.compile(
    r"Junos\s+OS\s+(\d+(?:\.\d+)*(?:[Xx]\d+(?:\.\d+)*)?)",
    re.I,
)

# Accept only strong version matches (exact or multi-segment prefix).
_MIN_RELEASE_SCORE = 80
_NON_VERSION_HINTS = frozenset({"16", "32", "64", "86", "128", "256"})


def _clean(value: object) -> str:
    return str(value or "").strip()


def _default_db_path() -> Path:
    return Path(__file__).resolve().parent / "_data" / "junos_os.db"


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
        "category": "junos",
        "source_id": "junos",
        "source_label": "Juniper Junos",
    }


def query_matches_junos(*values: object) -> bool:
    """True when the query matches configured Junos family keywords."""
    return query_matches_keywords(source_keywords("junos"), *values)


def _parse_us_date(value: str) -> str:
    text = _clean(value)
    if not text or text in {"-", "—", "N/A", "n/a"}:
        return ""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _extract_table_html(page_html: str) -> str:
    """Juniper embeds the EOL table as escaped HTML inside a CMS property blob."""
    match = re.search(
        r'"selector"\s*:\s*"sw-eol-table"\s*,\s*"properties"\s*:\s*\{\s*'
        r'"htmlContent"\s*:\s*\'(.*?)\'\s*\n?\s*\}',
        page_html,
        re.S,
    )
    if not match:
        raise ValueError(
            "Could not find Junos EOL table (sw-eol-table) on the Juniper page."
        )
    raw = match.group(1)
    return (
        raw.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\'", "'")
        .replace('\\"', '"')
    )


def _split_product_release(product_cell: str) -> tuple[str, str]:
    text = _clean(product_cell)
    match = _PRODUCT_RELEASE_RE.search(text)
    if match:
        return PRODUCT_NAME, match.group(1)
    # Fall back: first dotted version token (avoid trailing maintenance markers).
    versions = re.findall(r"\d+\.\d+(?:\.\d+)*", text)
    if versions:
        return PRODUCT_NAME, versions[0]
    versions = re.findall(r"\d+", text)
    if versions:
        return PRODUCT_NAME, versions[0]
    return PRODUCT_NAME, text


def parse_junos_table(html: str) -> list[dict[str, str]]:
    """Parse the Junos OS Dates & Milestones table.

    Mapping (per product decision):
      End of Engineering (EOE) → eol_date
      End of Support (EOS)     → eoas_date
      FRS Date                 → released_date
    """
    table_html = _extract_table_html(html)
    soup = BeautifulSoup(table_html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("Junos EOL table HTML did not contain a <table>.")

    headers = [_clean(th.get_text(" ", strip=True)).lower() for th in table.find_all("th")]
    expected = {
        "product",
        "frs date",
        "end of engineering",
        "end of support",
    }
    if not expected.issubset(set(headers)):
        raise ValueError(f"Unexpected Junos table headers: {headers}")

    index = {label: position for position, label in enumerate(headers)}
    by_release: dict[str, dict[str, str]] = {}
    today = date.today().isoformat()

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        values = [cell.get_text(" ", strip=True) for cell in cells]

        def cell(label: str) -> str:
            pos = index.get(label)
            if pos is None or pos >= len(values):
                return ""
            return values[pos]

        product_cell = cell("product")
        if not product_cell:
            continue
        _, release_name = _split_product_release(product_cell)
        if not release_name:
            continue

        frs = _parse_us_date(cell("frs date"))
        eoe = _parse_us_date(cell("end of engineering"))  # → eol
        eos = _parse_us_date(cell("end of support"))  # → eoas
        release_type = _clean(cell("release type")) if "release type" in index else ""

        # Supported while End of Support (eoas) is still in the future (or missing
        # EOS but EOE still ahead). Empty both → unsupported.
        support_end = eos or eoe
        is_supported = 1 if support_end and support_end >= today else 0

        candidate = {
            "release_name": release_name,
            "released_date": frs,
            "eol_date": eoe,
            "eoas_date": eos,
            "latest_raw": release_type,
            "is_supported": str(is_supported),
        }
        existing = by_release.get(release_name)
        if existing is None:
            by_release[release_name] = candidate
            continue
        # Prefer the train with the latest FRS when Juniper lists overlapping rows.
        if (frs or "") > (existing.get("released_date") or ""):
            by_release[release_name] = candidate
        else:
            by_release[release_name] = {
                "release_name": release_name,
                "released_date": existing.get("released_date") or frs,
                "eol_date": existing.get("eol_date") or eoe,
                "eoas_date": existing.get("eoas_date") or eos,
                "latest_raw": existing.get("latest_raw") or release_type,
                "is_supported": str(
                    max(
                        int(existing.get("is_supported", "0") or 0),
                        is_supported,
                    )
                ),
            }

    return list(by_release.values())


def sync_junos_database(
    db_path: Path | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, object]:
    init_db(db_path)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if cancel_event is not None and cancel_event.is_set():
        return {
            "ok": False,
            "product_count": 0,
            "release_count": 0,
            "started": started,
            "finished": started,
            "source_url": SOURCE_URL,
            "cancelled": True,
        }
    if progress_callback:
        progress_callback("fetch", 1, 2)

    response = requests.get(SOURCE_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    releases = parse_junos_table(response.text)
    if not releases:
        raise ValueError("Junos EOL table parsed zero releases.")

    if progress_callback:
        progress_callback("store", 2, 2)

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as connection:
        connection.execute("DELETE FROM releases")
        connection.execute("DELETE FROM products")
        connection.execute(
            """
            INSERT INTO products(slug, name, category, url, scraped_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (PRODUCT_SLUG, PRODUCT_NAME, "os", SOURCE_URL, scraped_at),
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
                    PRODUCT_SLUG,
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
            f"Synced {len(releases)} Junos releases from Juniper.",
        )
        _set_metadata(connection, "last_sync_started", started)
        connection.commit()

    return {
        "ok": True,
        "product_count": 1,
        "release_count": len(releases),
        "started": started,
        "finished": scraped_at,
        "source_url": SOURCE_URL,
        "cancelled": False,
    }


def _version_tokens(text: str) -> list[str]:
    """Release identity tokens only — do not split ``24.2`` into ``24`` / ``2``.

    Bare majors are kept only when no dotted / X-train version is present, so a
    query like ``Junos 24`` can still match, while hint ``2`` cannot score 100
    against every ``*.2`` release.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    x_trains = re.findall(r"\d+(?:\.\d+)*[Xx]\d+(?:\.\d+)*", text or "")
    for token in x_trains:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    for token in re.findall(r"\d+(?:\.\d+)+", text or ""):
        key = token.lower()
        # ``15.1X49`` also matches dotted ``15.1`` — drop that fragment.
        if any(xt.lower().startswith(f"{key}x") for xt in x_trains):
            continue
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    if not tokens:
        for token in re.findall(r"\d+", text or ""):
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(token)
    return tokens


def _junos_version_hints(os_name: str) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    text = str(os_name or "")
    candidates = _version_tokens(text)
    for hint in extract_version_hints(text):
        cleaned = _clean(hint)
        if not cleaned or "." not in cleaned:
            continue
        # Don't let ``15.1`` from ``15.1X53`` dilute an exact X-train hint.
        if any(
            token.lower().startswith(f"{cleaned.lower()}x")
            for token in candidates
        ):
            continue
        candidates.append(cleaned)
    for hint in candidates:
        cleaned = _clean(hint)
        if not cleaned or cleaned in _NON_VERSION_HINTS:
            continue
        if re.search(rf"(?<!\d){re.escape(cleaned)}\.x\b", text, re.I):
            continue
        # Drop lone SP/R/U pack digits kept by bare-token extraction (e.g. SP3 → 3).
        if "." not in cleaned and _is_service_pack_digit(text, cleaned):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        hints.append(cleaned)
    return hints


def _is_service_pack_digit(os_name: str, digit: str) -> bool:
    """True when every occurrence of ``digit`` is an SP/R/U pack marker."""
    found = False
    for match in re.finditer(rf"(?<!\d){re.escape(digit)}(?!\d)", os_name):
        found = True
        prefix = os_name[max(0, match.start() - 4) : match.start()]
        if not re.search(r"(?:^|[^A-Za-z0-9])(?:SP|R|U)\s*$", prefix, re.I):
            return False
    return found


def _normalize_version_key(value: str) -> str:
    return _clean(value).lower().replace("x", "x")


def _version_match_score(release_version: str, hint: str) -> int:
    if not release_version or not hint:
        return 0
    rel = _normalize_version_key(release_version)
    hint_key = _normalize_version_key(hint)
    if rel == hint_key:
        return 100
    rel_has_x = "x" in rel
    hint_has_x = "x" in hint_key
    rel_base = re.split(r"x", rel, maxsplit=1)[0]
    hint_base = re.split(r"x", hint_key, maxsplit=1)[0]
    # Different X-trains that share a base (15.1X53 vs 15.1X49) must not match.
    if rel_has_x and hint_has_x and rel != hint_key:
        return 0
    # Family-only hint (15.1) must not guess an X-train (15.1X53) — if unsure, blank.
    if rel_has_x and not hint_has_x:
        return 0
    # X-train hint against a non-X release with the same base — too ambiguous.
    if hint_has_x and not rel_has_x:
        return 0
    return score_release_against_hint(rel_base, hint_base)


def _release_score(release_name: str, hint: str) -> int:
    best = 0
    for candidate in _version_tokens(release_name):
        best = max(best, _version_match_score(candidate, hint))
    return best


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


def lookup_os_junos(
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
        "source": "junos",
    }

    if not query_matches_junos(
        cleaned_name, os_string, normalized_os, normalized_os_detailed_name
    ):
        empty["api_note"] = "Not a Junos/Juniper OS string"
        return empty

    init_db(db_path)
    with _connect(db_path) as connection:
        release_count = connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
        if not release_count:
            empty["api_note"] = (
                "Local Junos database is empty. Run Update under Vendor Lookups first."
            )
            return empty

        if _clean(os_string) and not vendors_compatible(os_string, PRODUCT_NAME):
            empty["product_slug"] = PRODUCT_SLUG
            empty["api_note"] = "Junos product does not match OS vendor"
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
                (PRODUCT_SLUG,),
            )
        )
        selected = _pick_release(releases, _junos_version_hints(cleaned_name))
        if not selected and cleaned_name != _clean(os_string):
            selected = _pick_release(releases, _junos_version_hints(_clean(os_string)))
        if not selected:
            empty["product_slug"] = PRODUCT_SLUG
            empty["api_note"] = "No matching Junos release in local database"
            return empty

        eol_iso = _clean(selected["eol_date"])
        eoas_iso = _clean(selected["eoas_date"])
        release_name = _clean(selected["release_name"])
        release_type = _clean(selected["latest_raw"])
        label = f"{PRODUCT_NAME} {release_name}"
        if release_type:
            label = f"{label} ({release_type})"

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
            "product_slug": PRODUCT_SLUG,
            "release_name": release_name,
            "release_label": label,
            "source": "junos",
        }


def lookup_os_junos_batch(
    items: list[dict[str, str]],
    reference_date: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, str]]:
    return [
        lookup_os_junos(
            item.get("os_string", ""),
            item.get("normalized_os_detailed_name", ""),
            item.get("normalized_os", ""),
            reference_date=reference_date,
            db_path=db_path,
        )
        for item in items
    ]


def list_all_rows(db_path: Path | None = None) -> list[dict[str, object]]:
    """Return every scraped Junos release for the Vendor Lookups viewer."""
    init_db(db_path)
    rows: list[dict[str, object]] = []
    with _connect(db_path) as connection:
        cursor = connection.execute(
            """
            SELECT release_name, released_date, eol_date, eoas_date,
                   latest_raw, is_supported
            FROM releases
            WHERE product_slug = ?
            ORDER BY is_supported DESC, released_date DESC, release_name DESC
            """,
            (PRODUCT_SLUG,),
        )
        for release in cursor:
            rows.append(
                {
                    "product": PRODUCT_NAME,
                    "release": _clean(release["release_name"]),
                    "released": _clean(release["released_date"]),
                    "eol_date": _clean(release["eol_date"]),
                    "eoas_date": _clean(release["eoas_date"]),
                    "supported": bool(release["is_supported"]),
                    "release_type": _clean(release["latest_raw"]),
                }
            )
    return rows
