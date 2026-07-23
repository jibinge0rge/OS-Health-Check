# OS Health Check — User Guide

A step-by-step guide to using the **Lookup Editor**. For install, Docker, and `.env` setup, see [README.md](README.md).

---

## What this app does

OS Health Check helps you maintain a lookup table that:

1. **Normalizes** raw operating system names from inventory (for example `Oracle Linux Server 9.5` → `Oracle Linux 9`)
2. Fills **EOL** (end of life) and **EOAS** (end of active support) dates
3. Keeps a short **evidence** trail of how each value was filled

You work safely in a **Draft**, then **Validate** when you are ready to publish into **Data**.

---

## First open

1. Start the app (see [README.md](README.md)).
2. Open **http://127.0.0.1:8000** in your browser.
3. You land on **Lookup Editor** with **Source = Data** (read-only published lookup).

At the top you will see summary cards (totals, missing normalization, EOL/EOAS counts) and the main table.

---

## The two modes: Data vs Draft

| | **Data** | **Draft** |
|--|----------|-----------|
| Purpose | Published lookup everyone trusts | Your working copy |
| Editing | No | Yes |
| Typical actions | View, Download, Deploy, Settings, Vendor Lookups | Add OS, edit cells, Refresh, Validate |

**Rule of thumb:** never edit Data directly. Always **Edit Data** → change the Draft → **Validate** when ready.

### Start editing

1. Click **Edit Data**.
2. If no Draft exists, the app copies Data into a new Draft.
3. If a Draft already exists, it opens that Draft (your previous work is kept).

You can also use the **Source** dropdown:

- **Data** — published view
- **Draft** — editable view (disabled until a draft exists)

### Leave editing without publishing

| Button | What it does |
|--------|----------------|
| **Exit Draft** | Go back to Data. The Draft file is **kept**. |
| **Delete draft** | Permanently remove the Draft and return to Data. |

---

## Screen tour

### Top bar (hero actions)

**Always available**

- **Refresh EOL/EOAS** — fill or refresh lifecycle dates
- **View / Update Vendor Lookups** — browse and rebuild local vendor databases
- **Settings** (gear icon) — vendor fallback options and AI match prompt

**Data only**

- **Edit Data** — enter Draft
- **Deploy** — upload published Data to Azure

**Draft only**

- **Auto-save** — saves shortly after you edit (on by default)
- **AI match** — optional AI help when fuzzy match fails
- **Provider** — OpenAI / Gemini / OpenRouter
- **Save Draft** — save now
- **Exit Draft** / **Validate** / **Revert** / **Delete draft**

### Toolbar (above the table)

- **Search** — filter any column
- **Add OS** / **Add multiple OS** (Draft)
- **Download** — export current view (respects filters)
- **Show Delta** / **Download Delta** (Draft) — focus on new or changed rows vs Data
- **Source** — Data or Draft
- Status pills — source name, row count, **Saved** / **Unsaved** / **Saving...**

### Summary cards

- Total Operating Systems  
- Missing Normalization  
- EOL / EOAS counts in the current filter  
- Shown Operating Systems (after filters)

### Main table columns

| Column | Meaning |
|--------|---------|
| **OS** | Raw inventory string |
| **Normalized OS Detailed Name** | Long normalized name |
| **Normalized OS** | Short normalized name |
| **EOL Date** / **EOL Status** | End of life |
| **EOAS Date** / **EOAS Status** | End of active support |
| **Actions** | Row tools + evidence filter |

Dates can be empty. Status (`True` / `False`) is mainly used when a date is missing.

---

## Everyday workflows

### 1. Browse and find rows (Data or Draft)

1. Use **Search** for a quick text filter.
2. Use each column’s filter (All / Empty / Contains / Equals, and date From–To).
3. In **Actions**, filter by how the row was matched (Fuzzy, AI, EOL API, eosl.date, and so on).
4. Click **Clear** in the Actions filter area to reset those filters.
5. Change **Show … per page** and use **Previous** / **Next**.

### 2. Add one operating system (Draft)

1. Click **Edit Data** if you are still on Data.
2. Click **Add OS**.
3. Type the OS string → **Add OS**.
4. The app tries to:
   - Detect **Ambiguous OS** when `/` separates multiple products
   - **Fuzzy-match** to an existing normalized pair (high confidence)
   - Optionally use **AI match** if fuzzy fails and AI is enabled
   - Look up EOL/EOAS dates
5. Review the new row. Fix cells manually if needed.

Duplicates (same OS string) are skipped.

