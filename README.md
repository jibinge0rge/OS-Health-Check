# OS Health Check

Web app for maintaining an **OS normalization and lifecycle lookup** — the table that maps raw inventory strings (e.g. `Oracle Linux Server 9.5`) to a normalized name and its **EOL** (end of life) / **EOAS** (end of active support) dates.

**New here?** Start with the step-by-step [User Guide](docs/USER_GUIDE.md) (screen tour, everyday workflows). This README covers setup, configuration, architecture, and technical detail.

Use it to:

- Browse, filter, sort, and search the published lookup
- Add one or many OS strings with fuzzy (and optional AI) matching
- Refresh EOL / EOAS dates from [endoflife.date](https://endoflife.date), then from local **Vendor Lookups** ([eosl.date](https://eosl.date), [Microsoft Lifecycle](https://learn.microsoft.com/en-us/lifecycle/products/), [Juniper Junos](https://support.juniper.net/support/eol/software/junos/), [SUSE lifecycle](https://www.suse.com/lifecycle/), [Layer23-Switch EOL](https://www.layer23-switch.com/eol-eosl-tool/), [Router-Switch EOL](https://www.router-switch.com/eol-eosl-checker/))
- Track every long-running operation (refresh, add, publish, vendor sync, cloud upload) in a **Background tasks** screen — cancel it, or navigate away and keep editing while it runs
- Keep a per-row **evidence** trail of how each value was filled
- Edit safely in a **Draft**, then **Validate & publish** into **Data** — publish never silently overwrites a colleague's already-published changes; see [Publish safety](#publish-safety-staleness) below
- Deploy the published lookup to **Azure Blob** or **AWS S3**

## Stack

- **FastAPI** — API, CSV/evidence I/O, cloud upload
- **Jinja2** — app shell (`templates/index.html` + partials)
- **Vanilla ES modules** — no bundler, no framework; `static/js/*.js` are `<script type="module">`
- **PostgreSQL** — the only storage backend: vendor lookup scrape caches, and the published lookup + draft itself (see [How the lookup data is stored](#how-the-lookup-data-is-stored))
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
| `db` | PostgreSQL 16 — the app's only storage backend: vendor lookup caches, and the published lookup + draft (see [How the lookup data is stored](#how-the-lookup-data-is-stored)) |
| `os-health-check` | FastAPI app on port `8000` (override with `APP_PORT`) |

The app's code is baked into the image (no bind mount, on purpose — see [How the lookup data is stored](#how-the-lookup-data-is-stored)), and live reload is off by default (`UVICORN_RELOAD=false`). Rebuild (`docker compose up -d --build`) after pulling new code.

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
| `DATABASE_URL` | **Yes** | Compose: `postgresql://oshealth:oshealth@db:5432/oshealth` | PostgreSQL connection string — required for vendor-lookup caches **and** the published lookup + draft. The app refuses to start without it |
| `LOOKUP_DB_ENABLED` | **Yes** | *(none)* | Must be `true` — the app refuses to start otherwise. There is no file-based fallback; see [How the lookup data is stored](#how-the-lookup-data-is-stored) |
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
| `UVICORN_RELOAD` | Optional | `false` (compose) | Live reload; set `true` for local development (there's no bind mount, so this only helps if you also rebuild-on-change some other way) |

### Minimal `.env` (Docker, no AI)

`docker compose up --build` works with **no `.env` at all** — `docker-compose.yml` already defaults `DATABASE_URL`/`LOOKUP_DB_ENABLED` to the bundled `db` service. Only create `.env` once you need to override something (a different Postgres, AI keys, etc.):

```env
POSTGRES_USER=oshealth
POSTGRES_PASSWORD=oshealth
POSTGRES_DB=oshealth
DATABASE_URL=postgresql://oshealth:oshealth@db:5432/oshealth
LOOKUP_DB_ENABLED=true
```

### Example `.env` with all three AI providers

```env
POSTGRES_USER=oshealth
POSTGRES_PASSWORD=oshealth
POSTGRES_DB=oshealth
DATABASE_URL=postgresql://oshealth:oshealth@db:5432/oshealth
LOOKUP_DB_ENABLED=true

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

User / password / db name in the URL must match `POSTGRES_*` (or your real Postgres credentials). There is no working mode without a database — the app refuses to start if `DATABASE_URL` / `LOOKUP_DB_ENABLED=true` aren't both set.

---

## How the lookup data is stored

PostgreSQL is the **only** storage backend — there is no file-based fallback. The published lookup, its draft, evidence, and backups all live in a dedicated `lookup` Postgres schema (`lookup_db.py`): one shared source of truth for everyone hitting that server, so there's nothing to reconcile between independent copies.

Publish is a normal atomic transaction with an optimistic-concurrency guard: if Data was published again since your Draft's `expected_revision`, the transaction is rejected outright (409, "Data was published again since your draft started — refresh and reapply your changes") instead of overwriting. Concurrent publish attempts are serialized with a Postgres advisory lock so exactly one ever wins. Backups happen automatically inside the same publish transaction (a `backups` table row) — query it directly with SQL if you need to recover an older Data state.

**Seeding a brand-new, empty database happens automatically** — no separate step needed. On every container start, `docker/entrypoint.sh` runs `docker/import_if_empty.py` before the app itself: if the `lookup` schema's `data` source has zero rows, it loads in whatever's at the image's baked-in `_data/eol_lookup.csv` (+ evidence sidecar) automatically. It's idempotent and safe to leave running forever — a no-op on every later restart once the DB has data (from that import, or a real publish).

If you're running outside Docker, or want to force a re-import (overwriting whatever's currently in Postgres with `_data/eol_lookup.csv`), the same logic is available by hand:

```bash
python lookup_db.py --force
```

(Without `--force`, it refuses if the `data` source already has rows — the automatic hook above already covers the "first deploy, DB is empty" case, so this is only for an explicit, deliberate overwrite.)

**Non-goal, on purpose**: there's exactly one shared Draft per database, not a per-user one — two people editing that one shared Draft at the same time can still step on each other's in-progress edits (that's a separate, larger feature if it's ever wanted).

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
| **Vendor lookups** | Enable sources + family keywords for Refresh fallback | `_config/vendor_lookup_settings.json` |
| **Configure AI** | AI on/off, provider, model per provider, confidence cutoff, custom system prompt | `_config/app_settings.json` (+ `_config/ai_model_choices.json` for the model catalog) |
| **Appearance** | Theme (light/dark) and row density (compact/comfortable) | Browser `localStorage` only — per-browser, not shared |

---

## Run without Docker

1. Install **Python 3.12+** and a **PostgreSQL** instance — required; there is no file-based fallback.
2. Create a database/user (or reuse defaults from `.env.example`).
3. Configure `.env`:

```env
DATABASE_URL=postgresql://oshealth:oshealth@127.0.0.1:5432/oshealth
LOOKUP_DB_ENABLED=true
# plus optional OPENAI_* / GEMINI_* / OPENROUTER_* as above
```

4. Install and run:

```bash
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

### Portainer

Deploy `docker-compose.yml` as a stack, then set environment variables in the Portainer UI (`DATABASE_URL` / `POSTGRES_*`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, models, `APP_PORT`, etc.).

Cloud **Deploy** (Azure/AWS) shells out to the `az` / `aws` CLI on the host running the app. The default image does **not** include either CLI, so Deploy from Portainer needs a host with the relevant CLI installed (or a custom image that adds it) and an authenticated session (`az login` / `aws configure`).

### Kubernetes

For a real cluster deployment (AKS/EKS/etc.), the manifests live in [`k8s/`](k8s/)
(Kustomize: `base/` + `overlays/{azure,aws,minikube}`) with a full guide in
[`k8s/README.md`](k8s/README.md). Postgres is **not** part of those manifests —
point `DATABASE_URL` at whatever managed Postgres (or self-hosted instance)
you're using; for local testing, keep using `docker compose` instead, which
runs its own Postgres container.

**Prerequisites** (see `k8s/README.md` for detail on each):

- A Kubernetes cluster already running, with `kubectl` pointed at it
- A reachable PostgreSQL database (managed or self-hosted) and its connection string
- A container registry account (Docker Hub by default) to push the built image to
- Docker, to build that image

**What gets deployed**: a `Namespace`, a `Secret` (created by hand via
`kubectl create secret`, never committed — see `k8s/secret.example.yaml`) for
`DATABASE_URL` + AI keys, a `ConfigMap` for non-secret env vars, a
`PersistentVolumeClaim` that persists `_config/` (Settings, vendor-source
toggles) across pod restarts, a `Deployment` (1 replica), a **ClusterIP**
`Service`, and (azure/aws overlays) an HTTPS `Ingress`. Apply with
`kubectl apply -k k8s/overlays/<azure|aws|minikube>`.

**Loading data the first time**: there's no separate "import" step to run by hand. The same startup hook the Docker deployment uses (`docker/import_if_empty.py`) runs automatically inside the pod before the app starts — on a genuinely empty Postgres database, it loads in the lookup data baked into the image (`_data/eol_lookup.csv`) the first time it connects and finds zero rows, logging `[lookup_db] No 'data' rows in Postgres schema 'lookup' yet -- importing N row(s)...`. Every later pod restart is a no-op once the database has any rows (from that import, or a real publish).

**No cloud account yet?** These same manifests can be tested against [minikube](https://minikube.sigs.k8s.io/) — a real one-node Kubernetes cluster that runs inside Docker on your own machine — before you ever touch Azure/AWS. See [k8s/README.md § Testing locally with minikube](k8s/README.md#testing-locally-with-minikube-no-cloud-needed).

Full walkthrough (building/pushing the image, creating the secret, applying an
overlay, watching startup logs, Ingress/TLS, updating a build, tearing it
down): [`k8s/README.md`](k8s/README.md).

---

## CSV schema

The lookup (`lookup` Postgres schema's `rows` table) has exactly these 7 fields:

| Field | Meaning |
|--------|---------|
| `os_string` | Raw OS as seen in inventory |
| `normalized_os_detailed_name` | Detailed normalized name |
| `normalized_os` | Short normalized name |
| `eol_date` | End of life (Unix epoch string, or empty) |
| `eol_status` | `true` / `false` / empty (only when date missing) |
| `eoas_date` | End of active support (epoch, or empty) |
| `eoas_status` | `true` / `false` / empty |

UI-only fields (matched-by, auto flags, evidence) are **not** part of this schema — evidence lives in its own table. Consecutive duplicate words in `normalized_os_detailed_name` / `normalized_os` are automatically collapsed wherever the app writes them (Refresh, Add OS, "Same as OS") — e.g. a value that would otherwise read `Apple macOS macOS 26 (Tahoe)` is written as `Apple macOS 26 (Tahoe)`. The raw `os_string` itself is never altered.

---

## Project layout

```
OS-Health-Check/
├── app.py                      # FastAPI routes, publish orchestration -- Postgres-only, refuses to
│                                #   start without DATABASE_URL + LOOKUP_DB_ENABLED=true
├── lookup_extras.py            # Evidence classification, Data-vs-Draft diff
├── lookup_db.py                # Postgres-backed lookup storage -- the only storage backend
├── normalization_service.py    # Vendor tags, fuzzy helpers, AI match, model/provider config
├── eol_service.py               # endoflife.date lookup
├── version_match.py             # Shared release/version scoring
├── os_import_service.py         # Bulk import from CSV/XLSX
├── vendor_lookups/              # Local vendor scrape caches + Refresh routing
│   ├── db.py                    # PostgreSQL pool + per-source schemas (shared by lookup_db.py)
│   ├── vendor_settings.py       # Persistent enable/keywords for vendor Refresh
│   ├── vendor_lookup_service.py # Registry + routed vendor fallback lookup
│   ├── eosl_service.py          # eosl.date scraper (OS only)
│   ├── microsoft_lifecycle_service.py  # learn.microsoft.com/lifecycle JSON API scraper
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
├── docs/                         # Architecture, setup guides, and production test plans
├── Dockerfile                   # Container image
├── docker-compose.yml            # App + PostgreSQL (local / Portainer)
├── docker/entrypoint.sh          # uvicorn startup (+ optional --reload)
├── docker/import_if_empty.py     # Seeds a brand-new, empty Postgres from _data/ on first startup
├── k8s/                          # Kustomize base + azure/aws/minikube overlays (see k8s/README.md)
├── .env.example                  # Documented env vars (copy to .env)
├── _data/
│   ├── eol_lookup.csv            # Baked-in seed CSV -- only ever read once, by the first-boot
│   │                              #   import hook above, to populate a brand-new empty Postgres
│   └── eol_lookup_evidence.json  # Its evidence sidecar, same one-time purpose
└── _config/                      # Persisted Settings (gitignored; a PVC in Kubernetes)
    ├── app_settings.json         # ai_enabled, ai_provider, ai_confidence_threshold, ai_models, prompt
    ├── ai_model_choices.json     # Per-provider model catalog (curated + anything you've added)
    ├── vendor_lookup_settings.json  # Refresh enable/keywords for eosl/microsoft-lifecycle/junos/suse
    ├── layer23_switch_sync.json  # Manufacturer selection for the Layer23-Switch scraper
    ├── router_switch_sync.json   # Manufacturer selection for the Router-Switch scraper
    ├── azure.json                # Named Azure Blob profiles + active selection
    └── aws.json                  # Named AWS S3 profiles + active selection
```

Vendor lookup scrapes are stored in PostgreSQL (schemas: `eosl`, `microsoft_lifecycle`, `junos`, `suse`, `layer23_switch`, `router_switch`); the lookup data itself lives in its own `lookup` schema (`rows`, `evidence`, `meta`, `backups` tables). Re-run **Vendor Lookups → Update** after a fresh deploy to populate the vendor schemas.

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

**Refresh EOL/EOAS** fills dates per row in this order. **endoflife.date is always first** (not configurable). Local Vendor Lookups follow a fixed order: **eosl → microsoft-lifecycle → junos → suse → layer23-switch → router-switch**. Specialists (junos / suse / layer23-switch / router-switch) only run when **enabled** and their **family keywords** match. eosl and microsoft-lifecycle have no keyword gate — they're general fallbacks gated only by product-name resolution. Enable flags and keywords are edited under **Settings → Vendor lookups** and stored in `_config/vendor_lookup_settings.json` (Microsoft Lifecycle, Layer23-Switch, and Router-Switch are **disabled by default**).

Refresh **only re-queries lifecycle sources — it does not re-run fuzzy/AI normalization**. It sends whatever `normalized_os_detailed_name` / `normalized_os` a row already has (or the raw `os_string` if those are blank) into the lifecycle lookup; it never calls the matching pipeline that Add OS uses. If a row's normalized fields are wrong, fix them by hand, use **Same as OS**, or re-add the row through Add OS.

### Per-row decision order

1. **endoflife.date API** — always tried first (same query preference as below). Release matching is **conservative**: no version (or only bitness / `SP3`-style pack digits used alone as a version) → **no match** (never guess the latest release); bare major like `11` does not pick `11.4`; only a strong version hit populates dates/names. Train matching compares **numeric** dotted segments (`17.09.08` → API release `17.9`; `11.4` → `11`). (SUSE Vendor Lookup still understands `11 SP3` as a full release identity.) Also scored against each release's `latest.name` (Windows' raw NT build, e.g. `10.0.28000`, since the release `name`/`label` is a marketing slug that never contains it). When a build is shared by multiple Windows editions/channels (IoT LTS, Enterprise, Enterprise LTSC, consumer), an edition word in the OS string (`Enterprise`, `(E)`, `IoT`) narrows to that edition first; any remaining tie (or no edition named) takes the **earliest** EOL/EOAS among the tied releases — never the longest possible support window.
2. **If the API returned dates/status** → write them (evidence `api` / `eol`). **Stop.** Vendor DBs are **not** consulted.
3. **If the API missed (or failed)** → call **Vendor Lookups** in fixed order:
   - **eosl** (if enabled) → evidence `eosl`
   - **microsoft-lifecycle** (if enabled) → evidence `microsoft-lifecycle`
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

| Situation | eosl | Microsoft Lifecycle | Junos | SUSE | Layer23-Switch | Router-Switch |
|-----------|------|----------------------|-------|------|----------------|---------------|
| API hit | No | No | No | No | No | No |
| API miss + source enabled | Yes (2nd) | Yes (3rd) | If keywords match (4th) | If keywords match (5th) | If enabled+keywords (6th; off by default) | If enabled+keywords (7th; off by default) |
| Source disabled in Settings | No | No | No | No | No | No |
| Update scrape only | No write | No write | No write | No write | No write | No write |

Example: `SUSE Linux 11 SP3` → API miss → eosl miss → SUSE keywords match → SUSE DB → evidence `suse`.

Dates are stored as Unix epoch. Status `true`/`false` is only used when a date is missing.

---

## Vendor Lookups (PostgreSQL caches)

Umbrella for **offline** lifecycle scrapes used as the Refresh fallback above. The **Vendor lookups** screen is read-only for browsing (Source selector, filterable viewer) — it does not write into your lookup.

| Source | Origin | Postgres schema | Date mapping | Used on Refresh when… |
|--------|--------|-----------------|--------------|------------------------|
| **eosl.date** | [eosl.date](https://eosl.date) OS category | `eosl` | EOAS = earliest support date, EOL = latest | API missed **and** source enabled (2nd) |
| **Microsoft Lifecycle** | [learn.microsoft.com/lifecycle/products](https://learn.microsoft.com/en-us/lifecycle/products/) | `microsoft_lifecycle` | API `end` → `eol_date` (single date only; `eoas_date` blank) | API+eosl miss, enabled (3rd) |
| **Juniper Junos** | [Junos Dates & Milestones](https://support.juniper.net/support/eol/software/junos/) | `junos` | **EOE → `eol_date`**, **EOS → `eoas_date`**, FRS → released | prior miss, enabled, keywords match (4th) |
| **SUSE Lifecycle** | [suse.com/lifecycle](https://www.suse.com/lifecycle/) | `suse` | **General Ends → `eol_date`**, **LTSS Ends → `eoas_date`**, FCS → released | prior miss, enabled, keywords match (5th) |
| **Layer23-Switch EOL** | [layer23-switch.com EOL tool](https://www.layer23-switch.com/eol-eosl-tool/) | `layer23_switch` | **EOL Announcement → `eol_date`**, **EOSL → `eoas_date`**, EOS → released | prior miss, enabled, keywords match (6th; **off by default**) |
| **Router-Switch EOL** | [router-switch.com EOL checker](https://www.router-switch.com/eol-eosl-checker/) | `router_switch` | **EOL Announcement → `eol_date`**, **EOSL → `eoas_date`**, EOS → released | prior miss, enabled, keywords match (7th; **off by default**) |

Per-source enable + family keywords are edited under **Settings → Vendor lookups** and saved to `_config/vendor_lookup_settings.json`. Each **Update** runs as a cancellable background task (`vendor-sync:{source}`).

### eosl.date notes

- Support-column labels vary; any non-metadata date column feeds earliest/latest EOAS/EOL.
- Strong product **and** release score required; vague `Other … Linux` / bitness / `N.x` false matches are rejected.
- Requests are throttled; scrapes are serialized server-side.

### Microsoft Lifecycle notes

- Backed by the JSON API behind [learn.microsoft.com/lifecycle/products](https://learn.microsoft.com/en-us/lifecycle/products/) (`/api/contentbrowser/search/lifecycles`), not HTML scraping — paginated at the API's max `$top=30` per page (~800 products, ~28 requests).
- Product family (Windows, Office, SQL Server, Visual Studio, Dynamics, Azure, .NET, System Center, Microsoft Servers, Internet Explorer, Microsoft Edge, Microsoft 365, Silverlight, Expression, Customer Care Framework, Connected Services Framework) → local `product`; each named product (e.g. `SQL Server 2025`) → local `release`.
- The API's `end` already matches the Extended End Date shown on each product's own lifecycle page, so it is used directly as `eol_date`; only one date is available per product, so `eoas_date` is left blank.
- No keyword gate (like eosl) — resolution relies on matching a specific Microsoft product name/version, then the same vendor-compatibility check every source uses.

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
- Manufacturer selection is stored in `_config/router_switch_sync.json`.
- Site is behind Cloudflare; sync uses `curl_cffi` Chrome TLS impersonation. Full sync is large (Cisco alone is ~2k pages) and can take a long time — run it as a background task and keep working elsewhere.

### Layer23-Switch notes

- Scrapes paginated manufacturer lists under [layer23-switch.com/eol-eosl-tool](https://www.layer23-switch.com/eol-eosl-tool/) (same manufacturer set as Router-Switch).
- **EOL Announcement → `eol_date`**, **End of Service Life (EOSL) → `eoas_date`**, End of Sale (EOS) → released.
- Wired into Refresh **before Router-Switch**, but **disabled by default**. Enable + keywords under **Settings**.
- Manufacturer selection is stored in `_config/layer23_switch_sync.json`.
- Site is behind Cloudflare; sync uses `curl_cffi` Chrome TLS impersonation.

---

## Evidence (proof)

Per-row evidence of how each field was filled, keyed by `os_string`, stored in the `lookup.evidence` table (one JSON payload per source):

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

Proof methods include: `fuzzy`, `ai`, `fuzzy+ai`, `eol` / `api`, `eosl`, `microsoft-lifecycle`, `junos`, `suse`, `layer23-switch`, `router-switch`, `lookup-fallback`, `ambiguous`, `manual`, `none`.

The row detail drawer's **Matched by** field, and the column filters panel's **Matched by** chip row, group these into: `All`, `endoflife.date`, `Fuzzy`, `AI`, `eosl.date`, `Microsoft Lifecycle`, `Juniper Junos`, `SUSE Lifecycle`, `Manual`, `Ambiguous`, `No match`.

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

## Publish safety: staleness

Covered in depth in [How the lookup data is stored](#how-the-lookup-data-is-stored). In short, from the editor's point of view:

1. Clicking **Validate & publish** immediately checks staleness (`POST /api/lookup/validate/check`) before showing you anything else.
2. You see the usual KPI tiles (new / edited / still-unresolved rows) and an optional backup-name suffix.
3. The actual publish then runs as a background task like any other. If Data was published again since your Draft's expected revision, it's rejected outright (409) instead of overwriting — reload and reapply your changes.

While you're on Data (not drafting) or sitting in an open Draft, a banner appears if Data has moved since you last knew about it — a reload prompt on the Data view, a reassurance note in Draft.

```mermaid
flowchart LR
  Edit[Edit in Draft] --> Save[Auto-save / Save Draft]
  Save --> Check[Validate and publish: check staleness]
  Check --> Publish[Publish: revision-guarded transaction, delete Draft]
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
- There's no local file to hand the CLI directly — the app exports the current Data to a throwaway temp CSV for the upload and cleans it up afterward.

---

## Main API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/lookup` | Load rows + evidence for a source (`data`/`draft`); includes `data_revision` and, for `draft`, `based_on_revision` |
| `POST` | `/api/lookup` | Save rows + evidence to a source; on first-ever draft save (or an explicit reset from Revert), captures the merge base |
| `GET` | `/api/lookup/evidence` | Evidence for one `os_string` |
| `GET` | `/api/lookup/diff` | Added / edited / deleted / unresolved counts, Draft vs Data |
| `POST` | `/api/lookup/validate/check` | No-write preview of a publish: reports staleness if Data moved since the draft's expected revision |
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
- **Publish never blindly overwrites** — the revision-guarded transaction exists specifically so publishing in parallel with someone else never silently discards their already-published work; see [Publish safety](#publish-safety-staleness).
- **Duplicate `os_string` values are never silently collapsed** — the diff treats a real duplicate as something to look at, not something to dedupe by picking one arbitrarily.
