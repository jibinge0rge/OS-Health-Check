from __future__ import annotations

import asyncio
import csv
import json
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
BACKUP_DIR = BASE_DIR / "_backup"
CONFIG_DIR = BASE_DIR / "_config"
AZURE_CONFIG_PATH = CONFIG_DIR / "azure.json"
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


AZURE_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def lookup_path(source: str) -> Path:
    normalized = source.strip().lower()
    if normalized == "data":
        return DATA_PATH
    if normalized == "draft":
        return DRAFT_PATH
    raise HTTPException(status_code=400, detail="Unsupported lookup source.")


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
    return {"headers": CSV_HEADERS, "rows": load_rows(source), "source": source}


@app.post("/api/lookup")
async def update_lookup(payload: LookupPayload, source: str = "draft") -> dict[str, object]:
    save_rows(payload.rows, source)
    return {"saved": True, "row_count": len(payload.rows), "source": source}


@app.post("/api/lookup/validate")
async def validate_lookup(payload: LookupPayload) -> dict[str, object]:
    backup_path = backup_data_file()
    save_rows(payload.rows, "data")
    return {
        "validated": True,
        "row_count": len(payload.rows),
        "source": "data",
        "backup_path": str(backup_path) if backup_path else "",
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
    return {"deleted": True, "source": "draft"}


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
        return {"results": []}

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

    return {"results": results}


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