### 3. Add many operating systems (Draft)

1. Click **Add multiple OS**.
2. Choose a tab:
   - **Paste list** — one OS per line
   - **CSV / Excel** — **Choose file**, then pick which columns hold OS names (**Select all** / **Clear**)
3. Click **Add OS entries**.
4. Watch progress in **Adding operating systems**. Use **Cancel** to stop.

### 4. Edit a row (Draft)

- Click a cell to edit text.
- Use the **Date** button on date cells.
- Use **True** / **False** on status cells.
- Row actions:
  - **Same as OS** — copy the OS into both normalized fields and clear EOL/EOAS
  - **Evidence** — see how values were filled
  - **Revert** (row) — restore that OS to the Data version (or remove it if it was new)
  - **Delete** — remove the row after confirm

With **Auto-save** on, wait for the pill to show **Saved**. Otherwise click **Save Draft**.

### 5. Review what changed (Draft)

1. Orange / new markers highlight rows that differ from Data.
2. Click **Show Delta** to list only new or updated rows (button becomes **Showing Delta**).
3. Click **Download Delta** to export those rows.

### 6. Refresh EOL / EOAS dates

1. Click **Refresh EOL/EOAS** (works from Data or Draft).
2. Confirm **Refresh lifecycle data?**
3. If you started on Data:
   - Existing Draft is reused when present (so draft work is not wiped)
   - Otherwise a new Draft is created from Data
4. Progress shows **Refreshing EOL/EOAS data**.

**How dates are chosen (per row)**

1. **endoflife.date** API first  
2. If that misses → enabled **Vendor Lookups** in fixed order:  
   eosl → junos → suse → layer23-switch → router-switch  
3. If still missing → copy from another row with the same normalized pair when possible  
4. Otherwise leave blank  

Blank OS and **Ambiguous OS** rows are skipped.

Configure which vendor sources run under **Settings → Vendor lookups**.

### 7. Publish your work (Validate)

When the Draft looks right:

1. Click **Validate** (Draft is auto-saved if needed).
2. Read **Write Draft to EOL Lookup?**
3. Optionally add a **Backup name suffix**.
4. Confirm with **Validate and delete draft**.

What happens:

1. Current Data is backed up  
2. Draft becomes the new Data  
3. Draft is deleted  
4. You return to Source **Data**

### 8. Undo Draft work

| Goal | Action |
|------|--------|
| Reset **one** row to Data | Row **Revert** |
| Reset **whole** Draft to Data | Toolbar **Revert** (saves Draft immediately) |
| Throw away the Draft entirely | **Delete draft** |

---

## Evidence (proof)

Click **Evidence** on a row to open **Evidence — How this row was filled**.

Typical methods:

| Method | Meaning |
|--------|---------|
| Fuzzy | Matched an existing normalized pair by similarity |
| AI / Fuzzy + AI | AI helped choose a pair |
| Manual | Edited by hand |
| EOL API | Dates from endoflife.date |
| eosl.date / Junos / suse / Layer23 / Router-Switch | Dates from a local vendor cache |
| Lookup copy | Copied from another row with the same normalized pair |
| Ambiguous | Multi-product `/` string; not auto-normalized |
| NULL / none | No match or no dates |

Evidence is stored next to the CSV on the server. It is **not** a CSV column.

---

## AI match

AI is optional. Fuzzy matching works without any API key.

1. Enter **Draft**.
2. Turn on **AI match**.
3. Choose **Provider** (OpenAI, Gemini, or OpenRouter).  
   Options without a configured key appear unavailable (for example **OpenAI (no key)**).
4. When you add OS values, AI runs only if fuzzy match fails.

Keys are set by your administrator in `.env` (see [README.md](README.md)).

### Customize the AI prompt

1. Open **Settings** (gear).
2. Open the **AI match prompt** tab.
3. Edit the plain-language rules, or click **Reset to default**.
4. Click **Save**.

Use `{threshold}` where the confidence cutoff should appear. You do **not** need to write JSON or `pair_index` — the app adds that automatically.

---

## Settings

Open the gear → **Settings**. Two tabs:

### Vendor lookups

Controls local sources used **after** endoflife.date during Refresh.

- Flow chips show order: **endoflife.date → eosl → junos → suse → layer23-switch → router-switch**
- Toggle each source on or off
- For keyword-gated sources, edit **Family keywords** (Add keyword / remove chips)
- **eosl** has no keyword gate (runs whenever enabled)
- Layer23-Switch and Router-Switch are **off by default** (large hardware catalogs)

