# OS Health Check

Web UI for maintaining an **OS normalization and lifecycle lookup** CSV (`eol_lookup.csv`).

Use it to:

- Search and browse existing OS lookup rows
- Add one or many OS strings with fuzzy (and optional AI) matching
- Refresh EOL / EOAS dates from [endoflife.date](https://endoflife.date)
- Keep match/EOL **evidence** (proof) in a JSON sidecar
- Promote Draft → Data via Validate, then optionally upload Data to Azure Blob

## Stack

- **FastAPI** — API, CSV/evidence I/O, Azure upload
- **Jinja2** — app shell
- **Vanilla HTML / CSS / JS** — table UI and workflows
- **OpenAI** (optional) — AI match + Ambiguous OS detection
- **endoflife.date API** — lifecycle dates

## Run locally

```bash
python -m pip install -r requirements.txt
# optional: set AI keys in .env
# OPENAI_API_KEY=...
# GEMINI_API_KEY=...   # or GOOGLE_API_KEY
python -m uvicorn app:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

| Variable | Required? | Purpose |
|----------|-----------|---------|
| `OPENAI_API_KEY` | For OpenAI AI match / Ambiguous detect | Enables OpenAI provider |
| `OPENAI_MODEL` | Optional | Defaults to `gpt-4o-mini` |
| `GEMINI_API_KEY` | For Gemini AI match / Ambiguous detect | Enables Gemini provider (`GOOGLE_API_KEY` also accepted) |
| `GEMINI_MODEL` | Optional | Defaults to `gemini-2.0-flash` |

In Edit mode, choose **OpenAI** or **Gemini** next to **AI match**. AI match is off by default. Azure upload uses Azure CLI (`az login`), not app secrets.

---

## CSV schema

Lookup CSV has exactly these 7 columns:

| Column | Meaning |
|--------|---------|
| `os_string` | Raw OS as seen in inventory |
| `normalized_os_detailed_name` | Detailed normalized name |
| `normalized_os` | Short normalized name |
| `eol_date` | End of life (Unix epoch string, or empty) |
| `eol_status` | `true` / `false` / empty (only when date missing) |
| `eoas_date` | End of active support (epoch, or empty) |
| `eoas_status` | `true` / `false` / empty |

UI-only fields (auto flags, proof) are **not** stored in the CSV.

---

## Project layout

```
OS-Health-Check/
├── app.py                      # FastAPI routes
├── normalization_service.py    # Vendor tags, fuzzy helpers, AI match
├── eol_service.py              # endoflife.date lookup
├── os_import_service.py        # Bulk import from CSV/XLSX
├── templates/index.html        # UI + client workflows
├── static/                     # CSS, favicon
├── _data/
│   ├── eol_lookup.csv          # Canonical published lookup
│   └── eol_lookup_evidence.json
├── _draft/                     # Working editable copy (+ evidence)
├── _config/                    # Local settings (gitignored)
│   ├── app_settings.json       # ai_enabled, ai_provider (openai|gemini)
│   └── azure.json
└── _backup/                    # Timestamped backups on Validate
```

---

## High-level architecture

```mermaid
flowchart LR
  UI["Browser UI<br/>templates/index.html"]
  API["FastAPI<br/>app.py"]
  Norm["normalization_service"]
  Eol["eol_service"]
  Data["_data/eol_lookup.csv"]
  Draft["_draft/eol_lookup.csv"]
  Ev["_data / _draft evidence JSON"]
  EOLAPI["endoflife.date"]
  OAI["OpenAI optional"]
  Az["Azure Blob via az CLI"]

  UI <--> API
  API --> Norm
  API --> Eol
  API --> Data
  API --> Draft
  API --> Ev
  Eol --> EOLAPI
  Norm --> OAI
  API --> Az
```

---

## Modes: Data vs Draft

| | **Data** (read-only) | **Draft** (editable) |
|--|----------------------|----------------------|
| Purpose | Published lookup | Working copy |
| Edit Data | Shown | Hidden |
| Add OS / bulk / delta | Hidden | Shown |
| Auto-save, AI match, Save, Validate, Revert, Delete draft | Hidden | Shown |
| Azure Settings / Upload | Shown | Hidden |
| Refresh EOL/EOAS | Opens/uses Draft first | Refreshes in place |

**Edit Data** loads an existing Draft if present, otherwise copies Data → Draft.

```mermaid
flowchart TD
  A[Open app] --> B{Source?}
  B -->|Data| C[Read-only table]
  C --> D[Edit Data]
  D --> E{Draft exists?}
  E -->|Yes| F[Load Draft]
  E -->|No| G[Copy Data to Draft]
  F --> H[Editable Draft]
  G --> H
  B -->|Draft| H
  H --> I[Save Draft / Auto-save]
  H --> J[Validate]
  J --> K[Backup Data]
  K --> L[Write Draft to Data]
```

---

## Add OS flow

**Add OS** — one string.  
**Add multiple OS** — paste lines, or import CSV/XLSX (pick columns → distinct values).

Duplicates (same `os_string`) are skipped.

```mermaid
flowchart TD
  Start[New OS string] --> Amb{Contains slash?}
  Amb -->|Yes| AmbAI[Ambiguous OS detect API]
  AmbAI -->|Ambiguous| AmbRow[Set both norm fields to Ambiguous OS<br/>Skip EOL]
  AmbAI -->|Not ambiguous| Fuzzy
  Amb -->|No| Fuzzy[Fuzzy match 95 percent plus<br/>vs existing norm pairs]

  Fuzzy --> Vendor{Same vendor / product family?}
  Vendor -->|No| Reject[Reject candidate]
  Vendor -->|Yes| FuzzyOK{Match found?}
  Reject --> FuzzyOK

  FuzzyOK -->|Yes| Apply[Apply normalized pair]
  FuzzyOK -->|No| AIOn{AI match ON and API key?}
  AIOn -->|Yes| AI[AI picks from allowed CSV pairs only<br/>vendor-scoped]
  AIOn -->|No| Empty[Leave norm blank]
  AI --> Apply
  AI -->|No sure match| Empty

  Apply --> EOL[EOL lookup]
  Empty --> EOL
  EOL --> Done[Row added plus evidence]
  AmbRow --> Done
```

### Matching rules (simple)

1. **Fuzzy first** — compare the OS string to existing `normalized_os_detailed_name` / `normalized_os` (not other raw `os_string`s). Score must be high (≥ 95%).
2. **Vendor guardrails** — keyword brands (Oracle, AlmaLinux, Cisco, Apple, Windows, …). Different brands cannot match (e.g. Oracle Linux ≠ AlmaLinux).
3. **AI match** — **off by default**. When enabled and the selected provider’s API key is set (`OPENAI_API_KEY` or `GEMINI_API_KEY`), AI may choose only from existing CSV pairs; never invents names. Batches are grouped by vendor so Oracle items don’t see AlmaLinux pairs in the same prompt.
4. **Conservative** — if unsure → no match (better blank than wrong).

**Example:** `Oracle Linux Server 9.5` → fuzzy/AI can map to `Oracle Linux 9`, but must **not** map to `AlmaLinux OS 9`.

---

## EOL / EOAS refresh flow

Uses [endoflife.date](https://endoflife.date) product API.

**Query preference:** try `normalized_os` → `normalized_os_detailed_name` → `os_string`, but **skip** a normalized value if its vendor doesn’t match the raw OS (wrong brand leftover).

```mermaid
flowchart TD
  R[Refresh EOL/EOAS] --> D{On Draft?}
  D -->|Yes| Work[Refresh rows]
  D -->|No| DE{Draft exists?}
  DE -->|Yes| LoadD[Load existing Draft]
  DE -->|No| Copy[Copy Data to Draft]
  LoadD --> Work
  Copy --> Work

  Work --> Skip{Ambiguous or blank OS?}
  Skip -->|Yes| Next[Skip row]
  Skip -->|No| Pick[Pick query string<br/>prefer norm if same vendor]
  Pick --> Slug[Resolve product slug]
  Slug --> API[Fetch product plus release]
  API --> Vendor2{Product label matches OS vendor?}
  Vendor2 -->|No| Retry[Retry with raw os_string / skip bad labels]
  Vendor2 -->|Yes| ApplyDates[Write EOL/EOAS dates plus compatible norm labels]
  Retry --> ApplyDates
  ApplyDates --> Proof[Store EOL proof in evidence]
```

Dates are stored as Unix epoch. Status `true`/`false` is only used when a date is missing.

---

## Evidence (proof)

Sidecar JSON next to the CSV (not in the CSV itself):

- `_data/eol_lookup_evidence.json`
- `_draft/eol_lookup_evidence.json`

Shape:

```json
{
  "updated_at": "2026-07-14T12:00:00",
  "by_os": {
    "Oracle Linux Server 9.5": {
      "detailed": { "method": "fuzzy" },
      "normalized": { "method": "fuzzy" },
      "eol": {
        "method": "api",
        "queryUsed": "Oracle Linux 9",
        "queryField": "normalized_os",
        "productSlug": "oracle-linux",
        "apiNote": ""
      }
    }
  }
}
```

Proof methods include: `fuzzy`, `ai`, `fuzzy+ai`, `eol`, `lookup-fallback`, `ambiguous`, `manual`, `none`.

---

## Toolbar features

| Control | Default / notes |
|---------|-----------------|
| **Auto-save** | On by default; debounced save to Draft |
| **AI match** | **Off by default**; Edit mode only; choose OpenAI or Gemini; needs that provider’s API key |
| **Save Draft** | Manual draft + evidence write |
| **Validate** | Backup Data → write Draft into Data |
| **Revert** | Reset Draft rows to Data baseline |
| **Delete draft** | Remove Draft (+ evidence), return to Data |
| **Show Delta / Download Delta** | Draft-only change view |
| **Azure** | Data mode: settings + upload via `az storage blob upload` |

---

## Main API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` / `POST` | `/api/lookup` | Load / save CSV (+ evidence) |
| `DELETE` | `/api/lookup/draft` | Delete draft |
| `POST` | `/api/normalize-suggest` | AI normalization (if enabled) |
| `POST` | `/api/ambiguous-os-detect` | Detect ambiguous `/` OS strings |
| `POST` | `/api/eol-lookup` | Batch EOL/EOAS from endoflife.date |
| `GET` / `PUT` | `/api/settings` | Persist `ai_enabled` + `ai_provider` |

---

## Validate and publish flow

```mermaid
flowchart LR
  Edit[Edit in Draft] --> Save[Auto-save / Save Draft]
  Save --> Val[Validate]
  Val --> Bak[Backup _data CSV plus evidence]
  Bak --> Pub[Overwrite _data from Draft]
  Pub --> AzureOpt[Optional: Azure Upload]
```

---

## Design choices worth knowing

- **Fuzzy before AI** — fast, local, no API key required.
- **AI opt-in** — avoids surprise wrong matches; toggle in Edit mode when needed.
- **Vendor keywords** — guardrails for known traps (Oracle/AlmaLinux, Cisco/Apple iOS). Not a full brand encyclopedia; AI + “unsure = no match” covers unknown brands.
- **Draft vs Data** — safe editing; Validate is the promote step; Refresh never silently wipes an existing Draft.
- **Evidence sidecar** — audit trail without changing CSV schema.
