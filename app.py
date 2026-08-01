from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from eol_service import lookup_os_eol_batch
from vendor_lookups.eosl_service import (
    get_status as eosl_get_status,
    list_all_rows as eosl_list_all_rows,
    lookup_os_eosl_batch,
    sync_os_database as eosl_sync_os_database,
)
from vendor_lookups.junos_service import (
    get_status as junos_get_status,
    list_all_rows as junos_list_all_rows,
    lookup_os_junos_batch,
    sync_junos_database,
)
from vendor_lookups.suse_service import (
    get_status as suse_get_status,
    list_all_rows as suse_list_all_rows,
    lookup_os_suse_batch,
    sync_suse_database,
)
from vendor_lookups.vendor_lookup_service import (
    get_lookup_settings as vendor_get_lookup_settings,
    get_status as vendor_get_status,
    list_rows as vendor_list_rows,
    list_sources as vendor_list_sources,
    lookup_vendor_batch,
    save_lookup_settings as vendor_save_lookup_settings,
    save_source_preferences as vendor_save_source_preferences,
    sync_source as vendor_sync_source,
)
from normalization_service import (
    AI_MODEL_CHOICES,
    DEFAULT_AI_MATCH_PROMPT,
    DEFAULT_AI_MODELS,
    DEFAULT_FUZZY_MATCH_THRESHOLD,
    collapse_consecutive_duplicate_words,
    detect_ambiguous_os_batch,
    gemini_model_name,
    normalize_ai_provider,
    openai_model_name,
    openrouter_model_name,
    provider_api_key_configured,
    strip_ai_match_response_format,
    suggest_normalization_batch,
)
from os_import_service import extract_distinct_os_values, inspect_os_import_file
from lookup_extras import (
    build_eol_evidence_slot,
    build_evidence_entries,
    compute_lookup_diff,
    is_ambiguous_row,
    merge_lookup_rows,
    row_matched_by,
)
import lookup_db


load_dotenv()

# Set once at process start. DATABASE_URL alone is NOT enough to switch the
# lookup data to Postgres -- every existing deployment already sets it
# unconditionally for the vendor-lookup caches (vendor_lookups/db.py), so
# treating its mere presence as "also move the main lookup data" would
# silently flip a working file-mode deployment into DB mode nobody asked
# for (this happened during development: a docker-compose environment with
# DATABASE_URL configured only for vendor caches ended up reading an empty
# Postgres table for draft-existence checks while still writing rows to the
# file, corrupting the Data-vs-Draft diff). LOOKUP_DB_ENABLED is a separate,
# explicit opt-in required in addition to DATABASE_URL.
_USE_DB = bool(os.environ.get("DATABASE_URL")) and str(os.environ.get("LOOKUP_DB_ENABLED", "")).strip().lower() in (
    "1", "true", "yes", "on",
)

# Optional, DB-mode-only: also write each successful publish out to
# _data/eol_lookup.csv / _data/eol_lookup_evidence.json / _data/.revision, so
# a single-instance deployment can keep a git-trackable snapshot alongside
# Postgres as the source of truth. Off by default -- Postgres is never read
# back from these files while _USE_DB is on, and if more than one app
# instance shares the same database, each instance's mirrored files only
# reflect publishes *it* performed, not the other instances' -- they will
# drift stale relative to the real (Postgres) Data whenever someone else
# publishes from a different instance.
_MIRROR_FILES = _USE_DB and str(os.environ.get("LOOKUP_DB_MIRROR_FILES", "")).strip().lower() in (
    "1", "true", "yes", "on",
)


BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "_data" / "eol_lookup.csv"
DRAFT_PATH = BASE_DIR / "_draft" / "eol_lookup.csv"
DATA_EVIDENCE_PATH = BASE_DIR / "_data" / "eol_lookup_evidence.json"
DRAFT_EVIDENCE_PATH = BASE_DIR / "_draft" / "eol_lookup_evidence.json"
# Frozen copy of Data taken the moment a draft is created, used as the
# publish-time 3-way merge base -- lets publish tell "upstream changed this"
# apart from "I changed this" instead of blindly overwriting Data.
DRAFT_BASE_PATH = BASE_DIR / "_draft" / "eol_lookup.base.csv"
DRAFT_BASE_EVIDENCE_PATH = BASE_DIR / "_draft" / "eol_lookup.base_evidence.json"
DRAFT_BASE_REVISION_PATH = BASE_DIR / "_draft" / "eol_lookup.base.revision"
# Bumped by 1 on every successful publish; a cheap "has Data changed" signal
# for the frontend's staleness banner. Not used for merge decisions -- the
# base/current row comparison handles that with full row content.
DATA_REVISION_PATH = BASE_DIR / "_data" / ".revision"
BACKUP_DIR = BASE_DIR / "_backup"
CONFIG_DIR = BASE_DIR / "_config"
AZURE_CONFIG_PATH = CONFIG_DIR / "azure.json"
AWS_CONFIG_PATH = CONFIG_DIR / "aws.json"
APP_SETTINGS_PATH = CONFIG_DIR / "app_settings.json"
AI_MODEL_CHOICES_PATH = CONFIG_DIR / "ai_model_choices.json"
CSV_HEADERS = [
    "os_string",
    "normalized_os_detailed_name",
    "normalized_os",
    "eol_date",
    "eol_status",
    "eoas_date",
    "eoas_status",
]
STATIC_DIR = BASE_DIR / "static"


def static_v(rel_path: str) -> str:
    """Cache-busting query param derived from a static asset's own mtime, so
    editing one CSS/JS file busts the browser cache for just that file --
    no server restart needed, and untouched assets keep caching normally."""
    path = STATIC_DIR / rel_path
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return str(int(time.time()))


app = FastAPI(title="OS Health Check")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["static_v"] = static_v


@app.middleware("http")
async def no_cache_static_assets(request: Request, call_next):
    """Static JS/CSS are plain files with no build/hash step, and JS module
    imports (e.g. `import ... from './modals.js'`) don't inherit the entry
    script's cache-busting query param -- so a stale cached module can keep
    getting served long after the file changed. Never cache /static/* rather
    than debug that per file; these assets are small and low-traffic."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# Serialize vendor scrape runs so only one hits a remote source at a time.
VENDOR_SYNC_LOCK = asyncio.Lock()
# Back-compat alias used by older EOSL-only call sites.
EOSL_SYNC_LOCK = VENDOR_SYNC_LOCK
VALID_VENDOR_SOURCES = {
    "eosl",
    "microsoft-lifecycle",
    "junos",
    "suse",
    "layer23-switch",
    "router-switch",
}
# In-flight vendor sync jobs (job_id -> cancel event) so the Stop button can
# reach a background scrape that's already streaming to a client.
ACTIVE_VENDOR_SYNC_JOBS: dict[str, threading.Event] = {}

# Serialize/track "Refresh EOL/EOAS" streaming jobs separately from vendor
# scrapes so a bulk lookup refresh doesn't queue behind a vendor DB sync.
LOOKUP_REFRESH_LOCK = asyncio.Lock()
ACTIVE_LOOKUP_REFRESH_JOBS: dict[str, threading.Event] = {}


class LookupRow(BaseModel):
    os_string: str = ""
    normalized_os_detailed_name: str = ""
    normalized_os: str = ""
    eol_date: str = ""
    eol_status: str = ""
    eoas_date: str = ""
    eoas_status: str = ""

    @field_validator("eol_status", "eoas_status", mode="before")
    @classmethod
    def validate_boolean_or_null_status(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"", "true", "false"}:
            return normalized
        raise ValueError("Status fields must be true, false, or empty.")


class LookupPayload(BaseModel):
    rows: list[LookupRow] = Field(default_factory=list)
    # Sidecar evidence keyed by os_string. Not written into the lookup CSV.
    evidence: dict[str, object] = Field(default_factory=dict)
    # Optional label appended to Validate backup filenames.
    backup_suffix: str = ""
    # Draft-save only: the Data rows/evidence the client actually forked
    # this draft from, recorded as the publish-time merge base. Sent once
    # when a draft is created (or reset via "Revert all changes"); omitted
    # on ordinary autosaves of an already-existing draft.
    base_rows: list[LookupRow] | None = None
    base_evidence: dict[str, object] | None = None
    reset_base: bool = False
    # Publish only: per-conflict choice ("mine" or "theirs") from a prior
    # /api/lookup/validate/check call. A conflict missing here is still
    # unresolved and blocks the publish.
    conflict_resolutions: dict[str, str] = Field(default_factory=dict)


class EolLookupItem(BaseModel):
    os_string: str = ""
    normalized_os_detailed_name: str = ""
    normalized_os: str = ""


class EolLookupBatchRequest(BaseModel):
    items: list[EolLookupItem] = Field(default_factory=list)


class VendorSyncRequest(BaseModel):
    """Optional sync / preference options for vendor sources."""

    manufacturers: list[str] | None = None
    enabled: bool | None = None
    keywords: list[str] | None = None


class VendorLookupSettingsSource(BaseModel):
    enabled: bool | None = None
    keywords: list[str] | None = None


class VendorLookupSettingsRequest(BaseModel):
    sources: dict[str, VendorLookupSettingsSource] = Field(default_factory=dict)


class NormalizationPair(BaseModel):
    normalized_os_detailed_name: str = ""
    normalized_os: str = ""


class NormalizeSuggestItem(BaseModel):
    os_string: str = ""


class NormalizeSuggestRequest(BaseModel):
    items: list[NormalizeSuggestItem] = Field(default_factory=list)
    allowed_pairs: list[NormalizationPair] = Field(default_factory=list)
    fuzzy_match_threshold: int = Field(
        default=DEFAULT_FUZZY_MATCH_THRESHOLD,
        ge=50,
        le=100,
    )


class NormalizeSuggestResult(BaseModel):
    normalized_os_detailed_name: str = ""
    normalized_os: str = ""


class AmbiguousOsDetectRequest(BaseModel):
    items: list[NormalizeSuggestItem] = Field(default_factory=list)


class AzureUploadRequest(BaseModel):
    account_name: str = Field(min_length=1)
    container_name: str = Field(min_length=1)
    blob_name: str = Field(min_length=1)

    @field_validator("account_name", "container_name", "blob_name", mode="before")
    @classmethod
    def strip_required_fields(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Azure upload settings cannot be empty.")
        return normalized

    @field_validator("blob_name", mode="after")
    @classmethod
    def validate_blob_name(cls, value: str) -> str:
        if value.startswith("/"):
            raise ValueError("Blob path must not start with /.")
        return value


class AzureProfile(BaseModel):
    id: str = ""
    name: str = ""
    account_name: str = ""
    container_name: str = ""
    blob_name: str = ""

    @field_validator("id", "name", "account_name", "container_name", "blob_name", mode="before")
    @classmethod
    def strip_optional_fields(cls, value: object) -> str:
        return str(value or "").strip()


class AzureSettingsStore(BaseModel):
    active_profile_id: str = ""
    profiles: list[AzureProfile] = Field(default_factory=list)

    @field_validator("active_profile_id", mode="before")
    @classmethod
    def strip_active_profile_id(cls, value: object) -> str:
        return str(value or "").strip()


class AzureSettings(BaseModel):
    """Legacy single-target shape kept for older clients/tests."""

    account_name: str = ""
    container_name: str = ""
    blob_name: str = ""


class AzureSettingsSaveRequest(BaseModel):
    active_profile_id: str = ""
    profiles: list[AzureProfile] = Field(default_factory=list)

    @field_validator("active_profile_id", mode="before")
    @classmethod
    def strip_active_profile_id(cls, value: object) -> str:
        return str(value or "").strip()


class AwsProfile(BaseModel):
    id: str = ""
    name: str = ""
    bucket: str = ""
    region: str = ""
    key: str = ""

    @field_validator("id", "name", "bucket", "region", "key", mode="before")
    @classmethod
    def strip_optional_fields(cls, value: object) -> str:
        return str(value or "").strip()


class AwsSettingsStore(BaseModel):
    active_profile_id: str = ""
    profiles: list[AwsProfile] = Field(default_factory=list)

    @field_validator("active_profile_id", mode="before")
    @classmethod
    def strip_active_profile_id(cls, value: object) -> str:
        return str(value or "").strip()


class AwsSettingsSaveRequest(BaseModel):
    active_profile_id: str = ""
    profiles: list[AwsProfile] = Field(default_factory=list)

    @field_validator("active_profile_id", mode="before")
    @classmethod
    def strip_active_profile_id(cls, value: object) -> str:
        return str(value or "").strip()


class RowRefreshRequest(BaseModel):
    """Re-run EOL/EOAS lookup (endoflife.date, then the vendor cascade) for one row."""

    row: LookupRow


class RowsRefreshRequest(BaseModel):
    """Bulk variant of RowRefreshRequest for the selection's Refresh lifecycle action."""

    rows: list[LookupRow] = Field(default_factory=list)