Click **Save** when done.

### AI match prompt

See [Customize the AI prompt](#customize-the-ai-prompt) above.

---

## View / Update Vendor Lookups

This window is **read-only** for browsing. It does **not** write into your CSV. Refresh EOL/EOAS is what applies dates to the lookup.

1. Click **View / Update Vendor Lookups**.
2. Choose a **Source**:
   - eosl.date  
   - Juniper Junos  
   - SUSE Lifecycle  
   - Layer23-Switch EOL  
   - Router-Switch EOL  
3. Search and filter the table like the main editor.
4. Click **Update** to scrape and rebuild that source’s local database.
5. For Layer23 / Router-Switch, pick **Manufacturers to pull** (**All** / **None** or checkboxes) before Update.
6. Use **Stop** if a long update should be cancelled.

After Update finishes, run **Refresh EOL/EOAS** on your Draft (or Data → Draft) so new cache data can fill dates.

---

## Deploy (Azure)

Available on **Data** only (publishes the validated lookup, not a Draft).

1. Click **Deploy**.
2. Choose **Azure** (AWS / GCP show as Coming soon).
3. Pick or create an **Active profile**:
   - Profile name  
   - Storage account name  
   - Container name  
   - Blob path in container  
4. **Save profile** to keep it.
5. **Upload to Azure** — uses the Azure CLI account signed in on the host.
6. Watch **Uploading to Azure**; **Cancel** or **Close** when finished.

**Delete** removes a profile after confirm.

---

## Download files

| Button | Result |
|--------|--------|
| **Download** | Current source rows → `eol_lookup.csv` (or `eol_lookup_filtered.csv` if filters are active) |
| **Download Delta** | Only new/changed Draft rows vs Data |

Filters (including evidence filter) apply to both downloads.

---

## Suggested end-to-end path

For a typical update session:

1. Open the app → confirm you are on **Data**.  
2. **View / Update Vendor Lookups** → Update sources you rely on (optional but useful).  
3. Click **Edit Data**.  
4. **Add OS** or **Add multiple OS** for new inventory strings.  
5. Turn on **AI match** if keys are configured and fuzzy misses are common.  
6. Click **Refresh EOL/EOAS**.  
7. Use **Show Delta** and **Evidence** to review.  
8. Fix anything wrong by hand.  
9. Click **Validate** → **Validate and delete draft**.  
10. (Optional) **Deploy** → Upload to Azure.

---

## Quick reference: buttons

| Button | Mode | Meaning |
|--------|------|---------|
| Edit Data | Data | Open / create Draft |
| Exit Draft | Draft | Return to Data; keep Draft |
| Save Draft | Draft | Save now |
| Validate | Draft | Publish Draft → Data, backup old Data, delete Draft |
| Revert | Draft | Reset entire Draft to Data and save |
| Delete draft | Draft | Discard Draft permanently |
| Refresh EOL/EOAS | Both | Fill dates (uses/creates Draft) |
| View / Update Vendor Lookups | Both | Browse/rebuild vendor caches |
| Deploy | Data | Upload Data to Azure |
| Settings | Both | Vendor + AI prompt |
| Add OS / Add multiple OS | Draft | Insert rows |
| Show Delta / Download Delta | Draft | Review/export changes |
| Download | Both | Export current view |

---

## Common questions

**Why can’t I edit the table?**  
You are on **Data**. Click **Edit Data**.

**I clicked Exit Draft — where did my work go?**  
It is still in **Draft**. Open **Source → Draft** or **Edit Data** again.

**Validate deleted my Draft — is that normal?**  
Yes. Validate publishes Draft into Data and removes the Draft. The previous Data was backed up first.

**Refresh did not fill some dates.**  
Dates may be missing from endoflife.date and vendor caches, or the row may be Ambiguous / blank. Update Vendor Lookups, check Settings toggles/keywords, then Refresh again.

**AI match does nothing.**  
Confirm the toggle is on, a Provider with a key is selected, and the row did not already fuzzy-match. Without a key, only fuzzy matching runs.

**Deploy failed.**  
Azure upload needs Azure CLI (`az login`) on the machine running the app. Profiles must be complete (account, container, blob path).

**What is Ambiguous OS?**  
A string like `AIX 5.x / AIX 6.x` that lists multiple products. The app will not invent a single normalization; fill those carefully by hand.

---

## Need setup help?

Installation, Docker, PostgreSQL, and AI API keys are documented in [README.md](README.md).
