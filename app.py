from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from eol_service import lookup_os_eol_batch
from eosl_service import (
    get_status as eosl_get_status,
    list_all_rows as eosl_list_all_rows,
    lookup_os_eosl_batch,
    sync_os_database as eosl_sync_os_database,
)
from junos_service import (
    get_status as junos_get_status,
    list_all_rows as junos_list_all_rows,
    lookup_os_junos_batch,
    sync_junos_database,
)
from suse_service import (
    get_status as suse_get_status,
    list_all_rows as suse_list_all_rows,
    lookup_os_suse_batch,
    sync_suse_database,
)
from vendor_lookup_service import (
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
    DEFAULT_FUZZY_MATCH_THRESHOLD,
    detect_ambiguous_os_batch,
    normalize_ai_provider,
    provider_api_key_configured,
    suggest_normalization_batch,
)
from os_import_service import extract_distinct_os_values, inspect_os_import_file


load_dotenv()


BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "_data" / "eol_lookup.csv"
DRAFT_PATH = BASE_DIR / "_draft" / "eol_lookup.csv"
DATA_EVIDENCE_PATH = BASE_DIR / "_data" / "eol_lookup_evidence.json"
DRAFT_EVIDENCE_PATH = BASE_DIR / "_draft" / "eol_lookup_evidence.json"
BACKUP_DIR = BASE_DIR / "_backup"
CONFIG_DIR = BASE_DIR / "_config"
AZURE_CONFIG_PATH = CONFIG_DIR / "azure.json"
APP_SETTINGS_PATH = CONFIG_DIR / "app_settings.json"
CSV_HEADERS = [
    "os_string",
    "normalized_os_detailed_name",
    "normalized_os",
    "eol_date",
    "eol_status",
    "eoas_date",
    "eoas_status",
]

app = FastAPI(title="OS Health Check")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Serialize vendor scrape runs so only one hits a remote source at a time.
VENDOR_SYNC_LOCK = asyncio.Lock()
# Back-compat alias used by older EOSL-only call sites.
EOSL_SYNC_LOCK = VENDOR_SYNC_LOCK
VALID_VENDOR_SOURCES = {
    "eosl",
    "junos",
    "suse",
    "layer23-switch",
    "router-switch",
}
# In-flight vendor sync jobs (job_id -> cancel event) so the Stop button can
# reach a background scrape that's already streaming to a client.
ACTIVE_VENDOR_SYNC_JOBS: dict[str, threading.Event] = {}


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

class AppSettings(BaseModel):
    ai_enabled: bool = False
    ai_provider: str = "openai"

    @field_validator("ai_provider", mode="before")
    @classmethod
    def validate_ai_provider(cls, value: object) -> str:
        return normalize_ai_provider(value)


class AppSettingsResponse(BaseModel):
    ai_enabled: bool = False
    ai_provider: str = "openai"
    ai_available: bool = False
    openai_available: bool = False
    gemini_available: bool = False


AZURE_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def openai_api_key_configured() -> bool:
    return provider_api_key_configured("openai")


def gemini_api_key_configured() -> bool:
    return provider_api_key_configured("gemini")


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


def load_evidence(source: str = "data") -> dict[str, object]:
    path = evidence_path(source)
    if not path.exists():
        return empty_evidence_payload()

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return empty_evidence_payload()

    return normalize_evidence_payload(payload)


def save_evidence(evidence: dict[str, object], source: str = "data") -> dict[str, object]:
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


def delete_evidence(source: str) -> None:
    path = evidence_path(source)
    if path.exists():
        path.unlink()


def load_rows(source: str = "data") -> list[dict[str, str]]:
    path = lookup_path(source)
    if not path.exists():
        detail = "Draft lookup CSV not found." if source == "draft" else "Lookup CSV not found."
        raise HTTPException(status_code=404, detail=detail)

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CSV_HEADERS:
            raise HTTPException(
                status_code=500,
                detail="CSV headers do not match the expected lookup schema.",
            )

        return [
            {header: (row.get(header) or "") for header in CSV_HEADERS}
            for row in reader
        ]


def save_rows(rows: list[LookupRow], source: str = "data") -> None:
    path = lookup_path(source)
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


def backup_data_file() -> Path | None:
    if not DATA_PATH.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"eol_lookup_{timestamp}.csv"
    shutil.copy2(DATA_PATH, backup_path)
    return backup_path


