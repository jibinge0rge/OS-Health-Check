from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import subprocess
import threading
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
from normalization_service import (
    DEFAULT_FUZZY_MATCH_THRESHOLD,
    detect_ambiguous_os_batch,
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


class AzureSettings(BaseModel):
    account_name: str = ""
    container_name: str = ""
    blob_name: str = ""


class AppSettings(BaseModel):
    ai_enabled: bool = True


class AppSettingsResponse(BaseModel):
    ai_enabled: bool = True
    ai_available: bool = False


AZURE_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def openai_api_key_configured() -> bool:
    return bool(str(os.environ.get("OPENAI_API_KEY") or "").strip())


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


def load_azure_settings() -> AzureSettings:
    if not AZURE_CONFIG_PATH.exists():
        return AzureSettings()

    with AZURE_CONFIG_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Azure settings file is invalid.")

    return AzureSettings(
        account_name=str(payload.get("account_name") or "").strip(),
        container_name=str(payload.get("container_name") or "").strip(),
        blob_name=str(payload.get("blob_name") or "").strip(),
    )


def save_azure_settings(payload: AzureUploadRequest) -> AzureSettings:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    settings = AzureSettings(**payload.model_dump())
    with AZURE_CONFIG_PATH.open("w", encoding="utf-8") as handle:
        json.dump(settings.model_dump(), handle, indent=2)
        handle.write("\n")
    return settings


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

    return AppSettings(ai_enabled=bool(payload.get("ai_enabled", True)))


def save_app_settings(settings: AppSettings) -> AppSettings:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with APP_SETTINGS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(settings.model_dump(), handle, indent=2)
        handle.write("\n")
    return settings


def app_settings_response() -> AppSettingsResponse:
    settings = load_app_settings()
    return AppSettingsResponse(
        ai_enabled=settings.ai_enabled,
        ai_available=openai_api_key_configured(),
    )


def require_azure_settings() -> AzureUploadRequest:
    settings = load_azure_settings()
    if not settings.account_name or not settings.container_name or not settings.blob_name:
        raise HTTPException(
            status_code=400,
            detail="Azure settings are not configured. Save settings first.",
        )

    return AzureUploadRequest(
        account_name=settings.account_name,
        container_name=settings.container_name,
        blob_name=settings.blob_name,
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
    save_app_settings(payload)
    return app_settings_response()


@app.get("/api/azure/settings")
async def get_azure_settings() -> AzureSettings:
    return load_azure_settings()


@app.put("/api/azure/settings")
async def update_azure_settings(payload: AzureUploadRequest) -> AzureSettings:
    return save_azure_settings(payload)


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

    if not load_app_settings().ai_enabled or not openai_api_key_configured():
        return {"results": [None for _ in payload.items], "ai_skipped": True}

    suggestions = await asyncio.to_thread(
        suggest_normalization_batch,
        [item.os_string for item in payload.items],
        [pair.model_dump() for pair in payload.allowed_pairs],
        payload.fuzzy_match_threshold,
    )

    results: list[NormalizeSuggestResult | None] = []
    for suggestion in suggestions:
        if suggestion is None:
            results.append(None)
            continue
        results.append(NormalizeSuggestResult(**suggestion))

    return {"results": results, "ai_skipped": False}


@app.post("/api/ambiguous-os-detect")
async def ambiguous_os_detect(payload: AmbiguousOsDetectRequest) -> dict[str, object]:
    if not payload.items:
        return {"results": []}

    results = await asyncio.to_thread(
        detect_ambiguous_os_batch,
        [item.os_string for item in payload.items],
    )
    return {"results": results}


@app.post("/api/eol-lookup")
async def eol_lookup(payload: EolLookupBatchRequest) -> dict[str, object]:
    results = await asyncio.to_thread(
        lookup_os_eol_batch,
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
