# OS Health Check

Web UI for maintaining an **OS normalization and lifecycle lookup** CSV (`eol_lookup.csv`).

**New here?** Start with the step-by-step [User Guide](USER_GUIDE.md) (features, navigation, and everyday workflows). This README covers setup, configuration, and technical detail.

Use it to:

- Search and browse existing OS lookup rows
- Add one or many OS strings with fuzzy (and optional AI) matching
- Refresh EOL / EOAS dates from [endoflife.date](https://endoflife.date)
- Refresh EOL / EOAS from local **Vendor Lookups** ([eosl.date](https://eosl.date) OS scrape, [Juniper Junos](https://support.juniper.net/support/eol/software/junos/) scrape, [SUSE lifecycle](https://www.suse.com/lifecycle/), [Layer23-Switch EOL](https://www.layer23-switch.com/eol-eosl-tool/), [Router-Switch EOL](https://www.router-switch.com/eol-eosl-checker/))
- Keep match/EOL **evidence** (proof) in a JSON sidecar
- Promote Draft → Data via Validate, then optionally upload Data to Azure Blob

## Stack

- **FastAPI** — API, CSV/evidence I/O, Azure upload
- **Jinja2** — app shell
- **Vanilla HTML / CSS / JS** — table UI and workflows
- **PostgreSQL** — vendor lookup scrape caches
- **AI providers (optional)** — OpenAI, Google Gemini, or OpenRouter for AI match + Ambiguous OS detection
- **endoflife.date API** — primary lifecycle dates
- **Vendor Lookups** — local scrapes used when the API misses

---

## Quick start (recommended: Docker)

You need **Docker** and **Docker Compose**.

```bash
# 1. Clone / open this repo, then create your env file
cp .env.example .env

# 2. (Optional) Edit .env and add AI keys — see "Configure .env" below
#    Vendor lookups work without AI keys.

# 3. Start PostgreSQL + the app
docker compose up --build
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Compose starts:

| Service | Role |
|---------|------|
| `db` | PostgreSQL 16 — vendor lookup caches |
| `os-health-check` | FastAPI app on port `8000` (override with `APP_PORT`) |

The app bind-mounts the repo and enables live reload by default (`UVICORN_RELOAD=true`), so code edits apply without rebuilding.

**First useful steps in the UI**

1. Open the app → Source **Data** (read-only published CSV).
2. Click **Edit Data** to work in **Draft**.
3. (Optional) Turn on **AI match** and pick a provider (needs a key in `.env`).
4. Under **View / Update Vendor Lookups**, run **Update** for sources you care about (populates Postgres).
5. Use **Refresh EOL/EOAS** to fill dates (API first, then vendor caches).
6. **Validate** when ready to publish Draft → Data.

---

## Configure `.env`

Copy the template, then fill only what you need:

```bash
cp .env.example .env
```

Never commit `.env` (it is gitignored). For Portainer, set the same variables in the stack Environment UI instead of a file.

### Full reference

| Variable | Required? | Default | Purpose |
|----------|-----------|---------|---------|
| `DATABASE_URL` | **Yes** for vendor lookups | Compose: `postgresql://oshealth:oshealth@db:5432/oshealth` | PostgreSQL connection string |
| `POSTGRES_USER` | Compose `db` service | `oshealth` | Postgres username |
| `POSTGRES_PASSWORD` | Compose `db` service | `oshealth` | Postgres password |
| `POSTGRES_DB` | Compose `db` service | `oshealth` | Postgres database name |
| `OPENAI_API_KEY` | For OpenAI | *(empty)* | Enables the **OpenAI** provider |
| `OPENAI_MODEL` | Optional | `gpt-4o-mini` | OpenAI chat model |
| `GEMINI_API_KEY` | For Gemini | *(empty)* | Enables the **Gemini** provider |
| `GOOGLE_API_KEY` | Alternate for Gemini | *(empty)* | Accepted if `GEMINI_API_KEY` is empty |
| `GEMINI_MODEL` | Optional | `gemini-2.0-flash` | Gemini model id |
| `OPENROUTER_API_KEY` | For OpenRouter | *(empty)* | Enables the **OpenRouter** provider |
| `OPENROUTER_MODEL` | Optional | `openrouter/free` | OpenRouter model / router (see below) |
| `APP_PORT` | Optional | `8000` | Host port mapped to the container |
| `UVICORN_RELOAD` | Optional | `true` (compose) | Live reload; set `false` in production-style deploys |

### Minimal `.env` (Docker, no AI)

Enough to run the app + Postgres with vendor lookups:

```env
POSTGRES_USER=oshealth
POSTGRES_PASSWORD=oshealth
POSTGRES_DB=oshealth
DATABASE_URL=postgresql://oshealth:oshealth@db:5432/oshealth
```

### Example `.env` with all three AI providers

```env
POSTGRES_USER=oshealth
POSTGRES_PASSWORD=oshealth
POSTGRES_DB=oshealth
DATABASE_URL=postgresql://oshealth:oshealth@db:5432/oshealth

OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash

OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=openrouter/free
```

You do **not** need every provider. Configure one (or more). In Edit mode, pick which provider to use from the **Provider** dropdown next to **AI match**.

### `DATABASE_URL` tips

| How you run | Typical `DATABASE_URL` |
|-------------|-------------------------|
| `docker compose` (app talks to service `db`) | `postgresql://oshealth:oshealth@db:5432/oshealth` |
| App on host, Postgres in Docker published on `5432` | `postgresql://oshealth:oshealth@127.0.0.1:5432/oshealth` |
| Your own Postgres | `postgresql://USER:PASSWORD@HOST:5432/DBNAME` |

User / password / db name in the URL must match `POSTGRES_*` (or your real Postgres credentials).

---

## AI providers (OpenAI, Gemini, OpenRouter)

AI is **optional**. Fuzzy matching works without any API key. AI is used for:

1. **AI match** — when fuzzy match fails, suggest a normalization from **existing CSV pairs only** (never invents names).
2. **Ambiguous OS detection** — strings with `/` that list multiple products.

### Supported providers

| Provider | Env key(s) | Default model | Notes |
|----------|------------|---------------|-------|
| **OpenAI** | `OPENAI_API_KEY` | `OPENAI_MODEL` → `gpt-4o-mini` | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Gemini** | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `GEMINI_MODEL` → `gemini-2.0-flash` | [Google AI Studio](https://aistudio.google.com/apikey) |
| **OpenRouter** | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` → `openrouter/free` | [openrouter.ai/keys](https://openrouter.ai/keys) |

**OpenRouter model tip:** `openrouter/free` is OpenRouter’s free-models router — it picks an available free model for you. You still set `OPENROUTER_MODEL` (or accept the default); you do not omit the field entirely. To pin a specific free or paid model, set e.g. `meta-llama/llama-3.3-70b-instruct:free` or another [OpenRouter model slug](https://openrouter.ai/models).

### Enable AI in the UI

1. Put at least one provider key in `.env` and restart the app (`docker compose up` / restart the container).
2. Switch to **Draft** (Edit Data).
3. Turn on **AI match**.
4. Choose **OpenAI**, **Gemini**, or **OpenRouter** in the **Provider** dropdown.
   - Options without a configured key show as unavailable (e.g. “OpenAI (no key)”).
5. Optionally open **Settings → AI match prompt** to customize matching rules in plain language (default is shown; leave as default or clear to use the built-in prompt). `{threshold}` is replaced with the confidence cutoff at runtime. Technical output rules (`pair_index` / JSON shape) are always appended by the app and are not editable.

AI match stays **off by default** even when keys are present, so you never get surprise AI calls.

### Settings tabs

**Settings** has two tabs:

| Tab | What it controls | Stored in |
|-----|------------------|-----------|
| **Vendor lookups** | Enable sources + family keywords for Refresh fallback | `_data/vendor_lookup_settings.json` |
| **AI match prompt** | Custom system prompt for AI normalization | `_config/app_settings.json` |

Provider choice and AI on/off are also stored in `_config/app_settings.json` (updated from the toolbar).

---

## Run without Docker

1. Install **Python 3.12+** and a **PostgreSQL** instance.
2. Create a database/user (or reuse defaults from `.env.example`).
3. Configure `.env`:

```env
DATABASE_URL=postgresql://oshealth:oshealth@127.0.0.1:5432/oshealth
# plus optional OPENAI_* / GEMINI_* / OPENROUTER_* as above
```

4. Install and run:

```bash
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Without `DATABASE_URL` / Postgres, the main CSV UI still works, but **Vendor Lookups** (Update / Refresh fallback to local scrapes) will not.

### Portainer

Deploy `docker-compose.yml` as a stack, then set environment variables in the Portainer UI (`DATABASE_URL` / `POSTGRES_*`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, models, `APP_PORT`, etc.).

Azure **Deploy** uses Azure CLI (`az login`) on the host. The default image does **not** include `az`, so Deploy from Portainer needs a host with Azure CLI or a custom image that installs it.

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
├── version_match.py            # Shared release/version scoring
├── os_import_service.py        # Bulk import from CSV/XLSX
├── vendor_lookups/             # Local vendor scrape caches + Refresh routing
│   ├── __init__.py
│   ├── db.py                   # PostgreSQL pool + per-source schemas
│   ├── vendor_settings.py      # Persistent enable/keywords for vendor Refresh
│   ├── vendor_lookup_service.py  # Registry + routed vendor fallback lookup
│   ├── eosl_service.py         # eosl.date scraper (OS only)
│   ├── junos_service.py        # Juniper Junos Dates & Milestones scraper
│   ├── suse_service.py         # SUSE lifecycle scraper
│   ├── layer23_switch_service.py  # Layer23-Switch EOL/EOSL scraper
│   └── router_switch_service.py   # Router-Switch EOL/EOSL scraper
├── templates/index.html        # UI + client workflows
├── static/                     # CSS, favicon
├── Dockerfile                  # Container image
├── docker-compose.yml          # App + PostgreSQL (local / Portainer)
├── docker/entrypoint.sh        # uvicorn startup (+ optional --reload)
├── .env.example                # Documented env vars (copy to .env)
├── _data/
│   ├── eol_lookup.csv          # Canonical published lookup
│   ├── eol_lookup_evidence.json
│   ├── vendor_lookup_settings.json  # Refresh enable/keywords (shared)
│   ├── layer23_switch_sync.json # Layer23-Switch manufacturer selection
│   └── router_switch_sync.json # Router-Switch manufacturer selection
├── _draft/                     # Working editable copy (+ evidence)
├── _config/                    # Local settings (gitignored)
│   ├── app_settings.json       # ai_enabled, ai_provider, ai_match_prompt
│   └── azure.json              # Named Azure Blob profiles + active selection
└── _backup/                    # Timestamped backups on Validate
```

Vendor lookup scrapes are stored in PostgreSQL (schemas: `eosl`, `junos`, `suse`, `layer23_switch`, `router_switch`). Re-run **Vendor Lookups → Update** after a fresh deploy to populate them.

## High-level architecture

```mermaid
flowchart LR
  UI["Browser UI<br/>templates/index.html"]
  API["FastAPI<br/>app.py"]
  Norm["normalization_service"]
  Eol["eol_service"]
  subgraph vl [vendor_lookups]
    Vendor["vendor_lookup_service"]
    Eosl["eosl_service"]
    Junos["junos_service"]
  end
  Data["_data/eol_lookup.csv"]
  Draft["_draft/eol_lookup.csv"]
  Ev["_data / _draft evidence JSON"]
  DB[("PostgreSQL<br/>vendor schemas")]
  EOLAPI["endoflife.date"]
  EOSLSITE["eosl.date"]
  JUNIPER["support.juniper.net"]
  AI["OpenAI / Gemini / OpenRouter"]
  Az["Azure Blob via az CLI"]

  UI <--> API
  API --> Norm
  API --> Eol
  API --> Vendor
  Vendor --> Eosl
  Vendor --> Junos
  API --> Data
  API --> Draft
  API --> Ev
  Eol --> EOLAPI
  Eosl --> DB
  Junos --> DB
  Eosl -->|scrape| EOSLSITE
  Junos -->|scrape| JUNIPER
  Norm --> AI
  API --> Az
```

---

## Modes: Data vs Draft

The **Source** dropdown switches between the published lookup and the editable working copy. Scraped vendor data is viewed under **Vendor Lookups** — see [Vendor Lookups](#vendor-lookups-postgresql-caches).

| | **Data** (read-only) | **Draft** (editable) |
|--|----------------------|----------------------|
| Purpose | Published lookup | Working copy |
| Edit Data | Shown | Hidden |
| Add OS / bulk / delta | Hidden | Shown |
| Auto-save, AI match, Save, Validate, Revert, Delete draft | Hidden | Shown |
| Deploy / Settings | Shown | Hidden |
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
3. **AI match** — **off by default**. When enabled and the selected provider’s API key is set (`OPENAI_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY`, or `OPENROUTER_API_KEY`), AI may choose only from existing CSV pairs; never invents names. Batches are grouped by vendor so Oracle items don’t see AlmaLinux pairs in the same prompt. Accepted picks must also pass code checks: confidence ≥ threshold, same vendor, compatible version family, and no extra Windows SKU words (e.g. Pro must not become Pro Enterprise). OpenAI and OpenRouter get a stricter “prefer null over guess” instruction because small chat models tend to over-match compared with Gemini.
4. **Conservative** — if unsure → no match (better blank than wrong).

**Example:** `Oracle Linux Server 9.5` → fuzzy/AI can map to `Oracle Linux 9`, but must **not** map to `AlmaLinux OS 9`.

---

## EOL / EOAS refresh flow

**Refresh EOL/EOAS** fills dates per row in this order. **endoflife.date is always first** (not configurable). Local Vendor Lookups follow a fixed order: **eosl → junos → suse → layer23-switch → router-switch**. Specialists (junos / suse / layer23-switch / router-switch) only run when **enabled** and their **family keywords** match. eosl has no keyword gate. Enable flags and keywords are edited under **Settings → Vendor lookups** and stored in `_data/vendor_lookup_settings.json` (Layer23-Switch and Router-Switch are **disabled by default**).

### Per-row decision order

1. **endoflife.date API** — always tried first (same query preference as below). Release matching is **conservative**: no version (or only bitness / `SP3`-style pack digits used alone as a version) → **no match** (never guess the latest release); bare major like `11` does not pick `11.4`; only a strong version hit populates dates/names. Train matching compares **numeric** dotted segments (`17.09.08` → API release `17.9`; `11.4` → `11`). (SUSE Vendor Lookup still understands `11 SP3` as a full release identity.)
2. **If the API returned dates/status** → write them (evidence `api` / `eol`). **Stop.** Vendor DBs are **not** consulted.
3. **If the API missed (or failed)** → call **Vendor Lookups** (`POST /api/vendor-lookup`) in fixed order:
   - **eosl** (if enabled) → evidence `eosl`
   - **junos** (if enabled **and** keywords match) → evidence `junos`
   - **suse** (if enabled **and** keywords match) → evidence `suse`
   - **layer23-switch** (if enabled **and** keywords match; off by default) → evidence `layer23-switch`
   - **router-switch** (if enabled **and** keywords match; off by default) → evidence `router-switch`
4. **If vendor DBs also miss** → copy dates from another row with the same normalized pair when possible (evidence `lookup-fallback`).
5. **Still nothing** → leave blank (evidence `none`).

**Query preference** (for API and vendor lookup): try `normalized_os` → `normalized_os_detailed_name` → `os_string`, but **skip** a normalized value if its vendor doesn’t match the raw OS.

**Product slug detection** (endoflife.date): the v1 product catalog (`GET /api/v1/products`) is cached and indexed by slug, label, and aliases. Inventory strings are normalized first (letter/digit boundaries, glued names like `UbuntuLinux`), then matched longest-phrase-first against that index, with a small regex override table for ambiguous families (e.g. `windows-server` vs `windows`, RHEL vs OpenShift).

**Important:** scraping / **Update** under Vendor Lookups only rebuilds the PostgreSQL vendor schemas. It does **not** apply dates to your CSV. Dates are applied only by **Refresh EOL/EOAS** (or equivalent lookup APIs).

```mermaid
flowchart TD
  R[Refresh EOL/EOAS] --> DraftGate{Work on Draft?}
  DraftGate -->|create or load Draft if needed| Row[For each eligible row]

  Row --> Skip{Ambiguous or blank OS?}
  Skip -->|Yes| Next[Skip]
  Skip -->|No| API[Query endoflife.date]

  API --> ApiOk{Dates or status returned?}
  ApiOk -->|Yes| ApplyApi[Write dates<br/>evidence: api]
  ApiOk -->|No| EoslDB[eosl if enabled]

  EoslDB --> EoslHit{eosl hit?}
  EoslHit -->|Yes| ApplyE[evidence: eosl]
  EoslHit -->|No| JunosGate{junos enabled<br/>+ keywords?}

  JunosGate -->|Yes| JunosDB[junos schema]
  JunosGate -->|No| SuseGate
  JunosDB --> JunosHit{Hit?}
  JunosHit -->|Yes| ApplyJ[evidence: junos]
  JunosHit -->|No| SuseGate{suse enabled<br/>+ keywords?}

  SuseGate -->|Yes| SuseDB[suse schema]
  SuseGate -->|No| L23Gate
  SuseDB --> SuseHit{Hit?}
  SuseHit -->|Yes| ApplyS[evidence: suse]
  SuseHit -->|No| L23Gate{layer23-switch enabled<br/>+ keywords?}

  L23Gate -->|Yes| L23DB[layer23_switch schema]
  L23Gate -->|No| RsGate
  L23DB --> L23Hit{Hit?}
  L23Hit -->|Yes| ApplyL[evidence: layer23-switch]
  L23Hit -->|No| RsGate{router-switch enabled<br/>+ keywords?}

  RsGate -->|Yes| RsDB[router_switch schema]
  RsGate -->|No| CopyFb
  RsDB --> RsHit{Hit?}
  RsHit -->|Yes| ApplyR[evidence: router-switch]
  RsHit -->|No| CopyFb{Same normalized pair<br/>has dates elsewhere?}
  CopyFb -->|Yes| ApplyCopy[evidence: lookup-fallback]
  CopyFb -->|No| Empty[evidence: none]

  ApplyApi --> Proof[Store EOL proof]
  ApplyE --> Proof
  ApplyJ --> Proof
  ApplyS --> Proof
  ApplyL --> Proof
  ApplyR --> Proof
  ApplyCopy --> Proof
  Empty --> Proof
```

### When are vendor caches checked?

| Situation | eosl | Junos | SUSE | Layer23-Switch | Router-Switch |
|-----------|------|-------|------|----------------|---------------|
| API hit | No | No | No | No | No |
| API miss + source enabled | Yes (2nd) | If keywords match (3rd) | If keywords match (4th) | If enabled+keywords (5th; off by default) | If enabled+keywords (6th; off by default) |
| Source disabled in Settings | No | No | No | No | No |
| Update scrape only | No CSV write | No CSV write | No CSV write | No CSV write | No CSV write |

Example: `SUSE Linux 11 SP3` → API miss → eosl miss → SUSE keywords match → SUSE DB → evidence `suse`.

Dates are stored as Unix epoch. Status `true`/`false` is only used when a date is missing.

---

## Vendor Lookups (PostgreSQL caches)

Umbrella for **offline** lifecycle scrapes used as the Refresh fallback above. **View / Update Vendor Lookups** opens a read-only modal with a **Source** selector (browse + rebuild cache only).

| Source | Origin | Postgres schema | Date mapping | Used on Refresh when… |
|--------|--------|-----------------|--------------|------------------------|
| **eosl.date** | [eosl.date](https://eosl.date) OS category | `eosl` | EOAS = earliest support date, EOL = latest | API missed **and** source enabled (2nd) |
| **Juniper Junos** | [Junos Dates & Milestones](https://support.juniper.net/support/eol/software/junos/) | `junos` | **EOE → `eol_date`**, **EOS → `eoas_date`**, FRS → released | API+eosl miss, enabled, keywords match (3rd) |
| **SUSE Lifecycle** | [suse.com/lifecycle](https://www.suse.com/lifecycle/) | `suse` | **General Ends → `eol_date`**, **LTSS Ends → `eoas_date`**, FCS → released | prior miss, enabled, keywords match (4th) |
| **Layer23-Switch EOL** | [layer23-switch.com EOL tool](https://www.layer23-switch.com/eol-eosl-tool/) | `layer23_switch` | **EOL Announcement → `eol_date`**, **EOSL → `eoas_date`**, EOS → released | prior miss, enabled, keywords match (5th; **off by default**) |
| **Router-Switch EOL** | [router-switch.com EOL checker](https://www.router-switch.com/eol-eosl-checker/) | `router_switch` | **EOL Announcement → `eol_date`**, **EOSL → `eoas_date`**, EOS → released | prior miss, enabled, keywords match (6th; **off by default**) |

Per-source **Use for Refresh** + **family keywords** are edited under **Settings → Vendor lookups** and saved to `_data/vendor_lookup_settings.json`.

```mermaid
flowchart LR
  subgraph refresh [Refresh EOL/EOAS]
    A[endoflife.date] -->|miss| D[eosl]
    D -->|miss| C[junos if keywords]
    C -->|miss| S[suse if keywords]
    S -->|miss| L23[layer23-switch if enabled]
    L23 -->|miss| RS[router-switch if enabled]
    RS -->|miss| E[lookup-copy]
  end

  subgraph viewer [View / Update Vendor Lookups]
    V[Source select] --> U[Update scrape]
    U --> C
    U --> S
    U --> D
    U --> L232[layer23_switch schema]
    U --> RS2[router_switch schema]
  end
```

### eosl.date notes

- Support-column labels vary; any non-metadata date column feeds earliest/latest EOAS/EOL.
- Strong product **and** release score required; vague `Other … Linux` / bitness / `N.x` false matches are rejected.
- Requests are throttled; scrapes are serialized server-side.

### Junos notes

- One page scrape; table HTML is embedded in the Juniper CMS payload (`sw-eol-table`).
- Product cells like `Junos OS 24.2` (sometimes with trailing maintenance markers) split into product `Junos OS` + release `24.2` / `15.1X53`.
- For Junos rows, EOE is often **before** EOS, so **EOL may be earlier than EOAS** in the app (intentional naming).
- Matching: token gate first, then strong version score. Family-only versions (e.g. `15.1`) do **not** guess an X-train (`15.1X53`); if unsure, blank.

### SUSE notes

- Scrapes [suse.com/lifecycle](https://www.suse.com/lifecycle/) tables that include **General Ends** / **General Support Ends** and **LTSS Ends**.
- **General Ends → `eol_date`**, **LTSS Ends → `eoas_date`**, FCS → released.
- Releases keep SP identity (`11 SP3`, `15 SP4`); generic `SUSE`/`SLES` prefers **SUSE Linux Enterprise Server** (not Desktop/SAP/HPC unless named).
- Conservative: no SP/version → no match; bare `11` does not pick `11 SP3`.

### Router-Switch notes

- Scrapes paginated manufacturer lists under [router-switch.com/eol-eosl-checker](https://www.router-switch.com/eol-eosl-checker/) (Arista, Aruba, Cisco, Dell, Fortinet, H3C, HPE, Juniper, Mellanox, Palo Alto, Ruckus).
- **EOL Announcement → `eol_date`**, **End of Service Life (EOSL) → `eoas_date`**, End of Sale (EOS) → released / viewer “End of Sale”.
- Wired into Refresh as the **last** local fallback, but **disabled by default**. Enable + keywords under **Settings**.
- **Update** shows a manufacturer checklist. Selection is stored in `_data/router_switch_sync.json`.
- Site is behind Cloudflare; sync uses `curl_cffi` Chrome TLS impersonation. Full sync is large (Cisco alone is ~2k pages) and can take a long time.

### Layer23-Switch notes

- Scrapes paginated manufacturer lists under [layer23-switch.com/eol-eosl-tool](https://www.layer23-switch.com/eol-eosl-tool/) (Arista, Aruba, Cisco, Dell, Fortinet, H3C, HPE, Juniper, Mellanox, Palo Alto, Ruckus).
- **EOL Announcement → `eol_date`**, **End of Service Life (EOSL) → `eoas_date`**, End of Sale (EOS) → released / viewer “End of Sale”.
- Wired into Refresh **before Router-Switch**, but **disabled by default**. Enable + keywords under **Settings**.
- **Update** shows a manufacturer checklist. Selection is stored in `_data/layer23_switch_sync.json`.
- Site is behind Cloudflare; sync uses `curl_cffi` Chrome TLS impersonation. Full sync is large and can take a long time.

```mermaid
flowchart TD
  View[View / Update Vendor Lookups] --> Modal[Source select + filterable viewer]
  Modal --> U[Update selected source]
  U --> Prog[Progress dialog]
  Prog --> EoslPath[eosl.date: crawl OS products]
  Prog --> JunosPath[Juniper: fetch junos EOL table]
  Prog --> SusePath[SUSE: fetch lifecycle tables]
  Prog --> L23Path[Layer23-Switch: paginated manufacturer lists]
  Prog --> RsPath[Router-Switch: paginated manufacturer lists]
  EoslPath --> Store[(PostgreSQL schemas)]
  JunosPath --> Store
  SusePath --> Store
  L23Path --> Store
  RsPath --> Store
  Store --> RefreshUI[Viewer table refreshes]
```

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

Proof methods include: `fuzzy`, `ai`, `fuzzy+ai`, `eol` / `api`, `eosl`, `junos`, `suse`, `layer23-switch`, `router-switch`, `lookup-fallback`, `ambiguous`, `manual`, `none`.

The Actions column filter can narrow rows by: Fuzzy, AI, Fuzzy + AI, Manual, EOL API, eosl.date, Juniper Junos, SUSE Lifecycle, Layer23-Switch, Router-Switch, Lookup copy, Ambiguous, or NULL.

---

## Toolbar features

| Control | Default / notes |
|---------|-----------------|
| **Auto-save** | On by default; debounced save to Draft |
| **AI match** | **Off by default**; Edit mode only; Provider = OpenAI / Gemini / OpenRouter; needs that provider’s API key in `.env` |
| **Save Draft** | Manual draft + evidence write |
| **Validate** | Backup Data → write Draft into Data |
| **Revert** | Reset Draft rows (+ evidence) to the Data baseline and **save `_draft/`** immediately |
| **Delete draft** | Remove Draft (+ evidence), return to Data |
| **Show Delta / Download Delta** | Draft-only change view |
| **Settings** | Tabs: **Vendor lookups** (enable/keywords) and **AI match prompt** (custom system prompt) |
| **View / Update Vendor Lookups** | Read-only viewer for eosl / Junos / SUSE / Layer23-Switch / Router-Switch; update/re-scrape per source |
| **Deploy** | Data mode: cloud deploy dialog with named Azure profiles (inline settings + upload); AWS/GCP placeholders |

---

## Main API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` / `POST` | `/api/lookup` | Load / save CSV (+ evidence) |
| `DELETE` | `/api/lookup/draft` | Delete draft |
| `POST` | `/api/normalize-suggest` | AI normalization (if enabled) |
| `POST` | `/api/ambiguous-os-detect` | Detect ambiguous `/` OS strings |
| `POST` | `/api/eol-lookup` | Batch EOL/EOAS from endoflife.date |
| `POST` | `/api/vendor-lookup` | Routed vendor fallback |
| `GET` | `/api/vendor-lookups/sources` | List vendor lookup sources |
| `GET` | `/api/vendor-lookups/settings` | Enable flags + family keywords for Refresh |
| `POST` | `/api/vendor-lookups/settings` | Save enable flags + family keywords |
| `GET` | `/api/vendor-lookups/{source}/rows` | Viewer rows + status |
| `GET` | `/api/vendor-lookups/{source}/status` | DB status for a source |
| `POST` | `/api/vendor-lookups/{source}/sync` | Re-scrape and rebuild that source’s DB |
| `POST` | `/api/eosl-lookup` | Batch from eosl.date only (compat) |
| `GET` / `POST` | `/api/eosl/*` | eosl.date status / rows / sync (compat) |
| `POST` | `/api/junos-lookup` | Batch from Junos DB only |
| `GET` / `POST` | `/api/junos/*` | Junos status / rows / sync |
| `POST` | `/api/suse-lookup` | Batch from SUSE DB only |
| `GET` / `POST` | `/api/suse/*` | SUSE status / rows / sync |
| `GET` / `PUT` | `/api/settings` | Persist `ai_enabled`, `ai_provider`, `ai_match_prompt` |

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
- **AI opt-in** — avoids surprise wrong matches; toggle in Edit mode when needed. Supports OpenAI, Gemini, and OpenRouter.
- **EOL release matching** — if unsure, don’t populate (no version / weak major / bitness → blank; never default to latest release).
- **Vendor keywords** — guardrails for known traps (Oracle/AlmaLinux, Cisco/Apple iOS). Not a full brand encyclopedia; AI + “unsure = no match” covers unknown brands.
- **Draft vs Data** — safe editing; Validate is the promote step; Refresh never silently wipes an existing Draft.
- **Evidence sidecar** — audit trail without changing CSV schema.