def backup_data_evidence() -> Path | None:
    if not DATA_EVIDENCE_PATH.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"eol_lookup_evidence_{timestamp}.json"
    shutil.copy2(DATA_EVIDENCE_PATH, backup_path)
    return backup_path


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

    return AppSettings(
        ai_enabled=bool(payload.get("ai_enabled", False)),
        ai_provider=normalize_ai_provider(payload.get("ai_provider", "openai")),
    )


def save_app_settings(settings: AppSettings) -> AppSettings:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with APP_SETTINGS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(settings.model_dump(), handle, indent=2)
        handle.write("\n")
    return settings


def app_settings_response() -> AppSettingsResponse:
    settings = load_app_settings()
    openai_available = openai_api_key_configured()
    gemini_available = gemini_api_key_configured()
    return AppSettingsResponse(
        ai_enabled=settings.ai_enabled,
        ai_provider=settings.ai_provider,
        ai_available=selected_ai_provider_available(settings),
        openai_available=openai_available,
        gemini_available=gemini_available,
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


async def azure_upload_events(payload: AzureUploadRequest) -> AsyncIterator[str]:
    if not DATA_PATH.exists():
        yield sse_event({"type": "error", "message": "Data lookup CSV not found at _data/eol_lookup.csv."})
        return

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
        str(DATA_PATH),
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
                f"Uploading _data/eol_lookup.csv to "
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


@app.get("/api/lookup")
async def get_lookup(source: str = "data") -> dict[str, object]:
    return {
        "headers": CSV_HEADERS,
        "rows": load_rows(source),
        "source": source,
        "evidence": load_evidence(source),
    }


@app.post("/api/lookup")
async def update_lookup(payload: LookupPayload, source: str = "draft") -> dict[str, object]:
    save_rows(payload.rows, source)
    evidence = save_evidence(prune_evidence_to_rows(payload.evidence, payload.rows), source)
    return {
        "saved": True,
        "row_count": len(payload.rows),
        "source": source,
        "evidence": evidence,
    }


@app.post("/api/lookup/validate")
async def validate_lookup(payload: LookupPayload) -> dict[str, object]:
    backup_path = backup_data_file()
    evidence_backup_path = backup_data_evidence()
    save_rows(payload.rows, "data")
    evidence = save_evidence(prune_evidence_to_rows(payload.evidence, payload.rows), "data")
    # Keep draft evidence aligned with what was validated.
    if DRAFT_PATH.exists():
        save_evidence(evidence, "draft")
    return {
        "validated": True,
        "row_count": len(payload.rows),
        "source": "data",
        "backup_path": str(backup_path) if backup_path else "",
        "evidence_backup_path": str(evidence_backup_path) if evidence_backup_path else "",
        "evidence": evidence,
    }


@app.get("/api/lookup/download")
async def download_lookup(source: str = "data") -> FileResponse:
    path = lookup_path(source)
    if not path.exists():
        detail = "Draft lookup CSV not found." if source == "draft" else "Lookup CSV not found."
        raise HTTPException(status_code=404, detail=detail)

    return FileResponse(
        path=path,
        media_type="text/csv",
        filename="eol_lookup.csv",
    )


@app.delete("/api/lookup/draft")
async def delete_draft_lookup() -> dict[str, object]:
    if not DRAFT_PATH.exists():
        raise HTTPException(status_code=404, detail="Draft lookup CSV not found.")

    DRAFT_PATH.unlink()
    delete_evidence("draft")
    return {"deleted": True, "source": "draft"}


@app.get("/api/settings")
async def get_app_settings() -> AppSettingsResponse:
    return app_settings_response()


@app.put("/api/settings")
async def update_app_settings(payload: AppSettings) -> AppSettingsResponse:
    current = load_app_settings()
    merged = AppSettings(
        ai_enabled=payload.ai_enabled,
        ai_provider=normalize_ai_provider(payload.ai_provider or current.ai_provider),
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
async def upload_lookup_to_azure() -> StreamingResponse:
    payload = require_azure_settings()
    return StreamingResponse(
        azure_upload_events(payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
    """Local vendor fallback after endoflife.date (eosl → junos → suse → layer23-switch → router-switch)."""
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
