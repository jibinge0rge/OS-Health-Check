# OS Health Check

Web app for maintaining an **OS normalization and lifecycle lookup** — the table that maps raw inventory strings (e.g. `Oracle Linux Server 9.5`) to a normalized name and its **EOL** (end of life) / **EOAS** (end of active support) dates.

**New here?** Start with the step-by-step [User Guide](USER_GUIDE.md) (screen tour, everyday workflows). This README covers setup, configuration, architecture, and technical detail.

Use it to:

- Browse, filter, sort, and search the published lookup
- Add one or many OS strings with fuzzy (and optional AI) matching
- Refresh EOL / EOAS dates from [endoflife.date](https://endoflife.date), then from local **Vendor Lookups** ([eosl.date](https://eosl.date), [Juniper Junos](https://support.juniper.net/support/eol/software/junos/), [SUSE lifecycle](https://www.suse.com/lifecycle/), [Layer23-Switch EOL](https://www.layer23-switch.com/eol-eosl-tool/), [Router-Switch EOL](https://www.router-switch.com/eol-eosl-checker/))
- Track every long-running operation (refresh, add, publish, vendor sync, cloud upload) in a **Background tasks** screen — cancel it, or navigate away and keep editing while it runs
- Keep a per-row **evidence** trail of how each value was filled
- Edit safely in a **Draft**, then **Validate & publish** into **Data** — publish never silently overwrites a colleague's already-published changes; see [Publish safety](#publish-safety-conflict-resolution--staleness) below
- Deploy the published lookup to **Azure Blob** or **AWS S3**

## Stack

- **FastAPI** — API, CSV/evidence I/O, cloud upload
- **Jinja2** — app shell (`templates/index.html` + partials)
- **Vanilla ES modules** — no bundler, no framework; `static/js/*.js` are `<script type="module">`
- **PostgreSQL** — vendor lookup scrape caches (always), and optionally the published lookup + draft itself (see [Where the lookup data lives](#where-the-lookup-data-lives-file-mode-vs-shared-postgres))
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
| `db` | PostgreSQL 16 — vendor lookup caches (always used); also the published lookup + draft when `LOOKUP_DB_ENABLED` is explicitly turned on (see below) |
| `os-health-check` | FastAPI app on port `8000` (override with `APP_PORT`) |

The app bind-mounts the repo and enables live reload by default (`UVICORN_RELOAD=true`), so code edits apply without rebuilding.

**First useful steps in the UI**

1. Open the app → left rail defaults to **Lookup editor**, mode **Data** (read-only published lookup).
2. Click **Edit data** to fork a **Draft**.
3. (Optional) In **Settings → Configure AI**, turn on AI match and pick a provider + model (needs a key in `.env`).
4. Go to **Vendor lookups** and run **Update** for sources you care about (populates Postgres).
5. Back in the editor, use **Refresh EOL/EOAS** to fill dates (API first, then vendor caches).
6. **Validate & publish** when ready to promote Draft → Data.
7. Check **Background tasks** any time you want to see what's still running, or cancel it.

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
| `DATABASE_URL` | **Yes** for vendor lookups | Compose: `postgresql://oshealth:oshealth@db:5432/oshealth` | PostgreSQL connection string, used unconditionally for vendor-lookup caches |
| `LOOKUP_DB_ENABLED` | Optional — explicit opt-in | *(unset / false)* | Set to `true` to **also** move the published lookup + draft into Postgres (requires `DATABASE_URL` too). Leaving it unset keeps the lookup data as local files even if `DATABASE_URL` is set for vendor caches — see [Where the lookup data lives](#where-the-lookup-data-lives-file-mode-vs-shared-postgres) |
| `POSTGRES_USER` | Compose `db` service | `oshealth` | Postgres username |
| `POSTGRES_PASSWORD` | Compose `db` service | `oshealth` | Postgres password |
| `POSTGRES_DB` | Compose `db` service | `oshealth` | Postgres database name |
| `OPENAI_API_KEY` | For OpenAI | *(empty)* | Enables the **OpenAI** provider |
| `OPENAI_MODEL` | Optional | `gpt-4o-mini` | Default OpenAI model (also editable per-provider in Settings — see [AI providers](#ai-providers-openai-gemini-openrouter)) |
| `GEMINI_API_KEY` | For Gemini | *(empty)* | Enables the **Gemini** provider |
| `GOOGLE_API_KEY` | Alternate for Gemini | *(empty)* | Accepted if `GEMINI_API_KEY` is empty |
| `GEMINI_MODEL` | Optional | `gemini-2.0-flash` | Default Gemini model |
| `OPENROUTER_API_KEY` | For OpenRouter | *(empty)* | Enables the **OpenRouter** provider |
| `OPENROUTER_MODEL` | Optional | `openrouter/free` | Default OpenRouter model / router (see below) |
| `APP_PORT` | Optional | `8000` | Host port mapped to the container |
| `UVICORN_RELOAD` | Optional | `true` (compose) | Live reload; set `false` in production-style deploys |

### Minimal `.env` (Docker, no AI, file-mode lookup storage)

Enough to run the app + Postgres with vendor lookups, keeping the published lookup itself as a git-synced CSV — this is the default and what most local/per-person clones should use:

```env
POSTGRES_USER=oshealth
POSTGRES_PASSWORD=oshealth
POSTGRES_DB=oshealth
DATABASE_URL=postgresql://oshealth:oshealth@db:5432/oshealth
```

`DATABASE_URL` here only wires up vendor-lookup caching — the published lookup and draft stay as local files, because `LOOKUP_DB_ENABLED` (see below) is unset. Set that too, explicitly, when you actually want the shared-Postgres lookup-data backend described in [Where the lookup data lives](#where-the-lookup-data-lives-file-mode-vs-shared-postgres).

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

You do **not** need every provider. Configure one (or more), then pick which to use in **Settings → Configure AI**.

### `DATABASE_URL` tips

| How you run | Typical `DATABASE_URL` |
|-------------|-------------------------|
| `docker compose` (app talks to service `db`) | `postgresql://oshealth:oshealth@db:5432/oshealth` |
| App on host, Postgres in Docker published on `5432` | `postgresql://oshealth:oshealth@127.0.0.1:5432/oshealth` |
| Your own Postgres | `postgresql://USER:PASSWORD@HOST:5432/DBNAME` |
| No shared DB at all (vendor lookups won't work either) | Leave `DATABASE_URL` unset — the main editor, Draft/Data, and publish all work fully on local files |

User / password / db name in the URL must match `POSTGRES_*` (or your real Postgres credentials).

---

## Where the lookup data lives: file-mode vs shared Postgres

This app can be run two different ways, and the choice matters a lot once more than one person is using it. **`DATABASE_URL` on its own only wires up vendor-lookup caching** (it always has, in every deployment) — switching the *lookup data itself* to Postgres requires the separate, explicit `LOOKUP_DB_ENABLED=true` flag too. This two-flag split exists specifically so that an existing deployment with `DATABASE_URL` set purely for vendor caches never gets silently switched into an empty Postgres-backed lookup it never asked for.

### File mode (default — `LOOKUP_DB_ENABLED` unset)

The published lookup and its evidence sidecar are plain files (`_data/eol_lookup.csv`, `_data/eol_lookup_evidence.json`), typically synced between people via `git`. Each person runs their own local instance against their own clone. This is the original design and is still the default.

Because everyone has an independent copy, **publishing is a 3-way merge, not a blind overwrite**:

- When you click **Edit data**, the app records a **base snapshot** — an exact copy of what Data looked like the instant your Draft was created (sent by your own browser, not re-derived later, so there's no race against Data changing in between).
- When you **Validate & publish**, the app re-reads the *current* Data, compares it against your base snapshot and your Draft, and merges:
  - A row someone else changed (that you never touched) — their change is kept automatically.
  - A row only you changed — your change is kept.
  - A row changed identically by both — no conflict.
  - A row **both of you changed differently** — publish stops and shows you a resolver: pick "Keep mine" or "Keep theirs" per row (default: "Keep theirs", since the most common real conflict is two environments each running Refresh EOL/EOAS and publishing at different times — the one already published is usually the fresher one), or apply one choice to every conflict at once.
  - Real duplicate `os_string` values (which do exist in production data) are never silently collapsed — a key that appears more than once on either side is always surfaced for you to resolve explicitly.
- A lightweight revision counter (`_data/.revision`) drives a **staleness banner**: if you're sitting on Data (not even drafting) and someone else publishes, you're told to reload; if you're mid-Draft and someone else publishes, you get a reassurance notice ("it's fine to keep editing — publishing will merge this in") rather than a surprise at the end.

**Commit `_data/.revision` alongside `_data/eol_lookup.csv`** on every publish (`git add _data/` covers both) — the staleness banner only stays meaningful across clones if the revision counter travels with the data file through git, the same way the CSV itself does.

The merge logic lives in `lookup_extras.py::merge_lookup_rows`; the endpoints are `POST /api/lookup/validate/check` (preview conflicts, no writes) and `POST /api/lookup/validate` / `POST /api/lookup/validate/stream` (the real publish, same merge, writes only if nothing is left unresolved).

### Shared Postgres (production — `LOOKUP_DB_ENABLED=true` and `DATABASE_URL` both set)

Once `LOOKUP_DB_ENABLED=true` is set (with `DATABASE_URL` pointing at Postgres), the published lookup, draft, and evidence move into a dedicated `lookup` Postgres schema (`lookup_db.py`) instead of local files — one shared source of truth for everyone hitting that server, no independent copies to reconcile. Publish becomes a normal atomic transaction with an optimistic-concurrency guard: if Data was published again since your Draft's `expected_revision`, the transaction is rejected outright (409, "Data was published again since your draft started — refresh and reapply your changes") instead of overwriting — no per-row merge needed because there's nothing to merge, just one table. Concurrent publish attempts are serialized with a Postgres advisory lock so exactly one ever wins.

Backups happen automatically inside the same publish transaction (a `backups` table row, not a file) — query it directly with SQL if you need to recover an older Data state.

**Migrating existing file-mode data into Postgres**: run the bundled one-time import script before cutting a deployment over, so you don't lose what's already published:

```bash
python lookup_db.py
```

(Reads the current `_data/eol_lookup.csv` + evidence and writes them into the `lookup` schema's `data` source. Run this once, from an environment where `DATABASE_URL` is already set and `_data/` still has the file-mode content you want to keep — then set `LOOKUP_DB_ENABLED=true` on the deployment before it starts serving traffic, so it actually reads the lookup from Postgres instead of falling back to `_data/`.)

**Non-goals, on purpose**: switching to Postgres does *not* turn Draft into a per-user thing — there's still exactly one shared Draft, same as file mode, just relocated. Two people editing that one shared Draft at the same time can still step on each other's in-progress edits (that's a separate, larger feature if it's ever wanted). Also, don't mix modes against the same deployment — pick file mode or Postgres mode per environment; running both against the same `_data/` would silently fork into two unrelated stores.

---

## AI providers (OpenAI, Gemini, OpenRouter)

AI is **optional**. Fuzzy matching works without any API key. AI is used for:

1. **AI match** — when fuzzy match fails, suggest a normalization from **existing pairs only** (never invents names).
2. **Ambiguous OS detection** — strings with `/` that list multiple products.

### Supported providers

| Provider | Env key(s) | Default model | Notes |
|----------|------------|---------------|-------|
| **OpenAI** | `OPENAI_API_KEY` | `OPENAI_MODEL` → `gpt-4o-mini` | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Gemini** | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `GEMINI_MODEL` → `gemini-2.0-flash` | [Google AI Studio](https://aistudio.google.com/apikey) |
| **OpenRouter** | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` → `openrouter/free` | [openrouter.ai/keys](https://openrouter.ai/keys) |

**OpenRouter model tip:** `openrouter/free` is OpenRouter's free-models router — it picks an available free model for you. To pin a specific model, set a real [OpenRouter model slug](https://openrouter.ai/models) instead, e.g. `meta-llama/llama-3.1-8b-instruct:free`.

### Choosing a model per provider (in the UI, not just `.env`)

`.env`'s `*_MODEL` variables are just the fallback default. The actual, editable source of truth is **`_config/ai_model_choices.json`** (gitignored, created automatically the first time you pick a custom model), and it's managed from **Settings → Configure AI**:

1. Pick a provider chip (OpenAI / Gemini / OpenRouter).
2. Open the **Model** dropdown — it lists a curated set of models for that provider (kept in the same JSON file) plus an **Add custom model…** option for anything not listed (useful for OpenRouter especially, since its catalog is far bigger than any curated list could cover).
3. **Reset to default** restores that provider's built-in default model.

A model you add via "Add custom model…" is saved back into `_config/ai_model_choices.json` so it's a normal option from then on — you can also hand-edit that file directly if you'd rather manage the list that way.

### Confidence cutoff

Also in **Settings → Configure AI**: a slider (50–100%, default 85%) sets how confident an AI match has to be before it's accepted. It updates live while dragging and only saves once you release it.

### Enable AI in the UI

1. Put at least one provider key in `.env` and restart the app (`docker compose up` / restart the container).
2. Enter a Draft (**Edit data**).
3. In **Settings → Configure AI**, turn on **AI match**.
4. Pick a provider chip — providers without a configured key show as unavailable.
5. Optionally edit the **AI match system prompt** (plain language; `{threshold}` is replaced with the confidence cutoff at runtime; the JSON output contract is always appended by the app and isn't editable).

AI match stays **off by default** even when keys are present, so you never get surprise AI calls.

### Settings screen (3 tabs)

| Tab | What it controls | Stored in |
|-----|-------------------|-----------|
| **Vendor lookups** | Enable sources + family keywords for Refresh fallback | `_data/vendor_lookup_settings.json` |
| **Configure AI** | AI on/off, provider, model per provider, confidence cutoff, custom system prompt | `_config/app_settings.json` (+ `_config/ai_model_choices.json` for the model catalog) |
| **Appearance** | Theme (light/dark) and row density (compact/comfortable) | Browser `localStorage` only — per-browser, not shared |

---

## Run without Docker

1. Install **Python 3.12+** and a **PostgreSQL** instance (skip Postgres entirely if you're fine losing Vendor Lookups and staying in file mode).
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

Without `DATABASE_URL` / Postgres, the main editor still works fully in file mode, but **Vendor Lookups** (Update / Refresh fallback to local scrapes) will not.

### Portainer

Deploy `docker-compose.yml` as a stack, then set environment variables in the Portainer UI (`DATABASE_URL` / `POSTGRES_*`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, models, `APP_PORT`, etc.).

Cloud **Deploy** (Azure/AWS) shells out to the `az` / `aws` CLI on the host running the app. The default image does **not** include either CLI, so Deploy from Portainer needs a host with the relevant CLI installed (or a custom image that adds it) and an authenticated session (`az login` / `aws configure`).

---

## CSV schema

The lookup (file mode: `_data/eol_lookup.csv`; Postgres mode: the `lookup` schema's `rows` table) has exactly these 7 fields:

| Field | Meaning |
|--------|---------|
| `os_string` | Raw OS as seen in inventory |
| `normalized_os_detailed_name` | Detailed normalized name |
| `normalized_os` | Short normalized name |
| `eol_date` | End of life (Unix epoch string, or empty) |
| `eol_status` | `true` / `false` / empty (only when date missing) |
| `eoas_date` | End of active support (epoch, or empty) |
| `eoas_status` | `true` / `false` / empty |

UI-only fields (matched-by, auto flags, evidence) are **not** part of this schema — evidence lives in a separate sidecar (file mode) or table (Postgres mode). Consecutive duplicate words in `normalized_os_detailed_name` / `normalized_os` are automatically collapsed wherever the app writes them (Refresh, Add OS, "Same as OS") — e.g. a value that would otherwise read `Apple macOS macOS 26 (Tahoe)` is written as `Apple macOS 26 (Tahoe)`. The raw `os_string` itself is never altered.

---

## Project layout

```
OS-Health-Check/
├── app.py                      # FastAPI routes, file/DB-mode switch, publish/merge orchestration
├── lookup_extras.py            # Evidence classification, Data-vs-Draft diff, 3-way publish merge
├── lookup_db.py                # Postgres-backed lookup storage (activated by LOOKUP_DB_ENABLED=true + DATABASE_URL)
├── normalization_service.py    # Vendor tags, fuzzy helpers, AI match, model/provider config
├── eol_service.py               # endoflife.date lookup
├── version_match.py             # Shared release/version scoring
├── os_import_service.py         # Bulk import from CSV/XLSX
├── vendor_lookups/              # Local vendor scrape caches + Refresh routing
│   ├── db.py                    # PostgreSQL pool + per-source schemas (shared by lookup_db.py)
│   ├── vendor_settings.py       # Persistent enable/keywords for vendor Refresh
│   ├── vendor_lookup_service.py # Registry + routed vendor fallback lookup
│   ├── eosl_service.py          # eosl.date scraper (OS only)
│   ├── junos_service.py         # Juniper Junos Dates & Milestones scraper
│   ├── suse_service.py          # SUSE lifecycle scraper
│   ├── layer23_switch_service.py  # Layer23-Switch EOL/EOSL scraper
│   └── router_switch_service.py   # Router-Switch EOL/EOSL scraper
├── templates/
│   ├── index.html               # App shell: topbar, left rail, screen mounts
│   ├── _editor.html             # Lookup editor: mode bar, toolbar, bulk bar, table, footer
│   ├── _filters_panel.html      # Column filters aside
│   ├── _drawer.html             # Row detail aside
│   ├── _vendor.html             # Vendor lookups screen
│   ├── _deploy.html             # Deploy screen (Azure/AWS)
│   ├── _settings.html           # Settings screen (3 tabs)
│   ├── _background.html         # Background tasks screen (Active / History)
│   └── _modals.html             # Refresh / Add OS / Validate & publish / delete / revert /
│                                 #   vendor-update / deploy-upload / generic-prompt modals + toast
├── static/
│   ├── css/                     # tokens (light+dark), base/shell, editor, panels, modals,
│   │                             #   vendor, settings, deploy, background — one file per area
│   └── js/                      # native ES modules, no bundler
│       ├── main.js              # Bootstrap: nav, theme/density, initial load
│       ├── state.js             # Central client state
│       ├── api.js               # fetch wrappers for every endpoint + SSE stream helpers
│       ├── editor.js            # Table render, filters, selection, mode-bar/toolbar/bulk-bar,
│       │                        #   Add OS pipeline, publish + conflict-resolution flow
│       ├── filters_panel.js  drawer.js  vendor.js  settings.js  deploy.js
│       ├── modals.js            # Generic modal chrome + progress rendering (attaches to tasks.js)
│       ├── tasks.js             # Background-task registry (survives modal close / navigation)
│       ├── background.js        # Background tasks screen (Active / History tabs)
│       ├── notifications.js     # Topbar bell dropdown, sourced from tasks.js completions
│       ├── staleness.js         # Proactive "Data changed" banner (fresh-on-draft-start +
│       │                        #   periodic revision check)
│       ├── date_utils.js  icons.js
├── Dockerfile                   # Container image
├── docker-compose.yml            # App + PostgreSQL (local / Portainer)
├── docker/entrypoint.sh          # uvicorn startup (+ optional --reload)
├── .env.example                  # Documented env vars (copy to .env)
├── _data/
│   ├── eol_lookup.csv            # Canonical published lookup (file mode)
│   ├── eol_lookup_evidence.json
│   ├── .revision                 # Publish counter for the staleness banner — commit this
│   │                              #   alongside eol_lookup.csv on every publish (file mode)
│   └── vendor_lookup_settings.json  # Refresh enable/keywords (shared)
├── _draft/                       # Working editable copy (gitignored)
│   ├── eol_lookup.csv  eol_lookup_evidence.json
│   └── eol_lookup.base.csv  eol_lookup.base_evidence.json  eol_lookup.base.revision
│                              # Merge base snapshot, captured when the draft was created
├── _config/                      # Local settings (gitignored)
│   ├── app_settings.json         # ai_enabled, ai_provider, ai_confidence_threshold, ai_models, prompt
│   ├── ai_model_choices.json     # Per-provider model catalog (curated + anything you've added)
│   ├── azure.json                # Named Azure Blob profiles + active selection
│   └── aws.json                  # Named AWS S3 profiles + active selection
└── _backup/                      # Timestamped backups on publish (file mode)
```

Vendor lookup scrapes are stored in PostgreSQL (schemas: `eosl`, `junos`, `suse`, `layer23_switch`, `router_switch`) whenever `DATABASE_URL` is set; the lookup data itself only moves into its own `lookup` schema (`rows`, `evidence`, `meta`, `backups` tables) when `LOOKUP_DB_ENABLED=true` is also set — otherwise it stays in `_data/` files even with `DATABASE_URL` present. Re-run **Vendor Lookups → Update** after a fresh deploy to populate the vendor schemas.

---

## App shell & navigation

The left rail has four groups plus Settings:

| Group | Screen | Purpose |
|-------|--------|---------|
| Lookup | **Lookup editor** | The main table — browse, filter, sort, edit, publish |
| Sources | **Vendor lookups** | Browse and rebuild the local vendor scrape caches |
| Publish | **Deploy** | Upload the published lookup to Azure Blob or AWS S3 |
| Activity | **Background tasks** | Every long-running operation, live or finished |
| *(footer)* | **Settings** | Vendor lookups config, Configure AI, Appearance |

The topbar shows a **published-at** timestamp and a notification bell that lights up when a background task finishes while you're looking elsewhere.

## Background tasks

Refresh EOL/EOAS, Add OS, Validate & publish, vendor source updates, and cloud uploads all run as **background tasks**: closing their progress modal or navigating to a different screen doesn't stop them — they keep running and you can watch or cancel them from **Background tasks** at any time. Starting the same kind of task twice is blocked until the first finishes (you can't run two refreshes at once, for example).

| Task kind | Cancellable? | Notes |
|-----------|---------------|-------|
| `refresh` (Refresh EOL/EOAS) | Yes | Server-side job with a cooperative cancel signal |
| `add-os` (Add OS pipeline) | Yes | Client-orchestrated; cancel takes effect within one row |
| `publish` (Validate & publish) | **No** | Backup → write → delete-draft is one atomic sequence with no safe midpoint to stop at — offering Cancel here would be a broken promise, so it's deliberately not offered |
| `deploy-upload:{provider}` | Yes | Aborting the request also stops the underlying `az`/`aws` CLI subprocess |
| `vendor-sync:{source}` | Yes | One kind per vendor source |

History keeps the most recent 40 finished tasks (success or failure, with the error message when relevant), pruned by count rather than age.

---

## Add OS flow

**Add OS** — a single string, a pasted list, or a CSV/Excel import (pick columns → distinct values). Duplicates (same `os_string`) are skipped. The whole pipeline runs as a cancellable, resumable-progress background task (`add-os`) — for a large batch, the EOL/EOAS lookup phase is chunked and streamed server-side so progress advances in real increments instead of sitting at a single "in progress" state for the whole batch.

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
  AIOn -->|Yes| AI[AI picks from allowed pairs only<br/>vendor-scoped]
  AIOn -->|No| Empty[Leave norm blank]
  AI --> Apply
  AI -->|No sure match| Empty

  Apply --> Dedup[Collapse consecutive<br/>duplicate words]
  Empty --> EOL
  Dedup --> EOL[Batched EOL/EOAS lookup]
  EOL --> Done[Row added plus evidence]
  AmbRow --> Done
```

### Matching rules (simple)

1. **Fuzzy first** — compare the OS string to existing `normalized_os_detailed_name` / `normalized_os` (not other raw `os_string`s). Score must be high (≥ 95%).
2. **Vendor guardrails** — keyword brands (Oracle, AlmaLinux, Cisco, Apple, Windows, …). Different brands cannot match (e.g. Oracle Linux ≠ AlmaLinux).
3. **AI match** — **off by default**. When enabled and the selected provider's API key is set, AI may choose only from existing pairs; never invents names. Batches are grouped by vendor so Oracle items don't see AlmaLinux pairs in the same prompt. Accepted picks must also pass code checks: confidence ≥ the cutoff set in Settings, same vendor, compatible version family, and no extra Windows SKU words (e.g. Pro must not become Pro Enterprise).
4. **Conservative** — if unsure → no match (better blank than wrong).

**Example:** `Oracle Linux Server 9.5` → fuzzy/AI can map to `Oracle Linux 9`, but must **not** map to `AlmaLinux OS 9`.

---

## EOL / EOAS refresh flow

**Refresh EOL/EOAS** fills dates per row in this order. **endoflife.date is always first** (not configurable). Local Vendor Lookups follow a fixed order: **eosl → junos → suse → layer23-switch → router-switch**. Specialists (junos / suse / layer23-switch / router-switch) only run when **enabled** and their **family keywords** match. eosl has no keyword gate. Enable flags and keywords are edited under **Settings → Vendor lookups** and stored in `_data/vendor_lookup_settings.json` (Layer23-Switch and Router-Switch are **disabled by default**).

Refresh **only re-queries lifecycle sources — it does not re-run fuzzy/AI normalization**. It sends whatever `normalized_os_detailed_name` / `normalized_os` a row already has (or the raw `os_string` if those are blank) into the lifecycle lookup; it never calls the matching pipeline that Add OS uses. If a row's normalized fields are wrong, fix them by hand, use **Same as OS**, or re-add the row through Add OS.

### Per-row decision order

1. **endoflife.date API** — always tried first (same query preference as below). Release matching is **conservative**: no version (or only bitness / `SP3`-style pack digits used alone as a version) → **no match** (never guess the latest release); bare major like `11` does not pick `11.4`; only a strong version hit populates dates/names. Train matching compares **numeric** dotted segments (`17.09.08` → API release `17.9`; `11.4` → `11`). (SUSE Vendor Lookup still understands `11 SP3` as a full release identity.)
2. **If the API returned dates/status** → write them (evidence `api` / `eol`). **Stop.** Vendor DBs are **not** consulted.
3. **If the API missed (or failed)** → call **Vendor Lookups** in fixed order:
   - **eosl** (if enabled) → evidence `eosl`
   - **junos** (if enabled **and** keywords match) → evidence `junos`
   - **suse** (if enabled **and** keywords match) → evidence `suse`
   - **layer23-switch** (if enabled **and** keywords match; off by default) → evidence `layer23-switch`
   - **router-switch** (if enabled **and** keywords match; off by default) → evidence `router-switch`
4. **If vendor DBs also miss** → copy dates from another row with the same normalized pair when possible (evidence `lookup-fallback`).
5. **Still nothing** → leave blank (evidence `none`).

Whenever a lifecycle source hands back its own canonical name and a row's normalized field was blank, that name is used to fill it — after collapsing any consecutive duplicate words (e.g. `macOS macOS 26` → `macOS 26`) — this is a side effect of the lookup, not a fuzzy-match step.

**Query preference** (for API and vendor lookup): try `normalized_os` → `normalized_os_detailed_name` → `os_string`, but **skip** a normalized value if its vendor doesn't match the raw OS.

**Product slug detection** (endoflife.date): the v1 product catalog (`GET /api/v1/products`) is cached and indexed by slug, label, and aliases. Inventory strings are normalized first (letter/digit boundaries, glued names like `UbuntuLinux`), then matched longest-phrase-first against that index, with a small regex override table for ambiguous families (e.g. `windows-server` vs `windows`, RHEL vs OpenShift).

**Important:** scraping / **Update** under Vendor Lookups only rebuilds the PostgreSQL vendor schemas. It does **not** apply dates to your lookup. Dates are applied only by **Refresh EOL/EOAS** (or the equivalent lookup APIs).

### When are vendor caches checked?

| Situation | eosl | Junos | SUSE | Layer23-Switch | Router-Switch |
|-----------|------|-------|------|----------------|---------------|
| API hit | No | No | No | No | No |
| API miss + source enabled | Yes (2nd) | If keywords match (3rd) | If keywords match (4th) | If enabled+keywords (5th; off by default) | If enabled+keywords (6th; off by default) |
| Source disabled in Settings | No | No | No | No | No |
| Update scrape only | No write | No write | No write | No write | No write |

Example: `SUSE Linux 11 SP3` → API miss → eosl miss → SUSE keywords match → SUSE DB → evidence `suse`.

Dates are stored as Unix epoch. Status `true`/`false` is only used when a date is missing.

---

## Vendor Lookups (PostgreSQL caches)

Umbrella for **offline** lifecycle scrapes used as the Refresh fallback above. The **Vendor lookups** screen is read-only for browsing (Source selector, filterable viewer) — it does not write into your lookup.

| Source | Origin | Postgres schema | Date mapping | Used on Refresh when… |
|--------|--------|-----------------|--------------|------------------------|
| **eosl.date** | [eosl.date](https://eosl.date) OS category | `eosl` | EOAS = earliest support date, EOL = latest | API missed **and** source enabled (2nd) |
| **Juniper Junos** | [Junos Dates & Milestones](https://support.juniper.net/support/eol/software/junos/) | `junos` | **EOE → `eol_date`**, **EOS → `eoas_date`**, FRS → released | API+eosl miss, enabled, keywords match (3rd) |
| **SUSE Lifecycle** | [suse.com/lifecycle](https://www.suse.com/lifecycle/) | `suse` | **General Ends → `eol_date`**, **LTSS Ends → `eoas_date`**, FCS → released | prior miss, enabled, keywords match (4th) |
| **Layer23-Switch EOL** | [layer23-switch.com EOL tool](https://www.layer23-switch.com/eol-eosl-tool/) | `layer23_switch` | **EOL Announcement → `eol_date`**, **EOSL → `eoas_date`**, EOS → released | prior miss, enabled, keywords match (5th; **off by default**) |
| **Router-Switch EOL** | [router-switch.com EOL checker](https://www.router-switch.com/eol-eosl-checker/) | `router_switch` | **EOL Announcement → `eol_date`**, **EOSL → `eoas_date`**, EOS → released | prior miss, enabled, keywords match (6th; **off by default**) |

Per-source enable + family keywords are edited under **Settings → Vendor lookups** and saved to `_data/vendor_lookup_settings.json`. Each **Update** runs as a cancellable background task (`vendor-sync:{source}`).

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
- **EOL Announcement → `eol_date`**, **End of Service Life (EOSL) → `eoas_date`**, End of Sale (EOS) → released.
- Wired into Refresh as the **last** local fallback, but **disabled by default**. Enable + keywords under **Settings**.
- Manufacturer selection is stored in `_data/router_switch_sync.json`.
- Site is behind Cloudflare; sync uses `curl_cffi` Chrome TLS impersonation. Full sync is large (Cisco alone is ~2k pages) and can take a long time — run it as a background task and keep working elsewhere.

### Layer23-Switch notes

- Scrapes paginated manufacturer lists under [layer23-switch.com/eol-eosl-tool](https://www.layer23-switch.com/eol-eosl-tool/) (same manufacturer set as Router-Switch).
- **EOL Announcement → `eol_date`**, **End of Service Life (EOSL) → `eoas_date`**, End of Sale (EOS) → released.
- Wired into Refresh **before Router-Switch**, but **disabled by default**. Enable + keywords under **Settings**.
- Manufacturer selection is stored in `_data/layer23_switch_sync.json`.
- Site is behind Cloudflare; sync uses `curl_cffi` Chrome TLS impersonation.

---

## Evidence (proof)

Per-row evidence of how each field was filled, keyed by `os_string`:

- File mode: `_data/eol_lookup_evidence.json` / `_draft/eol_lookup_evidence.json`
- Postgres mode: `lookup.evidence` table, one JSON payload per source

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

The row detail drawer's **Matched by** field, and the column filters panel's **Matched by** chip row, group these into: `All`, `endoflife.date`, `Fuzzy`, `AI`, `eosl.date`, `Juniper Junos`, `SUSE Lifecycle`, `Manual`, `Ambiguous`, `No match`.

---

## Lookup editor screen

**Mode bar** — segmented **Data** / **Draft** control:
- **Data**: read-only; shows **Edit data** (or **Resume draft**, if one exists).
- **Draft**: **Only changed rows** toggle, **Auto-save** toggle, a Saved/Unsaved pill, and **Save draft** / **Exit draft** / **Revert all changes** / **Delete draft** / **Validate & publish**.

**Toolbar** — search box; quick chips **All / Missing normalization / Past EOL / Past EOAS / No dates / Ambiguous** (plus **Changed**, Draft-only); **Refresh EOL/EOAS**, **Add OS** (Draft-only), **Export**, and **Column filters** (badge shows the active-filter count).

**Column filters panel** — per-field mode + text for OS string / Normalized detailed name / Normalized OS (all / contains / excludes / equals / empty / not empty), per-field mode + date range + status for EOL date / EOAS date (all / passed / upcoming / empty / not empty, status any/true/false), and the Matched-by chip row above.

**Table** — all 7 columns (OS string, Normalized detailed name, Normalized OS, EOL date, EOL status, EOAS date, EOAS status) are sortable by clicking the header (cycles ascending → descending → unsorted); Draft mode adds a leading selection checkbox column. Empty EOL/EOAS status and normalized-name cells render as a dotted "none" chip, not italic text.

**Bulk bar** (Draft, with a selection) — `{n} selected`, **Refresh lifecycle**, **Same as OS**, **Revert to Data**, **Export selection**, **Delete**, **Clear**.

**Row detail drawer** — click a row to open it. Shows Normalized detailed name / Normalized OS / EOL date+status / EOAS date+status (editable in Draft) and Matched by (always read-only), plus the row's evidence list. Draft-only actions: **Same as OS** (collapses consecutive duplicate words in the OS string and copies it into both normalized fields), **Re-run lookup** (re-queries lifecycle sources for just this row), **Revert row** (resets every field except `os_string` back to the published Data value).

---

## Publish safety: conflict resolution & staleness

Covered in depth in [Where the lookup data lives](#where-the-lookup-data-lives-file-mode-vs-shared-postgres). In short, from the editor's point of view:

1. Clicking **Validate & publish** immediately checks for conflicts (`POST /api/lookup/validate/check`) before showing you anything else.
2. If there are none, you see the usual KPI tiles (new / edited / still-unresolved rows) and an optional backup-name suffix.
3. If there are conflicts, a resolver replaces that view: each conflicting row shows both versions with **Keep mine** / **Keep theirs** radios (defaulted to "theirs"), plus **Keep mine for all** / **Keep theirs for all** bulk buttons. The confirm button is disabled and reads **Resolve & publish** until every conflict has a choice.
4. Either way, the actual publish (backup → write → delete draft) then runs as a background task like any other.

While you're on Data (not drafting) or sitting in an open Draft, a banner appears if Data has moved since you last knew about it — a reload prompt on the Data view, a reassurance note in Draft (since the merge above already handles it safely at publish time).

```mermaid
flowchart LR
  Edit[Edit in Draft] --> Save[Auto-save / Save Draft]
  Save --> Check[Validate and publish: check for conflicts]
  Check -->|conflicts| Resolve[Resolver: keep mine / theirs]
  Resolve --> Publish
  Check -->|none| Publish[Backup Data, write merged rows, delete Draft]
  Publish --> CloudOpt[Optional: Deploy to Azure / AWS]
```

---

## Deploy (Azure Blob / AWS S3)

Available from **Data** only — Deploy uploads the validated, published lookup; Drafts cannot be deployed.

- Two provider cards: **Azure Blob** and **AWS S3** (no other providers).
- Each supports named, multi-profile configuration:
  - **Azure**: Storage account, Container, Blob path (auth: `az login` on the host)
  - **AWS**: Bucket, Region, Key (auth: `aws configure` on the host)
- **+ New profile**, **Save profile**, **Delete profile**; upload button reads "Upload to {provider}".
- In Postgres mode, there's no local file to hand the CLI directly — the app exports the current Data to a throwaway temp CSV for the upload and cleans it up afterward. File mode uploads `_data/eol_lookup.csv` directly.

---

## Main API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/lookup` | Load rows + evidence for a source (`data`/`draft`); includes `data_revision` and, for `draft`, `based_on_revision` |
| `POST` | `/api/lookup` | Save rows + evidence to a source; on first-ever draft save (or an explicit reset from Revert), captures the merge base |
| `GET` | `/api/lookup/evidence` | Evidence for one `os_string` |
| `GET` | `/api/lookup/diff` | Added / edited / deleted / unresolved counts, Draft vs Data |
| `POST` | `/api/lookup/validate/check` | No-write preview of a publish: conflicts (file mode) or staleness (DB mode) |
| `POST` | `/api/lookup/validate` | Publish (non-streaming) |
| `POST` | `/api/lookup/validate/stream` | Publish with live progress (the UI's actual publish path) |
| `POST` | `/api/lookup/row/refresh` | Re-run lifecycle lookup for one row |
| `POST` | `/api/lookup/rows/refresh` | Batch lifecycle lookup, no persistence |
| `POST` | `/api/lookup/rows/refresh/stream` | Same, chunked + streamed progress |
| `POST` | `/api/lookup/refresh/stream` | Bulk "Refresh EOL/EOAS" with live progress; persists to the given source |
| `POST` | `/api/lookup/refresh/{job_id}/cancel` | Cancel a running refresh job |
| `GET` | `/api/lookup/download` | Export a source as CSV |
| `DELETE` | `/api/lookup/draft` | Delete the draft |
| `POST` | `/api/normalize-suggest` | AI normalization (if enabled) |
| `POST` | `/api/ambiguous-os-detect` | Detect ambiguous `/` OS strings |
| `POST` | `/api/eol-lookup` | Batch EOL/EOAS from endoflife.date |
| `POST` | `/api/vendor-lookup` | Routed vendor fallback |
| `GET` | `/api/vendor-lookups/sources` | List vendor lookup sources |
| `GET` / `POST` | `/api/vendor-lookups/settings` | Enable flags + family keywords for Refresh |
| `GET` | `/api/vendor-lookups/{source}/rows` | Viewer rows |
| `GET` | `/api/vendor-lookups/{source}/status` | DB status for a source |
| `POST` | `/api/vendor-lookups/{source}/sync` / `/sync/stream` | Re-scrape and rebuild that source's DB |
| `POST` | `/api/vendor-lookups/sync/{job_id}/cancel` | Cancel a running vendor sync |
| `GET` / `PUT` | `/api/settings` | AI enabled/provider/model(s)/confidence/prompt |
| `GET` / `PUT` | `/api/azure/settings`, `POST /api/azure/upload` | Named Azure Blob profiles + upload |
| `GET` / `PUT` | `/api/aws/settings`, `POST /api/aws/upload` | Named AWS S3 profiles + upload |
| `POST` | `/api/os-import/inspect`, `/api/os-import/extract` | CSV/XLSX bulk-import column picking |

(Not listed: the eosl/junos/suse-specific compatibility routes under `/api/eosl*`, `/api/junos*`, `/api/suse*` — superseded by the routed `/api/vendor-lookup` and `/api/vendor-lookups/*` endpoints above but kept for backward compatibility.)

---

## Design choices worth knowing

- **Fuzzy before AI** — fast, local, no API key required.
- **AI opt-in, model + confidence configurable** — avoids surprise wrong matches; both live in Settings, not hidden in `.env`.
- **EOL release matching** — if unsure, don't populate (no version / weak major / bitness → blank; never default to latest release).
- **Vendor keywords** — guardrails for known traps (Oracle/AlmaLinux, Cisco/Apple iOS). Not a full brand encyclopedia; AI + "unsure = no match" covers unknown brands.
- **Draft vs Data** — safe editing; Validate & publish is the promote step; Refresh never silently wipes an existing Draft.
- **Evidence sidecar** — audit trail without changing the lookup's own schema.
- **Background tasks are decoupled from any view** — a task keeps running in a client-side registry regardless of whether its progress modal is open; closing it (or navigating away) only detaches the view, never the task.
- **Publish never blindly overwrites** — file mode's 3-way merge and Postgres mode's revision-guarded transaction exist specifically so publishing in parallel with someone else never silently discards their already-published work; see [Publish safety](#publish-safety-conflict-resolution--staleness).
- **Duplicate `os_string` values are never silently collapsed** — both the diff and the publish merge treat a real duplicate as something to resolve explicitly, not something to dedupe by picking one arbitrarily.