class LookupRefreshStreamRequest(BaseModel):
    """Whole-table 'Refresh EOL/EOAS' with streamed progress. Persists on completion."""

    rows: list[LookupRow] = Field(default_factory=list)
    evidence: dict[str, object] = Field(default_factory=dict)
    source: str = "draft"


class LookupValidateStreamRequest(LookupPayload):
    """Same body as /api/lookup/validate, just streamed for progress UI parity."""


def _clean_ai_models(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for provider in ("openai", "gemini", "openrouter"):
        model = str(value.get(provider) or "").strip()
        if model:
            cleaned[provider] = model
    return cleaned


class AppSettings(BaseModel):
    ai_enabled: bool = False
    ai_provider: str = "openai"
    # Empty means use DEFAULT_AI_MATCH_PROMPT at match time.
    ai_match_prompt: str = ""
    # Confidence cutoff (0-100) an AI match must meet to be accepted.
    ai_confidence_threshold: int = 85
    # Per-provider model override; missing/empty means use that provider's default.
    ai_models: dict[str, str] = Field(default_factory=dict)

    @field_validator("ai_provider", mode="before")
    @classmethod
    def validate_ai_provider(cls, value: object) -> str:
        return normalize_ai_provider(value)

    @field_validator("ai_match_prompt", mode="before")
    @classmethod
    def validate_ai_match_prompt(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("ai_confidence_threshold", mode="before")
    @classmethod
    def validate_ai_confidence_threshold(cls, value: object) -> int:
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            threshold = 85
        return max(50, min(100, threshold))

    @field_validator("ai_models", mode="before")
    @classmethod
    def validate_ai_models(cls, value: object) -> dict[str, str]:
        return _clean_ai_models(value)


class AppSettingsUpdateRequest(BaseModel):
    """Partial settings update. Omit a field to leave it unchanged."""

    ai_enabled: bool = False
    ai_provider: str = "openai"
    ai_match_prompt: str | None = None
    ai_confidence_threshold: int | None = None
    ai_models: dict[str, str] | None = None

    @field_validator("ai_provider", mode="before")
    @classmethod
    def validate_ai_provider(cls, value: object) -> str:
        return normalize_ai_provider(value)

    @field_validator("ai_confidence_threshold")
    @classmethod
    def validate_ai_confidence_threshold(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return max(50, min(100, int(value)))

    @field_validator("ai_models", mode="before")
    @classmethod
    def validate_ai_models(cls, value: object) -> dict[str, str] | None:
        if value is None:
            return None
        return _clean_ai_models(value)


class AppSettingsResponse(BaseModel):
    ai_enabled: bool = False
    ai_provider: str = "openai"
    ai_available: bool = False
    openai_available: bool = False
    gemini_available: bool = False
    openrouter_available: bool = False
    ai_match_prompt: str = ""
    default_ai_match_prompt: str = DEFAULT_AI_MATCH_PROMPT
    ai_confidence_threshold: int = 85
    ai_models: dict[str, str] = Field(default_factory=dict)
    ai_model_choices: dict[str, list[str]] = Field(default_factory=dict)
    default_ai_models: dict[str, str] = Field(default_factory=dict)


AZURE_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def openai_api_key_configured() -> bool:
    return provider_api_key_configured("openai")


def gemini_api_key_configured() -> bool:
    return provider_api_key_configured("gemini")


def openrouter_api_key_configured() -> bool:
    return provider_api_key_configured("openrouter")


def selected_ai_provider_available(settings: AppSettings | None = None) -> bool:
    current = settings or load_app_settings()
    return provider_api_key_configured(current.ai_provider)

def lookup_path(source: str) -> Path:
    normalized = source.strip().lower()
    if normalized == "data":
        return DATA_PATH
    if normalized == "draft":
        return DRAFT_PATH
    raise HTTPException(status_code=400, detail="Unsupported lookup source.")


def evidence_path(source: str) -> Path:
    normalized = source.strip().lower()
    if normalized == "data":
        return DATA_EVIDENCE_PATH
    if normalized == "draft":
        return DRAFT_EVIDENCE_PATH
    raise HTTPException(status_code=400, detail="Unsupported lookup source.")


def empty_evidence_payload() -> dict[str, object]:
    return {"by_os": {}, "updated_at": ""}


def normalize_evidence_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return empty_evidence_payload()

    by_os_raw = payload.get("by_os")
    by_os: dict[str, object] = {}
    if isinstance(by_os_raw, dict):
        for key, value in by_os_raw.items():
            os_key = str(key or "").strip()
            if not os_key or not isinstance(value, dict):
                continue
            by_os[os_key] = value

    updated_at = str(payload.get("updated_at") or "").strip()
    return {"by_os": by_os, "updated_at": updated_at}


def prune_evidence_to_rows(
    evidence: dict[str, object], rows: list[LookupRow]
) -> dict[str, object]:
    normalized = normalize_evidence_payload(evidence)
    by_os = normalized.get("by_os")
    if not isinstance(by_os, dict):
        return empty_evidence_payload()

    allowed = {str(row.os_string or "").strip() for row in rows}
    allowed.discard("")
    pruned = {key: value for key, value in by_os.items() if key in allowed}
    return {
        "by_os": pruned,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _load_evidence_file(source: str = "data") -> dict[str, object]:
    path = evidence_path(source)
    if not path.exists():
        return empty_evidence_payload()

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty_evidence_payload()

    return normalize_evidence_payload(payload)


def _save_evidence_file(evidence: dict[str, object], source: str = "data") -> dict[str, object]:
    path = evidence_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_evidence_payload(evidence)
    if not normalized.get("updated_at"):
        normalized["updated_at"] = datetime.now().isoformat(timespec="seconds")

    temp_dir = path.parent
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=temp_dir,
        suffix=".json",
    ) as handle:
        json.dump(normalized, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)

    temp_path.replace(path)
    return normalized


def _delete_evidence_file(source: str) -> None:
    path = evidence_path(source)
    if path.exists():
        path.unlink()


def load_evidence(source: str = "data") -> dict[str, object]:
    if _USE_DB:
        return lookup_db.db_load_evidence(source)
    return _load_evidence_file(source)


def save_evidence(evidence: dict[str, object], source: str = "data") -> dict[str, object]:
    if _USE_DB:
        return lookup_db.db_save_evidence(evidence, source)
    return _save_evidence_file(evidence, source)


def delete_evidence(source: str) -> None:
    if _USE_DB:
        # Every call site passes "draft" -- Data's evidence is always
        # replaced via db_publish, never bare-deleted. db_delete_draft
        # covers both rows and evidence for the draft source in one call.
        if source.strip().lower() != "draft":
            raise HTTPException(status_code=400, detail="Only the draft evidence can be deleted directly.")
        lookup_db.db_delete_draft()
        return
    _delete_evidence_file(source)


def _data_exists() -> bool:
    return lookup_db.db_source_exists("data") if _USE_DB else DATA_PATH.exists()


def _draft_exists() -> bool:
    return lookup_db.db_source_exists("draft") if _USE_DB else DRAFT_PATH.exists()


def _source_exists(source: str) -> bool:
    normalized = source.strip().lower()
    if normalized == "draft":
        return _draft_exists()
    if normalized == "data":
        return _data_exists()
    raise HTTPException(status_code=400, detail="Unsupported lookup source.")


def _normalize_status_cell(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "true", "false"}:
        return normalized
    return ""


def _read_rows_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CSV_HEADERS:
            raise HTTPException(
                status_code=500,
                detail="CSV headers do not match the expected lookup schema.",
            )

        rows: list[dict[str, str]] = []
        for row in reader:
            item = {header: (row.get(header) or "") for header in CSV_HEADERS}
            item["eol_status"] = _normalize_status_cell(item.get("eol_status"))
            item["eoas_status"] = _normalize_status_cell(item.get("eoas_status"))
            rows.append(item)
        return rows


def _write_rows_csv(rows: list[LookupRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = path.parent
    with NamedTemporaryFile(
        "w",
        newline="",
        encoding="utf-8",
        delete=False,
        dir=temp_dir,
        suffix=".csv",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.model_dump())
        temp_path = Path(handle.name)

    temp_path.replace(path)


def load_rows(source: str = "data") -> list[dict[str, str]]:
    if _USE_DB:
        return lookup_db.db_load_rows(source)
    path = lookup_path(source)
    if not path.exists():
        detail = "Draft lookup CSV not found." if source == "draft" else "Lookup CSV not found."
        raise HTTPException(status_code=404, detail=detail)
    return _read_rows_csv(path)


def save_rows(rows: list[LookupRow], source: str = "data") -> None:
    if _USE_DB:
        lookup_db.db_save_rows([row.model_dump() for row in rows], source)
        return
    _write_rows_csv(rows, lookup_path(source))


def sanitize_backup_suffix(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip(".-_")
    return text[:80]


def backup_data_file(suffix: str = "") -> Path | None:
    if not DATA_PATH.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix_part = f"_{suffix}" if suffix else ""
    backup_path = BACKUP_DIR / f"eol_lookup_{timestamp}{suffix_part}.csv"
    shutil.copy2(DATA_PATH, backup_path)
    return backup_path


def backup_data_evidence(suffix: str = "") -> Path | None:
    if not DATA_EVIDENCE_PATH.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix_part = f"_{suffix}" if suffix else ""
    backup_path = BACKUP_DIR / f"eol_lookup_evidence_{timestamp}{suffix_part}.json"
    shutil.copy2(DATA_EVIDENCE_PATH, backup_path)
    return backup_path


def read_data_revision() -> int:
    if _USE_DB:
        return lookup_db.db_data_revision()
    if not DATA_REVISION_PATH.exists():
        return 0
    try:
        return int(DATA_REVISION_PATH.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def bump_data_revision() -> int:
    DATA_REVISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    next_revision = read_data_revision() + 1
    DATA_REVISION_PATH.write_text(str(next_revision), encoding="utf-8")
    return next_revision


def load_base_rows() -> list[dict[str, str]]:
    if not DRAFT_BASE_PATH.exists():
        return []
    try:
        return _read_rows_csv(DRAFT_BASE_PATH)
    except HTTPException:
        # Corrupt/foreign-shaped base file -- degrade to "no upstream
        # context" (equivalent to today's blind-overwrite behavior) rather
        # than blocking every publish on a broken merge base.
        return []


def save_base_rows(rows: list[LookupRow], based_on_revision: int) -> None:
    _write_rows_csv(rows, DRAFT_BASE_PATH)
    DRAFT_BASE_REVISION_PATH.write_text(str(based_on_revision), encoding="utf-8")


def load_base_evidence() -> dict[str, object]:
    if not DRAFT_BASE_EVIDENCE_PATH.exists():
        return empty_evidence_payload()
    try:
        with DRAFT_BASE_EVIDENCE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty_evidence_payload()
    return normalize_evidence_payload(payload)


def save_base_evidence(evidence: dict[str, object]) -> None:
    DRAFT_BASE_EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_evidence_payload(evidence)
    with DRAFT_BASE_EVIDENCE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(normalized, handle, ensure_ascii=True, indent=2)
        handle.write("\n")


def read_draft_based_on_revision() -> int:
    if _USE_DB:
        return lookup_db.db_draft_based_on_revision()
    if not DRAFT_BASE_REVISION_PATH.exists():
        return 0
    try:
        return int(DRAFT_BASE_REVISION_PATH.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def delete_draft_base() -> None:
    for path in (DRAFT_BASE_PATH, DRAFT_BASE_EVIDENCE_PATH, DRAFT_BASE_REVISION_PATH):
        if path.exists():
            path.unlink()


def ensure_draft_base() -> None:
    """Backfills a base snapshot for a draft that predates this feature (no
    recorded base at all). Best-effort approximation using *current* Data,
    since there's no way to recover what Data actually looked like when
    that draft was first created -- new drafts going forward always get an
    exact base sent explicitly by the client instead of hitting this path."""
    if not DRAFT_PATH.exists() or DRAFT_BASE_PATH.exists():
        return
    if DATA_PATH.exists():
        save_base_rows([LookupRow(**row) for row in load_rows("data")], read_data_revision())
        save_base_evidence(load_evidence("data"))
    else:
        save_base_rows([], read_data_revision())
        save_base_evidence(empty_evidence_payload())


def _new_azure_profile_id() -> str:
    return uuid.uuid4().hex


def _legacy_azure_payload_to_store(payload: dict[str, object]) -> AzureSettingsStore:
    account_name = str(payload.get("account_name") or "").strip()
    container_name = str(payload.get("container_name") or "").strip()
    blob_name = str(payload.get("blob_name") or "").strip()
    if not account_name and not container_name and not blob_name:
        return AzureSettingsStore()
    profile_id = _new_azure_profile_id()
    return AzureSettingsStore(
        active_profile_id=profile_id,
        profiles=[
            AzureProfile(
                id=profile_id,
                name="Default",
                account_name=account_name,
                container_name=container_name,
                blob_name=blob_name,
            )
        ],
    )


def _normalize_azure_store(payload: dict[str, object] | None) -> AzureSettingsStore:
    if not isinstance(payload, dict):
        return AzureSettingsStore()

    # Legacy single-target file.
    if "profiles" not in payload and (
        "account_name" in payload or "container_name" in payload or "blob_name" in payload
    ):
        return _legacy_azure_payload_to_store(payload)

    raw_profiles = payload.get("profiles")
    profiles: list[AzureProfile] = []
    seen_ids: set[str] = set()
    if isinstance(raw_profiles, list):
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id") or "").strip() or _new_azure_profile_id()
            if profile_id in seen_ids:
                profile_id = _new_azure_profile_id()
            seen_ids.add(profile_id)
            name = str(item.get("name") or "").strip() or "Untitled"
            profiles.append(
                AzureProfile(
                    id=profile_id,
                    name=name,
                    account_name=str(item.get("account_name") or "").strip(),
                    container_name=str(item.get("container_name") or "").strip(),
                    blob_name=str(item.get("blob_name") or "").strip(),
                )
            )

    active_profile_id = str(payload.get("active_profile_id") or "").strip()
    if profiles:
        valid_ids = {profile.id for profile in profiles}
        if active_profile_id not in valid_ids:
            active_profile_id = profiles[0].id
    else:
        active_profile_id = ""

    return AzureSettingsStore(active_profile_id=active_profile_id, profiles=profiles)


def load_azure_settings_store() -> AzureSettingsStore:
    if not AZURE_CONFIG_PATH.exists():
        return AzureSettingsStore()

    try:
        with AZURE_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=500,
            detail="Azure settings file is invalid.",
        ) from error

    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Azure settings file is invalid.")

    return _normalize_azure_store(payload)


def save_azure_settings_store(store: AzureSettingsStore) -> AzureSettingsStore:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_azure_store(store.model_dump())
    with AZURE_CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(normalized.model_dump(), handle, indent=2)
        handle.write("\n")
    return normalized


def load_azure_settings() -> AzureSettings:
    """Compatibility helper: active profile flattened to legacy fields."""
    store = load_azure_settings_store()
    active = next(
        (profile for profile in store.profiles if profile.id == store.active_profile_id),
        None,
    )
    if active is None and store.profiles:
        active = store.profiles[0]
    if active is None:
        return AzureSettings()
    return AzureSettings(
        account_name=active.account_name,
        container_name=active.container_name,
        blob_name=active.blob_name,
    )


def save_azure_settings(payload: AzureUploadRequest) -> AzureSettings:
    """Compatibility helper for legacy single-target saves."""
    store = load_azure_settings_store()
    active = next(
        (profile for profile in store.profiles if profile.id == store.active_profile_id),
        None,
    )
    if active is None:
        profile_id = _new_azure_profile_id()
        store.profiles.append(
            AzureProfile(
                id=profile_id,
                name="Default",
                account_name=payload.account_name,
                container_name=payload.container_name,
                blob_name=payload.blob_name,
            )
        )
        store.active_profile_id = profile_id
    else:
        active.account_name = payload.account_name
        active.container_name = payload.container_name
        active.blob_name = payload.blob_name
    save_azure_settings_store(store)
    return AzureSettings(
        account_name=payload.account_name,
        container_name=payload.container_name,
        blob_name=payload.blob_name,
    )


def require_azure_settings() -> AzureUploadRequest:
    settings = load_azure_settings()
    if not settings.account_name or not settings.container_name or not settings.blob_name:
        raise HTTPException(
            status_code=400,
            detail="Azure settings are not configured. Save a profile first.",
        )

    return AzureUploadRequest(
        account_name=settings.account_name,
        container_name=settings.container_name,
        blob_name=settings.blob_name,
    )


def resolve_azure_profile(profile_id: str = "") -> AzureUploadRequest:
    """Like require_azure_settings, but can target a non-active profile by id."""
    if not profile_id:
        return require_azure_settings()
    store = load_azure_settings_store()
    profile = next((item for item in store.profiles if item.id == profile_id), None)
    if profile is None or not profile.account_name or not profile.container_name or not profile.blob_name:
        raise HTTPException(status_code=400, detail="That Azure profile is not configured.")
    return AzureUploadRequest(
        account_name=profile.account_name,
        container_name=profile.container_name,
        blob_name=profile.blob_name,
    )


def _new_aws_profile_id() -> str:
    return uuid.uuid4().hex


def _normalize_aws_store(payload: dict[str, object] | None) -> AwsSettingsStore:
    if not isinstance(payload, dict):
        return AwsSettingsStore()

    raw_profiles = payload.get("profiles")
    profiles: list[AwsProfile] = []
    seen_ids: set[str] = set()
    if isinstance(raw_profiles, list):
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id") or "").strip() or _new_aws_profile_id()
            if profile_id in seen_ids:
                profile_id = _new_aws_profile_id()
            seen_ids.add(profile_id)
            name = str(item.get("name") or "").strip() or "Untitled"
            profiles.append(
                AwsProfile(
                    id=profile_id,
                    name=name,
                    bucket=str(item.get("bucket") or "").strip(),
                    region=str(item.get("region") or "").strip(),
                    key=str(item.get("key") or "").strip(),
                )
            )

    active_profile_id = str(payload.get("active_profile_id") or "").strip()
    if profiles:
        valid_ids = {profile.id for profile in profiles}
        if active_profile_id not in valid_ids:
            active_profile_id = profiles[0].id
    else:
        active_profile_id = ""

    return AwsSettingsStore(active_profile_id=active_profile_id, profiles=profiles)


def load_aws_settings_store() -> AwsSettingsStore:
    if not AWS_CONFIG_PATH.exists():
        return AwsSettingsStore()
    try:
        with AWS_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail="AWS settings file is invalid.") from error
    return _normalize_aws_store(payload)


def save_aws_settings_store(store: AwsSettingsStore) -> AwsSettingsStore:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_aws_store(store.model_dump())
    with AWS_CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(normalized.model_dump(), handle, indent=2)
        handle.write("\n")
    return normalized


def resolve_aws_profile(profile_id: str = "") -> AwsProfile:
    store = load_aws_settings_store()
    target_id = profile_id.strip() or store.active_profile_id
    profile = next((item for item in store.profiles if item.id == target_id), None)
    if profile is None and store.profiles:
        profile = store.profiles[0]
    if profile is None or not profile.bucket or not profile.key:
        raise HTTPException(
            status_code=400,
            detail="AWS settings are not configured. Save a profile first.",
        )
    return profile


def load_app_settings() -> AppSettings:
    if not APP_SETTINGS_PATH.exists():
        return AppSettings()

    try:
        with APP_SETTINGS_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return AppSettings()

    if not isinstance(payload, dict):
        return AppSettings()

    stored_prompt = payload.get("ai_match_prompt", "")
    if stored_prompt is None:
        stored_prompt = ""
    cleaned_prompt = strip_ai_match_response_format(stored_prompt)
    # Treat an exact copy of the built-in default as "use default".
    if cleaned_prompt == DEFAULT_AI_MATCH_PROMPT.strip():
        cleaned_prompt = ""

    return AppSettings(
        ai_enabled=bool(payload.get("ai_enabled", False)),
        ai_provider=normalize_ai_provider(payload.get("ai_provider", "openai")),
        ai_match_prompt=cleaned_prompt,
        ai_confidence_threshold=payload.get("ai_confidence_threshold", 85),
        ai_models=payload.get("ai_models", {}),
    )


def save_app_settings(settings: AppSettings) -> AppSettings:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with APP_SETTINGS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(settings.model_dump(), handle, indent=2)
        handle.write("\n")
    return settings


def load_ai_model_choices() -> dict[str, list[str]]:
    """The selectable model catalog per provider, editable at
    _config/ai_model_choices.json without touching code. Falls back to the
    built-in defaults if the file is missing or malformed, and always keeps
    those defaults present so a bad edit can't delete the last known model."""
    stored: dict[str, list[str]] = {}
    if AI_MODEL_CHOICES_PATH.exists():
        try:
            with AI_MODEL_CHOICES_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                stored = payload
        except (OSError, json.JSONDecodeError):
            stored = {}

    result: dict[str, list[str]] = {}
    for provider, defaults in AI_MODEL_CHOICES.items():
        raw = stored.get(provider)
        cleaned = [str(v).strip() for v in raw if str(v or "").strip()] if isinstance(raw, list) else []
        result[provider] = list(dict.fromkeys(cleaned + defaults))
    return result


def save_ai_model_choices(choices: dict[str, list[str]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with AI_MODEL_CHOICES_PATH.open("w", encoding="utf-8") as handle:
        json.dump(choices, handle, indent=2)
        handle.write("\n")


def remember_custom_ai_model(provider: str, model: str) -> None:
    """Adds a model an admin typed in Settings to the persistent catalog so
    it shows up as a normal option next time, instead of vanishing once the
    session that typed it ends."""
    selected = normalize_ai_provider(provider)
    model = str(model or "").strip()
    if not model:
        return
    choices = load_ai_model_choices()
    if model in choices.get(selected, []):
        return
    choices.setdefault(selected, []).append(model)
    save_ai_model_choices(choices)


def app_settings_response() -> AppSettingsResponse:
    settings = load_app_settings()
    openai_available = openai_api_key_configured()
    gemini_available = gemini_api_key_configured()
    openrouter_available = openrouter_api_key_configured()
    effective_models = {
        "openai": openai_model_name(settings.ai_models.get("openai")),
        "gemini": gemini_model_name(settings.ai_models.get("gemini")),
        "openrouter": openrouter_model_name(settings.ai_models.get("openrouter")),
    }
    return AppSettingsResponse(
        ai_enabled=settings.ai_enabled,
        ai_provider=settings.ai_provider,
        ai_available=selected_ai_provider_available(settings),
        openai_available=openai_available,
        gemini_available=gemini_available,
        openrouter_available=openrouter_available,
        ai_match_prompt=settings.ai_match_prompt,
        default_ai_match_prompt=DEFAULT_AI_MATCH_PROMPT,
        ai_confidence_threshold=settings.ai_confidence_threshold,
        ai_models=effective_models,
        ai_model_choices=load_ai_model_choices(),
        default_ai_models=DEFAULT_AI_MODELS,
    )


def sse_event(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def parse_azure_progress_line(line: str) -> float | None:
    match = AZURE_PROGRESS_RE.search(line)
    if not match:
        return None
    return float(match.group(1))


def _stream_az_upload_to_queue(
    command: list[str],
    cwd: str,
    output_queue: asyncio.Queue[str | None],
    loop: asyncio.AbstractEventLoop,
    process_holder: list[subprocess.Popen[str] | None],
) -> None:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            text=True,
            bufsize=1,
        )
        process_holder[0] = process
        assert process.stdout is not None

        for line in process.stdout:
            loop.call_soon_threadsafe(output_queue.put_nowait, line.rstrip())

        return_code = process.wait()
        loop.call_soon_threadsafe(output_queue.put_nowait, f"__RETURN_CODE__:{return_code}")
    except Exception as exc:
        loop.call_soon_threadsafe(output_queue.put_nowait, f"__ERROR__:{exc}")
    finally:
        loop.call_soon_threadsafe(output_queue.put_nowait, None)


@contextmanager
def _resolve_data_csv_for_upload() -> Iterator[Path | None]:
    """Path to the CSV that Deploy should actually upload. File mode: the
    real DATA_PATH, untouched. DB mode: there's no local file that's the
    source of truth, so export current Data into a throwaway temp file for
    the CLI upload to read, cleaned up afterward either way. Yields None if
    there's nothing to upload."""
    if not _USE_DB:
        yield DATA_PATH if DATA_PATH.exists() else None
        return

    rows = [LookupRow(**row) for row in lookup_db.db_load_rows("data")]
    if not rows:
        yield None
        return
    with NamedTemporaryFile("w", newline="", encoding="utf-8", delete=False, suffix=".csv") as handle:
        temp_path = Path(handle.name)
    try:
        _write_rows_csv(rows, temp_path)
        yield temp_path
    finally:
        temp_path.unlink(missing_ok=True)


async def azure_upload_events(payload: AzureUploadRequest) -> AsyncIterator[str]:
    with _resolve_data_csv_for_upload() as data_csv_path:
        if data_csv_path is None:
            yield sse_event({"type": "error", "message": "Data lookup CSV not found at _data/eol_lookup.csv."})
            return
        async for event in _azure_upload_events(payload, data_csv_path):
            yield event


async def _azure_upload_events(payload: AzureUploadRequest, data_csv_path: Path) -> AsyncIterator[str]:
    az_path = shutil.which("az")
    if not az_path:
        yield sse_event(
            {
                "type": "error",
                "message": "Azure CLI (az) is not installed or not available on PATH.",
            }
        )
        return

    command = [
        az_path,
        "storage",
        "blob",
        "upload",
        "--account-name",
        payload.account_name,
        "--container-name",
        payload.container_name,
        "--file",
        str(data_csv_path),
        "--name",
        payload.blob_name,
        "--overwrite",
        "--auth-mode",
        "login",
    ]

    yield sse_event(
        {
            "type": "start",
            "message": (
                f"Uploading the validated Data lookup to "
                f"{payload.account_name}/{payload.container_name}/{payload.blob_name}"
            ),
        }
    )

    output_queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    process_holder: list[subprocess.Popen[str] | None] = [None]
    worker = threading.Thread(
        target=_stream_az_upload_to_queue,
        args=(command, str(BASE_DIR), output_queue, loop, process_holder),
        daemon=True,
    )
    worker.start()

    result_lines: list[str] = []
    return_code: int | None = None

    try:
        while True:
            item = await output_queue.get()
            if item is None:
                break

            if item.startswith("__RETURN_CODE__:"):
                return_code = int(item.split(":", 1)[1])
                continue

            if item.startswith("__ERROR__:"):
                yield sse_event(
                    {
                        "type": "error",
                        "message": item.split(":", 1)[1],
                        "output": result_lines,
                    }
                )
                return

            if not item:
                continue

            result_lines.append(item)
            yield sse_event(
                {
                    "type": "progress",
                    "message": item,
                    "percent": parse_azure_progress_line(item),
                }
            )

        if return_code == 0:
            yield sse_event(
                {
                    "type": "complete",
                    "message": "Azure upload completed successfully.",
                    "output": result_lines,
                }
            )
            return

        yield sse_event(
            {
                "type": "error",
                "message": (
                    f"Azure upload failed with exit code {return_code}."
                    if return_code is not None
                    else "Azure upload failed."
                ),
                "output": result_lines,
            }
        )
    finally:
        process = process_holder[0]
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        worker.join(timeout=1)


async def aws_upload_events(profile: AwsProfile) -> AsyncIterator[str]:
    with _resolve_data_csv_for_upload() as data_csv_path:
        if data_csv_path is None:
            yield sse_event({"type": "error", "message": "Data lookup CSV not found at _data/eol_lookup.csv."})
            return
        async for event in _aws_upload_events(profile, data_csv_path):
            yield event


async def _aws_upload_events(profile: AwsProfile, data_csv_path: Path) -> AsyncIterator[str]:
    aws_path = shutil.which("aws")
    if not aws_path:
        yield sse_event(
            {"type": "error", "message": "AWS CLI (aws) is not installed or not available on PATH."}
        )
        return

    destination = f"s3://{profile.bucket}/{profile.key}"
    command = [aws_path, "s3", "cp", str(data_csv_path), destination]
    if profile.region:
        command.extend(["--region", profile.region])

    yield sse_event({"type": "start", "message": f"Uploading the validated Data lookup to {destination}"})

    output_queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    process_holder: list[subprocess.Popen[str] | None] = [None]
    worker = threading.Thread(
        target=_stream_az_upload_to_queue,
        args=(command, str(BASE_DIR), output_queue, loop, process_holder),
        daemon=True,
    )
    worker.start()

    result_lines: list[str] = []
    return_code: int | None = None
    try:
        while True:
            item = await output_queue.get()
            if item is None:
                break
            if item.startswith("__RETURN_CODE__:"):
                return_code = int(item.split(":", 1)[1])
                continue
            if item.startswith("__ERROR__:"):
                yield sse_event({"type": "error", "message": item.split(":", 1)[1], "output": result_lines})
                return
            if not item:
                continue
            result_lines.append(item)
            yield sse_event({"type": "progress", "message": item, "percent": parse_azure_progress_line(item)})

        if return_code == 0:
            yield sse_event({"type": "complete", "message": "AWS S3 upload completed successfully.", "output": result_lines})
            return
        yield sse_event(
            {
                "type": "error",
                "message": (
                    f"AWS S3 upload failed with exit code {return_code}." if return_code is not None else "AWS S3 upload failed."
                ),
                "output": result_lines,
            }
        )
    finally:
        process = process_holder[0]
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        worker.join(timeout=1)


LOOKUP_REFRESH_CHUNK_SIZE = 25


def _apply_lifecycle_result(row: dict, result: dict, evidence_by_os: dict) -> None:
    os_key = str(row.get("os_string") or "").strip()
    row["eol_date"] = str(result.get("eol_date") or "")
    row["eol_status"] = str(result.get("eol_status") or "")
    row["eoas_date"] = str(result.get("eoas_date") or "")
    row["eoas_status"] = str(result.get("eoas_status") or "")

    # Adopt whenever this lookup actually produced a name -- not just when the
    # row's field was previously blank. A confirmed release match names one
    # specific release (e.g. "23H2 (E)") together with its dates; refusing to
    # correct an already-non-blank name left eol_date/eoas_date matching a
    # *different*, newly-resolved release than whatever release-level tag the
    # row still displayed, silently, on every future refresh, with no way to
    # self-correct short of deleting the row's normalized fields by hand. A
    # lookup with nothing to report leaves these blank in `result`, so a
    # genuine no-match still leaves the row's existing values untouched.
    filled_detailed = bool(result.get("normalized_os_detailed_name"))
    filled_normalized = bool(result.get("normalized_os"))
    if filled_detailed:
        row["normalized_os_detailed_name"] = collapse_consecutive_duplicate_words(result.get("normalized_os_detailed_name"))
    if filled_normalized:
        row["normalized_os"] = collapse_consecutive_duplicate_words(result.get("normalized_os"))

    if not os_key:
        return
    entry = evidence_by_os.setdefault(os_key, {})
    entry["eol"] = build_eol_evidence_slot(result)
    if filled_detailed:
        entry["detailed"] = dict(entry["eol"], method=entry["eol"]["method"])
    if filled_normalized:
        entry["normalized"] = dict(entry["eol"], method=entry["eol"]["method"])


def _row_has_lifecycle_data(row: dict) -> bool:
    return bool(str(row.get("eol_date") or "").strip() or str(row.get("eoas_date") or "").strip())


def _attach_matched_by(rows: list[dict], evidence_by_os: dict) -> None:
    """Stamp row["matched_by"] the same way GET /api/lookup does, so a row
    returned from a Refresh/Add-OS run is self-consistent with its own
    evidence immediately -- without this, the client keeps whatever
    matched_by a row had from the last full /api/lookup fetch (or none at
    all for a brand-new row), which goes stale the moment its evidence
    actually changes. That staleness is what made the "Matched by" column
    filter look broken specifically on rows touched during the current
    Draft session."""
    for row in rows:
        os_key = str(row.get("os_string") or "").strip()
        row["matched_by"] = row_matched_by(evidence_by_os.get(os_key), row)


def refresh_rows_lifecycle_chunk(
    rows: list[dict],
    evidence_by_os: dict,
    product_cache: dict[str, dict[str, object]] | None = None,
) -> None:
    """Synchronous worker: one chunk through endoflife.date, then the vendor
    cascade for whatever is still unresolved. Meant to run inside
    asyncio.to_thread per chunk so the caller can yield progress between
    chunks.

    ``product_cache`` should be one dict shared across every chunk of the
    same Refresh run (see lookup_refresh_events/lookup_rows_refresh_events)
    so a product fetched by an earlier chunk is never re-fetched from
    endoflife.date by a later one -- previously this defaulted fresh per
    chunk, so a common slug like "windows" could be re-requested from the
    network once per chunk that contained a matching row instead of once
    for the whole refresh, which is what made large refreshes so slow.

    Ambiguous OS rows are skipped entirely -- not even queried. Querying a
    lifecycle source with the literal text "Ambiguous OS" doesn't fail
    cleanly: it can fall back to the raw (also ambiguous) os_string and pick
    up a real but unrelated product via coincidental version-number overlap,
    silently writing a wrong date onto a row that was flagged specifically
    because we can't tell which product it is.
    """
    eligible_rows = [row for row in rows if not is_ambiguous_row(row)]
    if not eligible_rows:
        return

    eol_items = [
        {
            "os_string": row.get("os_string", ""),
            "normalized_os_detailed_name": row.get("normalized_os_detailed_name", ""),
            "normalized_os": row.get("normalized_os", ""),
        }
        for row in eligible_rows
    ]
    eol_results = lookup_os_eol_batch(eol_items, product_cache=product_cache)
    still_unresolved: list[dict] = []
    for row, result in zip(eligible_rows, eol_results):
        _apply_lifecycle_result(row, result, evidence_by_os)
        if not _row_has_lifecycle_data(row):
            still_unresolved.append(row)

    if not still_unresolved:
        return

    vendor_items = [
        {
            "os_string": row.get("os_string", ""),
            "normalized_os_detailed_name": row.get("normalized_os_detailed_name", ""),
            "normalized_os": row.get("normalized_os", ""),
        }
        for row in still_unresolved
    ]
    vendor_results = lookup_vendor_batch(vendor_items)
    for row, result in zip(still_unresolved, vendor_results):
        if _row_has_lifecycle_data({"eol_date": result.get("eol_date"), "eoas_date": result.get("eoas_date")}):
            _apply_lifecycle_result(row, result, evidence_by_os)


async def lookup_refresh_events(
    rows: list[dict],
    evidence: dict[str, object],
    source: str,
    cancel_event: threading.Event,
) -> AsyncIterator[str]:
    total = len(rows)
    evidence_by_os = dict((normalize_evidence_payload(evidence).get("by_os") or {}))
    processed = 0
    # Shared across every chunk of this run -- see refresh_rows_lifecycle_chunk.
    product_cache: dict[str, dict[str, object]] = {}

    for start in range(0, total, LOOKUP_REFRESH_CHUNK_SIZE):
        if cancel_event.is_set():
            yield sse_event({"type": "cancelled", "processed": processed, "total": total})
            return
        chunk = rows[start : start + LOOKUP_REFRESH_CHUNK_SIZE]
        await asyncio.to_thread(refresh_rows_lifecycle_chunk, chunk, evidence_by_os, product_cache)
        processed += len(chunk)
        yield sse_event(
            {
                "type": "progress",
                "stage": "Refreshing EOL/EOAS",
                "processed": processed,
                "total": total,
            }
        )

    lookup_rows = [LookupRow(**row) for row in rows]
    save_rows(lookup_rows, source)
    saved_evidence = save_evidence(
        prune_evidence_to_rows({"by_os": evidence_by_os, "updated_at": ""}, lookup_rows), source
    )
    _attach_matched_by(rows, evidence_by_os)
    yield sse_event(
        {
            "type": "complete",
            "rows": rows,
            "evidence": saved_evidence,
            "source": source,
        }
    )


async def lookup_rows_refresh_events(rows: list[dict]) -> AsyncIterator[str]:
    """Chunked-progress variant of POST /api/lookup/rows/refresh -- same
    lifecycle lookup, streamed so a large batch (e.g. the Add-OS pipeline's
    final step) reports real per-chunk progress instead of one long silent
    wait. Unlike lookup_refresh_events this never writes to Data/Draft --
    the caller owns merging these rows into whatever rowset it's building."""
    total = len(rows)
    evidence_by_os: dict[str, object] = {}
    processed = 0
    # Shared across every chunk of this run -- see refresh_rows_lifecycle_chunk.
    product_cache: dict[str, dict[str, object]] = {}

    for start in range(0, total, LOOKUP_REFRESH_CHUNK_SIZE):
        chunk = rows[start : start + LOOKUP_REFRESH_CHUNK_SIZE]
        await asyncio.to_thread(refresh_rows_lifecycle_chunk, chunk, evidence_by_os, product_cache)
        processed += len(chunk)
        yield sse_event(
            {
                "type": "progress",
                "stage": "Looking up EOL/EOAS",
                "processed": processed,
                "total": total,
            }
        )

    _attach_matched_by(rows, evidence_by_os)
    yield sse_event({"type": "complete", "rows": rows, "evidence_by_os": evidence_by_os})


async def vendor_lookup_sync_events(
    source_id: str,
    options: dict[str, object] | None,
) -> AsyncIterator[str]:
    """Stream scrape progress so the UI can show N of M processed, not just a spinner."""
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    ACTIVE_VENDOR_SYNC_JOBS[job_id] = cancel_event

    output_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    result_holder: dict[str, object] = {}
    error_holder: list[str] = []

    def progress_callback(stage: str, processed: int, total: int) -> None:
        loop.call_soon_threadsafe(
            output_queue.put_nowait,
            {"type": "progress", "stage": stage, "processed": processed, "total": total},
        )

    def run_sync() -> None:
        try:
            result_holder["result"] = vendor_sync_source(
                source_id,
                progress_callback=progress_callback,
                options=options,
                cancel_event=cancel_event,
            )
        except Exception as exc:  # noqa: BLE001 - surface scrape failures to UI
            error_holder.append(str(exc))
        finally:
            loop.call_soon_threadsafe(output_queue.put_nowait, None)

    try:
        yield sse_event({"type": "started", "job_id": job_id})

        async with VENDOR_SYNC_LOCK:
            worker = threading.Thread(target=run_sync, daemon=True)
            worker.start()
            try:
                while True:
                    item = await output_queue.get()
                    if item is None:
                        break
                    yield sse_event(item)
            finally:
                worker.join(timeout=1)

        if error_holder:
            yield sse_event(
                {
                    "type": "error",
                    "message": f"Failed to update {source_id} database: {error_holder[0]}",
                }
            )
            return

        status = await asyncio.to_thread(vendor_get_status, source_id)
        result = result_holder.get("result", {})
        event_type = "cancelled" if isinstance(result, dict) and result.get("cancelled") else "complete"
        yield sse_event({"type": event_type, "result": result, "status": status})
    finally:
        ACTIVE_VENDOR_SYNC_JOBS.pop(job_id, None)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"headers": CSV_HEADERS},
    )


def _published_at() -> str:
    if _USE_DB:
        return lookup_db.db_published_at()
    if not DATA_PATH.exists():
        return ""
    return datetime.fromtimestamp(DATA_PATH.stat().st_mtime).isoformat(timespec="seconds")


@app.get("/api/lookup")
async def get_lookup(source: str = "data") -> dict[str, object]:
    rows = load_rows(source)
    evidence = load_evidence(source)
    by_os = evidence.get("by_os") if isinstance(evidence.get("by_os"), dict) else {}
    for row in rows:
        row["matched_by"] = row_matched_by(by_os.get(str(row.get("os_string") or "").strip()), row)
    result: dict[str, object] = {
        "headers": CSV_HEADERS,
        "rows": rows,
        "source": source,
        "evidence": evidence,
        "published_at": _published_at(),
        "draft_exists": _draft_exists(),
        "data_revision": read_data_revision(),
        "storage_mode": "postgres" if _USE_DB else "file",
    }
    if _USE_DB:
        # host:port/dbname only -- never the password -- so the UI can show
        # which Postgres this instance is actually talking to.
        result["storage_target"] = lookup_db.describe_target()
    if source.strip().lower() == "draft" and _draft_exists():
        result["based_on_revision"] = read_draft_based_on_revision()
    return result


@app.get("/api/lookup/evidence")
async def get_lookup_row_evidence(os_string: str, source: str = "data") -> dict[str, object]:
    evidence = load_evidence(source)
    by_os = evidence.get("by_os") if isinstance(evidence.get("by_os"), dict) else {}
    rows = load_rows(source)
    row = next((item for item in rows if item.get("os_string") == os_string), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found for that os_string.")
    return build_evidence_entries(by_os.get(os_string.strip()), row)


@app.get("/api/lookup/diff")
async def get_lookup_diff(source: str = "draft") -> dict[str, object]:
    if not _source_exists(source):
        return {"added": [], "edited": [], "deleted": [], "unresolved": 0, "added_count": 0, "edited_count": 0, "deleted_count": 0}
    data_rows = load_rows("data") if _data_exists() else []
    draft_rows = load_rows(source)
    return compute_lookup_diff(data_rows, draft_rows)


@app.post("/api/lookup")
async def update_lookup(payload: LookupPayload, source: str = "draft") -> dict[str, object]:
    # Capture the merge base whenever the client says this save represents a
    # fresh fork from Data -- either a brand-new draft (first save, no draft
    # file yet) or an explicit reset (Revert all changes re-syncing draft to
    # current Data). base_rows/base_evidence are exactly what the client's
    # own GET /api/lookup?source=data returned moments earlier, so there's
    # no server-side re-derivation and no race against Data having moved
    # between when the browser fetched it and when this request arrives.
    # File-mode only -- DB mode's db_save_rows stamps draft_based_on_revision
    # itself (just a revision number, not a full row snapshot; no per-row
    # merge base needed since DB mode's publish only guards on revision).
    if not _USE_DB and source.strip().lower() == "draft" and payload.base_rows is not None and (
        payload.reset_base or not DRAFT_PATH.exists()
    ):
        save_base_rows(payload.base_rows, read_data_revision())
        save_base_evidence(payload.base_evidence or empty_evidence_payload())

    save_rows(payload.rows, source)
    evidence = save_evidence(prune_evidence_to_rows(payload.evidence, payload.rows), source)
    return {
        "saved": True,
        "row_count": len(payload.rows),
        "source": source,
        "evidence": evidence,
    }


def _apply_conflict_resolution(conflict: dict, resolution: str) -> tuple[list[dict], dict[str, dict]]:
    """Applies a 'mine'/'theirs' choice to one conflict. Returns
    (rows_to_write, evidence_entries_to_write)."""
    side = conflict.get("mine") if resolution == "mine" else conflict.get("theirs")
    if not side:
        return [], {}
    if conflict.get("kind") == "ambiguous_duplicate":
        rows = side.get("rows") or []
        evidence = side.get("evidence") or {}
        entries = {row.get("os_string", ""): evidence for row in rows if evidence}
        return rows, entries
    row = side.get("row")
    if row is None:
        return [], {}
    evidence = side.get("evidence") or {}
    entries = {row.get("os_string", ""): evidence} if evidence else {}
    return [row], entries


def resolve_publish_rows(payload: LookupPayload) -> dict[str, object]:
    """Runs the file-mode 3-way merge for a publish attempt.

    Returns {"ok": True, "rows": [...], "evidence": {...}} ready to write,
    or {"ok": False, "conflicts": [...]} listing what's still unresolved --
    callers must write nothing in that case.
    """
    ensure_draft_base()
    base_rows = load_base_rows()
    base_evidence = load_base_evidence()
    current_rows = load_rows("data") if DATA_PATH.exists() else []
    current_evidence = load_evidence("data")
    draft_rows = [row.model_dump() for row in payload.rows]
    draft_evidence = normalize_evidence_payload(payload.evidence)

    merge_result = merge_lookup_rows(
        base_rows, current_rows, draft_rows,
        base_evidence, current_evidence, draft_evidence,
    )

    merged_rows = list(merge_result["merged_rows"])
    merged_by_os = dict(merge_result["merged_evidence"]["by_os"])
    unresolved: list[dict] = []
    for conflict in merge_result["conflicts"]:
        resolution = payload.conflict_resolutions.get(conflict["os_string"])
        if resolution not in ("mine", "theirs"):
            unresolved.append(conflict)
            continue
        rows_to_add, entries = _apply_conflict_resolution(conflict, resolution)
        merged_rows.extend(rows_to_add)
        merged_by_os.update(entries)

    if unresolved:
        return {"ok": False, "conflicts": unresolved}

    return {"ok": True, "rows": merged_rows, "evidence": {"by_os": merged_by_os, "updated_at": ""}}


def check_publish_conflicts(payload: LookupPayload) -> dict[str, object]:
    """No-write preview of a publish. File mode runs the real 3-way merge
    and returns per-row conflicts; DB mode has no per-row merge to run (a
    shared DB is already one source of truth -- see the plan's "lightweight
    guard only" design for production) -- it just reports whether Data has
    moved since the draft's expected revision, via `stale`."""
    if _USE_DB:
        expected = lookup_db.db_draft_based_on_revision()
        current = lookup_db.db_data_revision()
        return {"conflicts": [], "stale": current != expected}
    result = resolve_publish_rows(payload)
    return {"conflicts": [] if result["ok"] else result["conflicts"], "stale": False}


def _mirror_publish_to_files(
    rows: list[LookupRow],
    evidence: dict[str, object],
    revision: int,
    suffix: str,
) -> tuple[Path | None, Path | None]:
    """DB-mode-only, opt-in (``LOOKUP_DB_MIRROR_FILES``): write the just-published
    Data out to _data/ too, so the files stay git-trackable alongside Postgres
    as the actual source of truth. Backs up the previous file contents first,
    same as the file-mode publish path does."""
    backup_path = backup_data_file(suffix)
    evidence_backup_path = backup_data_evidence(suffix)
    _write_rows_csv(rows, DATA_PATH)
    _save_evidence_file(evidence, "data")
    DATA_REVISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_REVISION_PATH.write_text(str(revision), encoding="utf-8")
    return backup_path, evidence_backup_path


def perform_publish(payload: LookupPayload) -> dict[str, object]:
    """Executes a publish. Raises HTTPException(409) if it can't proceed --
    unresolved file-mode conflicts, or a stale DB-mode revision."""
    suffix = sanitize_backup_suffix(payload.backup_suffix)

    if _USE_DB:
        expected_revision = lookup_db.db_draft_based_on_revision()
        try:
            db_result = lookup_db.db_publish(
                [row.model_dump() for row in payload.rows],
                normalize_evidence_payload(payload.evidence),
                expected_revision,
                backup_suffix=suffix,
            )
        except lookup_db.PublishConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        backup_path = evidence_backup_path = None
        if _MIRROR_FILES:
            backup_path, evidence_backup_path = _mirror_publish_to_files(
                payload.rows, db_result["evidence"], db_result["data_revision"], suffix
            )
        return {
            "validated": True,
            "row_count": db_result["row_count"],
            "source": "data",
            "draft_deleted": True,
            "backup_suffix": suffix,
            # DB-mode backups are rows in the `backups` table, not files --
            # unless file mirroring is on, in which case these are real paths.
            "backup_path": str(backup_path) if backup_path else "",
            "evidence_backup_path": str(evidence_backup_path) if evidence_backup_path else "",
            "evidence": db_result["evidence"],
            "data_revision": db_result["data_revision"],
        }

    result = resolve_publish_rows(payload)
    if not result["ok"]:
        raise HTTPException(
            status_code=409,
            detail=f"{len(result['conflicts'])} row(s) changed both here and in Data since you last checked. "
            "Re-open Validate & Publish to resolve them.",
        )
    resolved_rows = [LookupRow(**row) for row in result["rows"]]
    backup_path = backup_data_file(suffix)
    evidence_backup_path = backup_data_evidence(suffix)
    save_rows(resolved_rows, "data")
    evidence = save_evidence(prune_evidence_to_rows(result["evidence"], resolved_rows), "data")
    # Promote replaces Draft: remove working copy after writing Data.
    if DRAFT_PATH.exists():
        DRAFT_PATH.unlink()
    delete_evidence("draft")
    delete_draft_base()
    new_revision = bump_data_revision()
    return {
        "validated": True,
        "row_count": len(resolved_rows),
        "source": "data",
        "draft_deleted": True,
        "backup_suffix": suffix,
        "backup_path": str(backup_path) if backup_path else "",
        "evidence_backup_path": str(evidence_backup_path) if evidence_backup_path else "",
        "evidence": evidence,
        "data_revision": new_revision,
    }


@app.post("/api/lookup/validate/check")
async def validate_lookup_check(payload: LookupPayload) -> dict[str, object]:
    """No-write preview of a publish: runs the same checks validate_lookup*
    will run and returns whatever it finds, so the UI can resolve conflicts
    (file mode) or warn about staleness (DB mode) up front instead of
    discovering it mid-publish."""
    return check_publish_conflicts(payload)


@app.post("/api/lookup/validate")
async def validate_lookup(payload: LookupPayload) -> dict[str, object]:
    return perform_publish(payload)


@app.post("/api/lookup/row/refresh")
async def refresh_lookup_row(payload: RowRefreshRequest) -> dict[str, object]:
    row = payload.row.model_dump()
    evidence_by_os: dict[str, object] = {}
    await asyncio.to_thread(refresh_rows_lifecycle_chunk, [row], evidence_by_os)
    os_key = str(row.get("os_string") or "").strip()
    _attach_matched_by([row], evidence_by_os)
    return {"row": row, "evidence_entry": evidence_by_os.get(os_key, {})}


@app.post("/api/lookup/rows/refresh")
async def refresh_lookup_rows(payload: RowsRefreshRequest) -> dict[str, object]:
    rows = [item.model_dump() for item in payload.rows]
    evidence_by_os: dict[str, object] = {}
    product_cache: dict[str, dict[str, object]] = {}
    for start in range(0, len(rows), LOOKUP_REFRESH_CHUNK_SIZE):
        chunk = rows[start : start + LOOKUP_REFRESH_CHUNK_SIZE]
        await asyncio.to_thread(refresh_rows_lifecycle_chunk, chunk, evidence_by_os, product_cache)
    _attach_matched_by(rows, evidence_by_os)
    return {"rows": rows, "evidence_by_os": evidence_by_os}


@app.post("/api/lookup/rows/refresh/stream")
async def refresh_lookup_rows_stream(payload: RowsRefreshRequest) -> StreamingResponse:
    """Streamed, non-persisting variant of the endpoint above -- large
    batches (Add-OS pipeline, bulk selection refresh) get real per-chunk
    progress instead of one long silent wait with no feedback."""
    rows = [item.model_dump() for item in payload.rows]
    return StreamingResponse(
        lookup_rows_refresh_events(rows),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/lookup/refresh/stream")
async def refresh_lookup_stream(payload: LookupRefreshStreamRequest) -> StreamingResponse:
    if LOOKUP_REFRESH_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="A lookup refresh is already running. Please wait for it to finish.",
        )

    async def events() -> AsyncIterator[str]:
        job_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        ACTIVE_LOOKUP_REFRESH_JOBS[job_id] = cancel_event
        try:
            yield sse_event({"type": "started", "job_id": job_id})
            async with LOOKUP_REFRESH_LOCK:
                rows = [row.model_dump() for row in payload.rows]
                async for event in lookup_refresh_events(
                    rows, payload.evidence, payload.source, cancel_event
                ):
                    yield event
        finally:
            ACTIVE_LOOKUP_REFRESH_JOBS.pop(job_id, None)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/lookup/refresh/{job_id}/cancel")
async def refresh_lookup_cancel(job_id: str) -> dict[str, object]:
    cancel_event = ACTIVE_LOOKUP_REFRESH_JOBS.get(job_id)
    if cancel_event is None:
        raise HTTPException(status_code=404, detail="Refresh job not found. It may have already finished.")
    cancel_event.set()
    return {"cancelling": True, "job_id": job_id}


@app.post("/api/lookup/validate/stream")
async def validate_lookup_stream(payload: LookupValidateStreamRequest) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        yield sse_event({"type": "started"})

        if _USE_DB:
            # One atomic transaction -- no meaningful sub-steps to report
            # progress on individually the way file mode's backup/write/
            # delete-draft sequence has.
            yield sse_event({"type": "progress", "stage": "Publishing to database", "processed": 0, "total": 1})
            try:
                db_publish_result = await asyncio.to_thread(perform_publish, payload)
            except HTTPException as exc:
                yield sse_event({"type": "error", "message": str(exc.detail)})
                return
            yield sse_event({"type": "complete", **db_publish_result})
            return

        yield sse_event({"type": "progress", "stage": "Checking for conflicts", "processed": 0, "total": 4})
        result = await asyncio.to_thread(resolve_publish_rows, payload)
        if not result["ok"]:
            yield sse_event({"type": "conflict", "conflicts": result["conflicts"]})
            return
        resolved_rows = [LookupRow(**row) for row in result["rows"]]

        yield sse_event({"type": "progress", "stage": "Backing up data/eol_lookup.csv", "processed": 1, "total": 4})
        suffix = sanitize_backup_suffix(payload.backup_suffix)
        backup_path = await asyncio.to_thread(backup_data_file, suffix)
        evidence_backup_path = await asyncio.to_thread(backup_data_evidence, suffix)

        yield sse_event({"type": "progress", "stage": "Writing merged rows to Data", "processed": 2, "total": 4})
        await asyncio.to_thread(save_rows, resolved_rows, "data")
        evidence = await asyncio.to_thread(
            save_evidence, prune_evidence_to_rows(result["evidence"], resolved_rows), "data"
        )

        yield sse_event({"type": "progress", "stage": "Deleting draft", "processed": 3, "total": 4})
        if DRAFT_PATH.exists():
            DRAFT_PATH.unlink()
        delete_evidence("draft")
        delete_draft_base()
        new_revision = await asyncio.to_thread(bump_data_revision)

        yield sse_event(
            {
                "type": "complete",
                "validated": True,
                "row_count": len(resolved_rows),
                "source": "data",
                "draft_deleted": True,
                "backup_suffix": suffix,
                "backup_path": str(backup_path) if backup_path else "",
                "evidence_backup_path": str(evidence_backup_path) if evidence_backup_path else "",
                "evidence": evidence,
                "data_revision": new_revision,
            }
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/lookup/download")
async def download_lookup(source: str = "data") -> Response:
    if not _source_exists(source):
        detail = "Draft lookup CSV not found." if source == "draft" else "Lookup CSV not found."
        raise HTTPException(status_code=404, detail=detail)

    if _USE_DB:
        # No local file is the source of truth in DB mode -- build the CSV
        # in memory from the DB rows instead of FileResponse-ing a path.
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for row in load_rows(source):
            writer.writerow(row)
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="eol_lookup.csv"'},
        )

    return FileResponse(
        path=lookup_path(source),
        media_type="text/csv",
        filename="eol_lookup.csv",
    )


@app.delete("/api/lookup/draft")
async def delete_draft_lookup() -> dict[str, object]:
    if not _draft_exists():
        raise HTTPException(status_code=404, detail="Draft lookup CSV not found.")

    if _USE_DB:
        delete_evidence("draft")  # -> lookup_db.db_delete_draft(): clears rows + evidence + base revision
    else:
        DRAFT_PATH.unlink()
        delete_evidence("draft")
        delete_draft_base()
    return {"deleted": True, "source": "draft"}


@app.get("/api/settings")
async def get_app_settings() -> AppSettingsResponse:
    return app_settings_response()


@app.put("/api/settings")
async def update_app_settings(payload: AppSettingsUpdateRequest) -> AppSettingsResponse:
    current = load_app_settings()
    if payload.ai_match_prompt is None:
        prompt = current.ai_match_prompt
    else:
        prompt = strip_ai_match_response_format(payload.ai_match_prompt)
        if prompt == DEFAULT_AI_MATCH_PROMPT.strip():
            prompt = ""
    threshold = current.ai_confidence_threshold if payload.ai_confidence_threshold is None else payload.ai_confidence_threshold
    if payload.ai_models is None:
        models = current.ai_models
    else:
        # Merge rather than replace so setting one provider's model doesn't drop the others.
        models = {**current.ai_models, **payload.ai_models}
        # A model typed in Settings becomes a normal catalog entry from now on,
        # not just a one-off override for this setting.
        for provider, model in payload.ai_models.items():
            remember_custom_ai_model(provider, model)
    merged = AppSettings(
        ai_enabled=payload.ai_enabled,
        ai_provider=normalize_ai_provider(payload.ai_provider or current.ai_provider),
        ai_match_prompt=prompt,
        ai_confidence_threshold=threshold,
        ai_models=models,
    )
    save_app_settings(merged)
    return app_settings_response()


@app.get("/api/azure/settings")
async def get_azure_settings() -> AzureSettingsStore:
    return load_azure_settings_store()


@app.put("/api/azure/settings")
async def update_azure_settings(payload: AzureSettingsSaveRequest) -> AzureSettingsStore:
    if not payload.profiles:
        raise HTTPException(
            status_code=400,
            detail="Add at least one Azure profile before saving.",
        )
    for profile in payload.profiles:
        if not profile.name:
            raise HTTPException(status_code=400, detail="Each Azure profile needs a name.")
        if not profile.account_name or not profile.container_name or not profile.blob_name:
            raise HTTPException(
                status_code=400,
                detail=f"Profile '{profile.name}' is incomplete. Fill account, container, and blob path.",
            )
        if profile.blob_name.startswith("/"):
            raise HTTPException(
                status_code=400,
                detail=f"Profile '{profile.name}': blob path must not start with /.",
            )
    return save_azure_settings_store(
        AzureSettingsStore(
            active_profile_id=payload.active_profile_id,
            profiles=payload.profiles,
        )
    )

@app.post("/api/azure/upload")
async def upload_lookup_to_azure(profile_id: str = "") -> StreamingResponse:
    payload = resolve_azure_profile(profile_id)
    return StreamingResponse(
        azure_upload_events(payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/aws/settings")
async def get_aws_settings() -> AwsSettingsStore:
    return load_aws_settings_store()


@app.put("/api/aws/settings")
async def update_aws_settings(payload: AwsSettingsSaveRequest) -> AwsSettingsStore:
    if not payload.profiles:
        raise HTTPException(status_code=400, detail="Add at least one AWS profile before saving.")
    for profile in payload.profiles:
        if not profile.name:
            raise HTTPException(status_code=400, detail="Each AWS profile needs a name.")
        if not profile.bucket or not profile.key:
            raise HTTPException(
                status_code=400,
                detail=f"Profile '{profile.name}' is incomplete. Fill bucket and key.",
            )
    return save_aws_settings_store(
        AwsSettingsStore(active_profile_id=payload.active_profile_id, profiles=payload.profiles)
    )


@app.post("/api/aws/upload")
async def upload_lookup_to_aws(profile_id: str = "") -> StreamingResponse:
    profile = resolve_aws_profile(profile_id)
    return StreamingResponse(
        aws_upload_events(profile),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/normalize-suggest")
async def normalize_suggest(payload: NormalizeSuggestRequest) -> dict[str, object]:
    if not payload.items:
        return {"results": [], "ai_skipped": False}

    settings = load_app_settings()
    if not settings.ai_enabled or not selected_ai_provider_available(settings):
        return {"results": [None for _ in payload.items], "ai_skipped": True}

    suggestions = await asyncio.to_thread(
        suggest_normalization_batch,
        [item.os_string for item in payload.items],
        [pair.model_dump() for pair in payload.allowed_pairs],
        payload.fuzzy_match_threshold,
        settings.ai_provider,
        settings.ai_match_prompt,
        settings.ai_models.get(settings.ai_provider),
    )

    results: list[NormalizeSuggestResult | None] = []
    for suggestion in suggestions:
        if suggestion is None:
            results.append(None)
            continue
        results.append(NormalizeSuggestResult(**suggestion))

    return {
        "results": results,
        "ai_skipped": False,
        "ai_provider": settings.ai_provider,
    }


@app.post("/api/ambiguous-os-detect")
async def ambiguous_os_detect(payload: AmbiguousOsDetectRequest) -> dict[str, object]:
    if not payload.items:
        return {"results": []}

    settings = load_app_settings()
    results = await asyncio.to_thread(
        detect_ambiguous_os_batch,
        [item.os_string for item in payload.items],
        settings.ai_provider,
        settings.ai_models.get(settings.ai_provider),
    )
    return {"results": results, "ai_provider": settings.ai_provider}


@app.post("/api/eol-lookup")
async def eol_lookup(payload: EolLookupBatchRequest) -> dict[str, object]:
    results = await asyncio.to_thread(
        lookup_os_eol_batch,
        [item.model_dump() for item in payload.items],
    )
    return {"results": results}


@app.get("/api/eosl/status")
async def eosl_status() -> dict[str, object]:
    return await asyncio.to_thread(eosl_get_status)


@app.get("/api/eosl/rows")
async def eosl_rows() -> dict[str, object]:
    rows = await asyncio.to_thread(eosl_list_all_rows)
    status = await asyncio.to_thread(eosl_get_status)
    return {"rows": rows, "status": status}


@app.post("/api/eosl/sync")
async def eosl_sync() -> dict[str, object]:
    if VENDOR_SYNC_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="A vendor lookup update is already running. Please wait for it to finish.",
        )
    async with VENDOR_SYNC_LOCK:
        try:
            result = await asyncio.to_thread(eosl_sync_os_database)
        except Exception as error:  # noqa: BLE001 - surface scrape failures to UI
            raise HTTPException(
                status_code=502,
                detail=f"Failed to update EOSL database: {error}",
            ) from error
    status = await asyncio.to_thread(eosl_get_status)
    return {"result": result, "status": status}


@app.post("/api/eosl-lookup")
async def eosl_lookup(payload: EolLookupBatchRequest) -> dict[str, object]:
    results = await asyncio.to_thread(
        lookup_os_eosl_batch,
        [item.model_dump() for item in payload.items],
    )
    return {"results": results}


@app.get("/api/vendor-lookups/sources")
async def vendor_lookup_sources() -> dict[str, object]:
    return {"sources": vendor_list_sources()}


@app.get("/api/vendor-lookups/{source_id}/status")
async def vendor_lookup_status(source_id: str) -> dict[str, object]:
    if source_id not in VALID_VENDOR_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown vendor source: {source_id}")
    return await asyncio.to_thread(vendor_get_status, source_id)


@app.get("/api/vendor-lookups/{source_id}/rows")
async def vendor_lookup_rows(source_id: str) -> dict[str, object]:
    if source_id not in VALID_VENDOR_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown vendor source: {source_id}")
    rows = await asyncio.to_thread(vendor_list_rows, source_id)
    status = await asyncio.to_thread(vendor_get_status, source_id)
    return {"rows": rows, "status": status, "source_id": source_id}


@app.get("/api/vendor-lookups/settings")
async def vendor_lookup_settings_get() -> dict[str, object]:
    return await asyncio.to_thread(vendor_get_lookup_settings)


@app.post("/api/vendor-lookups/settings")
async def vendor_lookup_settings_save(
    payload: VendorLookupSettingsRequest,
) -> dict[str, object]:
    try:
        return await asyncio.to_thread(
            vendor_save_lookup_settings,
            {
                "sources": {
                    source_id: source.model_dump(exclude_none=True)
                    for source_id, source in payload.sources.items()
                }
            },
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/vendor-lookups/{source_id}/preferences")
async def vendor_lookup_preferences(
    source_id: str,
    payload: VendorSyncRequest,
) -> dict[str, object]:
    if source_id not in VALID_VENDOR_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown vendor source: {source_id}")
    options: dict[str, object] = {}
    if payload.enabled is not None:
        options["enabled"] = payload.enabled
    if payload.keywords is not None:
        options["keywords"] = payload.keywords
    if payload.manufacturers is not None:
        options["manufacturers"] = payload.manufacturers
    try:
        result = await asyncio.to_thread(
            vendor_save_source_preferences,
            source_id,
            options,
        )
    except KeyError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Preferences are not supported for {source_id}",
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    status = await asyncio.to_thread(vendor_get_status, source_id)
    return {"result": result, "status": status, "source_id": source_id}


@app.post("/api/vendor-lookups/{source_id}/sync")
async def vendor_lookup_sync(
    source_id: str,
    payload: VendorSyncRequest | None = None,
) -> dict[str, object]:
    if source_id not in VALID_VENDOR_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown vendor source: {source_id}")
    if VENDOR_SYNC_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="A vendor lookup update is already running. Please wait for it to finish.",
        )
    options: dict[str, object] = {}
    if payload and payload.manufacturers is not None:
        options["manufacturers"] = payload.manufacturers
    async with VENDOR_SYNC_LOCK:
        try:
            result = await asyncio.to_thread(
                vendor_sync_source,
                source_id,
                options=options or None,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001 - surface scrape failures to UI
            raise HTTPException(
                status_code=502,
                detail=f"Failed to update {source_id} database: {error}",
            ) from error
    status = await asyncio.to_thread(vendor_get_status, source_id)
    return {"result": result, "status": status, "source_id": source_id}


@app.post("/api/vendor-lookups/{source_id}/sync/stream")
async def vendor_lookup_sync_stream(
    source_id: str,
    payload: VendorSyncRequest | None = None,
) -> StreamingResponse:
    if source_id not in VALID_VENDOR_SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown vendor source: {source_id}")
    if VENDOR_SYNC_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="A vendor lookup update is already running. Please wait for it to finish.",
        )
    options: dict[str, object] = {}
    if payload and payload.manufacturers is not None:
        options["manufacturers"] = payload.manufacturers
    return StreamingResponse(
        vendor_lookup_sync_events(source_id, options or None),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/vendor-lookups/sync/{job_id}/cancel")
async def vendor_lookup_sync_cancel(job_id: str) -> dict[str, object]:
    cancel_event = ACTIVE_VENDOR_SYNC_JOBS.get(job_id)
    if cancel_event is None:
        raise HTTPException(
            status_code=404,
            detail="Update job not found. It may have already finished.",
        )
    cancel_event.set()
    return {"cancelling": True, "job_id": job_id}


@app.post("/api/vendor-lookup")
async def vendor_lookup(payload: EolLookupBatchRequest) -> dict[str, object]:
    """Local vendor fallback after endoflife.date
    (eosl → microsoft-lifecycle → junos → suse → layer23-switch → router-switch)."""
    results = await asyncio.to_thread(
        lookup_vendor_batch,
        [item.model_dump() for item in payload.items],
    )
    return {"results": results}


@app.get("/api/junos/status")
async def junos_status() -> dict[str, object]:
    return await asyncio.to_thread(junos_get_status)


@app.get("/api/junos/rows")
async def junos_rows() -> dict[str, object]:
    rows = await asyncio.to_thread(junos_list_all_rows)
    status = await asyncio.to_thread(junos_get_status)
    return {"rows": rows, "status": status}


@app.post("/api/junos/sync")
async def junos_sync() -> dict[str, object]:
    if VENDOR_SYNC_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="A vendor lookup update is already running. Please wait for it to finish.",
        )
    async with VENDOR_SYNC_LOCK:
        try:
            result = await asyncio.to_thread(sync_junos_database)
        except Exception as error:  # noqa: BLE001 - surface scrape failures to UI
            raise HTTPException(
                status_code=502,
                detail=f"Failed to update Junos database: {error}",
            ) from error
    status = await asyncio.to_thread(junos_get_status)
    return {"result": result, "status": status}


@app.post("/api/junos-lookup")
async def junos_lookup(payload: EolLookupBatchRequest) -> dict[str, object]:
    results = await asyncio.to_thread(
        lookup_os_junos_batch,
        [item.model_dump() for item in payload.items],
    )
    return {"results": results}


@app.get("/api/suse/status")
async def suse_status() -> dict[str, object]:
    return await asyncio.to_thread(suse_get_status)


@app.get("/api/suse/rows")
async def suse_rows() -> dict[str, object]:
    rows = await asyncio.to_thread(suse_list_all_rows)
    status = await asyncio.to_thread(suse_get_status)
    return {"rows": rows, "status": status}


@app.post("/api/suse/sync")
async def suse_sync() -> dict[str, object]:
    if VENDOR_SYNC_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="A vendor lookup update is already running. Please wait for it to finish.",
        )
    async with VENDOR_SYNC_LOCK:
        try:
            result = await asyncio.to_thread(sync_suse_database)
        except Exception as error:  # noqa: BLE001 - surface scrape failures to UI
            raise HTTPException(
                status_code=502,
                detail=f"Failed to update SUSE database: {error}",
            ) from error
    status = await asyncio.to_thread(suse_get_status)
    return {"result": result, "status": status}


@app.post("/api/suse-lookup")
async def suse_lookup(payload: EolLookupBatchRequest) -> dict[str, object]:
    results = await asyncio.to_thread(
        lookup_os_suse_batch,
        [item.model_dump() for item in payload.items],
    )
    return {"results": results}


@app.post("/api/os-import/inspect")
async def os_import_inspect(file: UploadFile = File(...)) -> dict[str, object]:
    content = await file.read()
    try:
        return await asyncio.to_thread(
            inspect_os_import_file,
            content,
            file.filename or "upload.csv",
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/os-import/extract")
async def os_import_extract(
    file: UploadFile = File(...),
    columns: str = Form(...),
) -> dict[str, object]:
    try:
        selected_columns = json.loads(columns)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Invalid columns payload.") from error

    if not isinstance(selected_columns, list):
        raise HTTPException(status_code=400, detail="Columns must be a JSON array.")

    content = await file.read()
    try:
        return await asyncio.to_thread(
            extract_distinct_os_values,
            content,
            file.filename or "upload.csv",
            [str(column) for column in selected_columns],
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
