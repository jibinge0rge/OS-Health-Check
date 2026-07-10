from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from eol_service import lookup_os_eol_batch


BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "_data" / "eol_lookup.csv"
DRAFT_PATH = BASE_DIR / "_draft" / "eol_lookup.csv"
BACKUP_DIR = BASE_DIR / "_backup"
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


@app.delete("/api/lookup/draft")
async def delete_draft_lookup() -> dict[str, object]:
    if not DRAFT_PATH.exists():
        raise HTTPException(status_code=404, detail="Draft lookup CSV not found.")

    DRAFT_PATH.unlink()
    return {"deleted": True, "source": "draft"}


@app.post("/api/eol-lookup")
async def eol_lookup(payload: EolLookupBatchRequest) -> dict[str, object]:
    results = lookup_os_eol_batch(
        [item.model_dump() for item in payload.items],
    )
    return {"results": results}
