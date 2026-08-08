# OS Health Check — User Guide

A step-by-step guide to using the app. For install, Docker, and `.env` setup, see [README.md](../README.md).

---

## What this app does

OS Health Check helps you maintain a lookup table that:

1. **Normalizes** raw operating system names from inventory (for example `Oracle Linux Server 9.5` → `Oracle Linux 9`)
2. Fills **EOL** (end of life) and **EOAS** (end of active support) dates
3. Keeps a short **evidence** trail of how each value was filled

You work safely in a **Draft**, then **Validate & publish** when you are ready to promote it into **Data**. Long-running work (refreshing dates, adding rows, publishing, updating vendor caches, uploading to the cloud) all runs as a trackable, mostly-cancellable **background task**, so you never have to sit and wait on one screen.

---

## First open

1. Start the app (see [README.md](../README.md)).
2. Open **http://127.0.0.1:8000** in your browser.
3. You land on **Lookup editor**, mode **Data** (the read-only published lookup).

The left rail is your main navigation:

| Section | Screen | What it's for |
|---------|--------|----------------|
| Lookup | **Lookup editor** | Browse, filter, sort, edit, and publish the lookup |
| Sources | **Vendor lookups** | Browse and rebuild the local vendor lifecycle caches |
| Publish | **Deploy** | Upload the published lookup to Azure Blob / AWS S3 |
| Activity | **Background tasks** | Everything running or recently finished |
| *(bottom)* | **Settings** | Vendor lookups config, Configure AI, Appearance |

Click the collapse arrow at the top of the rail to shrink it to icons only. The topbar shows when the lookup was last published, and a bell icon that lights up when a background task finishes while you're on another screen.

**Theme & density**: set light/dark and compact/comfortable row spacing in **Settings → Appearance** — these are saved per-browser, not shared with anyone else.

---

## Data vs Draft

| | **Data** | **Draft** |
|--|----------|-----------|
| Purpose | Published lookup everyone trusts | Your working copy |
| Editing | No | Yes |
| Typical actions | Browse, filter, sort, Export, Deploy | Add OS, edit cells, Refresh, Validate & publish |

**Rule of thumb:** never edit Data directly. Always **Edit data** → change the Draft → **Validate & publish** when ready.

### Start editing

At the top of the Lookup editor, the mode bar has a **Data** / **Draft** segmented switch.

1. Click **Edit data** (shown in Data mode).
2. If no Draft exists, the app creates one from the current Data.
3. If a Draft already exists, the button instead reads **Resume draft** — clicking it opens your previous work exactly as you left it.

### Leave editing without publishing

| Button | What it does |
|--------|----------------|
| **Exit draft** | Go back to Data. The Draft is **kept**. |
| **Delete draft** | Permanently remove the Draft (and its evidence). Data is untouched. |
| **Revert all changes** | Reset every row in the Draft back to the current Data — the Draft itself stays, only its contents change. |

---

## Lookup editor screen tour

### Mode bar

- **Data**: **Edit data** / **Resume draft** button only.
- **Draft**: **Only changed rows** toggle, **Auto-save** toggle, a **Saved** / **Unsaved** pill, and buttons for **Save draft**, **Exit draft**, **Revert all changes**, **Delete draft**, and **Validate & publish**.

### Toolbar

- **Search** — filters across OS string and both normalized name fields.
- **Quick chips** — one-click filters, each showing a live count:
  - **All**
  - **Missing normalization**
  - **Past EOL**
  - **Past EOAS**
  - **No dates**
  - **Ambiguous**
  - **Changed** (Draft only — rows added or edited since Data)
- **Refresh EOL/EOAS**, **Add OS** (Draft only), **Export**, **Column filters** (the button shows a badge with the number of active filters).

### Column filters

Click **Column filters** to open the panel:

- **OS string**, **Normalized detailed name**, **Normalized OS** — each has a mode (All / Contains / Excludes / Equals / Empty / Not empty) and a text box.
- **EOL date**, **EOAS date** — each has a mode (All / Passed / Upcoming / Empty / Not empty), a From–To date range, and a status filter (Any / True / False).
- **Matched by** — a single-select chip row: All, endoflife.date, Fuzzy, AI, eosl.date, Microsoft Lifecycle, Juniper Junos, SUSE Lifecycle, Manual, Ambiguous, No match.

Changing any filter clears your current row selection. **Clear** resets everything in the panel at once.

### The table

All 7 columns are sortable — click a header to sort ascending, click again for descending, click a third time to go back to unsorted:

| Column | Meaning |
|--------|---------|
| **OS string** | Raw inventory string |
| **Normalized detailed name** | Long normalized name |
| **Normalized OS** | Short normalized name |
| **EOL date** / **EOL status** | End of life |
| **EOAS date** / **EOAS status** | End of active support |

In Draft mode a checkbox column appears on the left for bulk selection. Empty cells (no date, no status, no normalized name) show as a dotted **none** chip rather than plain italic text — that's intentional, it means "genuinely blank," not "unknown."

Use the footer to change rows-per-page (50 / 100 / 250 / 500 / 1000) and page through results.

### Row detail drawer

Click any row (when nothing is selected) to open its detail drawer:

- **Normalized detailed name**, **Normalized OS**, **EOL date/status**, **EOAS date/status** — editable in Draft, read-only on Data.
- **Matched by** — always read-only; shows how the row's values were derived.
- **Evidence** — the full trail of methods used to fill this row.

Draft-only row actions:

| Action | What it does |
|--------|----------------|
| **Same as OS** | Copies the OS string into both normalized fields (collapsing any immediately-repeated word first, e.g. `Linux Linux 2.6.32` → `Linux 2.6.32`), and clears EOL/EOAS |
| **Re-run lookup** | Re-queries lifecycle sources for just this row |
| **Revert row** | Resets every field except the OS string back to the published Data value |

### Bulk actions

Select one or more rows in Draft mode (checkbox column) to reveal the bulk bar: **Refresh lifecycle**, **Same as OS**, **Revert to Data**, **Export selection**, **Delete**, and **Clear** (to drop the selection). Selecting anything disables single-row-click-to-open — click a row's checkbox specifically to add/remove it from the selection instead.

---

## Everyday workflows

### 1. Browse and find rows (Data or Draft)

1. Use **Search** for a quick text filter, or a **quick chip** for a common one.
2. Open **Column filters** for anything more specific, including the **Matched by** evidence filter.
3. Click any column header to sort by it.
4. Change rows-per-page and page through with **Previous** / **Next**.

### 2. Add one operating system (Draft)

1. Click **Edit data** if you're still on Data.
2. Click **Add OS** → the **Single OS** tab is selected by default.
3. Type the OS string → **Add OS**.
4. The app, as a background task:
   - Detects **Ambiguous OS** when `/` separates multiple products
   - **Fuzzy-matches** to an existing normalized pair (high confidence)
   - Optionally uses **AI match** if fuzzy fails and AI is enabled
   - Looks up EOL/EOAS dates
5. Review the new row; fix cells manually if needed.

You can close the progress dialog or switch screens while this runs — check **Background tasks** to see it finish, or to cancel it.

### 3. Add many operating systems (Draft)

1. Click **Add OS**, then choose a tab:
   - **Paste list** — one OS string per line (duplicates are skipped automatically)
   - **CSV / Excel** — drop or choose a file, then pick which column(s) hold OS names
2. Click **Add OS**.
3. Watch progress — for a large batch, it advances in real chunks rather than sitting still, and you can **Cancel** at any point without losing rows already added before the cancel.

### 4. Edit a row (Draft)

- Click a row to open its detail drawer and edit fields there, or edit inline where the table supports it.
- Use the drawer's **Same as OS**, **Re-run lookup**, or **Revert row** as needed (see [Row detail drawer](#row-detail-drawer) above).

With **Auto-save** on (the default), wait for the pill to show **Saved**. Otherwise click **Save draft**.

### 5. Review what changed (Draft)

Click the **Changed** quick chip to show only rows that are new or edited relative to Data. New rows and edited rows are visually flagged in the table. **Validate & publish**'s KPI tiles (see below) also summarize new/edited/still-unresolved counts before you commit to publishing.

### 6. Sort and filter together

Sorting and filtering compose freely — apply a quick chip or column filter first to narrow the rows, then click a column header to order what's left. Sort persists as you page through results.

### 7. Refresh EOL / EOAS dates

1. Click **Refresh EOL/EOAS** (works from Data or Draft — if you're on Data, this opens/creates a Draft first).
2. Confirm in the dialog. If a Draft already exists, it's reused — your in-progress work isn't wiped.
3. Progress runs as a background task; cancel it any time from the dialog or from **Background tasks**.

**How dates are chosen (per row):**

1. **endoflife.date** API first
2. If that misses → enabled **Vendor Lookups** in fixed order: eosl → microsoft-lifecycle → junos → suse → layer23-switch → router-switch
3. If still missing → copy from another row with the same normalized pair, when possible
4. Otherwise leave blank

Blank OS and **Ambiguous OS** rows are skipped. Refresh only fills lifecycle dates — it does not re-run fuzzy/AI matching on the normalized name fields; if those are wrong, fix them with **Same as OS**, by hand, or by re-adding the row through Add OS.

Configure which vendor sources run under **Settings → Vendor lookups**.

### 8. Publish your work (Validate & publish)

When the Draft looks right:

1. Click **Validate & publish**.
2. The app immediately checks whether anyone else has published changes since you started this Draft.
   - **No conflicts**: you see KPI tiles (**New rows**, **Edited rows**, **Still unresolved**) and an optional **Backup name suffix** field. Click **Validate and publish** to confirm.
   - **Conflicts found** (someone else changed the *same* row you did, differently — the most common cause is two people each running Refresh and publishing around the same time): a resolver appears instead, listing each conflicting row with **Keep mine** and **Keep theirs** side by side. **Keep theirs** is pre-selected by default (the already-published version is usually the more current one), but you can flip individual rows, or use **Keep mine for all** / **Keep theirs for all** to apply one choice everywhere. The button reads **Resolve & publish** and stays disabled until every conflict has a choice.
3. Confirm. Publishing itself (backup → write → delete draft) can't be cancelled once started — it's a single atomic step with no safe pause point — but you can watch its progress in **Background tasks**.

What happens on a successful publish:

1. Current Data is backed up.
2. Your Draft's changes are merged in (automatically, for anything that doesn't conflict; per your choices, for anything that does).
3. The Draft is deleted.
4. You return to **Data** mode.

### 9. Undo Draft work

| Goal | Action |
|------|--------|
| Reset **one** row to Data | Drawer's **Revert row** |
| Reset the **whole** Draft to Data | Mode bar's **Revert all changes** |
| Throw away the Draft entirely | **Delete draft** |

### 10. Notice when Data changes under you

A banner can appear at the top of the app:

- **While viewing Data** (not drafting): "Data has been updated since you loaded this page" with a **Reload** button — someone else published while your tab was open.
- **While in a Draft**: a reassurance note that Data was published again since you started, and that publishing will merge it in automatically and only ask about rows you both touched. You can keep editing — nothing forces you to stop.

Dismissing the banner hides it until Data changes again.

---

## Background tasks

Open **Background tasks** from the rail to see everything currently running (**Active** tab) or finished (**History** tab, most recent first, capped at the last 40).

- Each active task shows its current stage, a progress bar, a short live log, and — for anything cancellable — a **Cancel** button.
- **Validate & publish** is the one task type that never shows Cancel (there's no safe point to stop a backup→write→delete-draft sequence partway through).
- Finished tasks in **History** show whether they **Succeeded**, **Failed** (with the error message), or were **Cancelled**, plus a **Dismiss** button and a **Clear history** button for the whole list.
- The topbar bell lights up whenever a task completes while you're looking at a different screen — click it for a quick dropdown, or go to Background tasks for the full picture.
- You can't start the same kind of task twice at once (e.g. two refreshes) — you'll get a toast telling you one is already running.

---

## Vendor lookups screen

Read-only browser for the local lifecycle caches used as Refresh's fallback. It does **not** write into your lookup — only **Refresh EOL/EOAS** applies dates to rows.

1. Open **Vendor lookups**.
2. Choose a **Source**: eosl.date, Microsoft Lifecycle, Juniper Junos, SUSE Lifecycle, Layer23-Switch EOL, or Router-Switch EOL.
3. Search and filter the table the same way as the main editor.
4. Click **Update** to re-scrape and rebuild that source's local database — this runs as a background task too, so you can navigate away and check back later.
5. For Layer23-Switch / Router-Switch, pick which manufacturers to pull before updating (these can be large — full syncs can take a while).

After an update finishes, run **Refresh EOL/EOAS** on your Draft so the new cache data can actually fill dates.

---

## Settings

Open **Settings** from the rail. Three tabs:

### Vendor lookups

Controls the local sources used **after** endoflife.date during Refresh.

- The refresh-order strip shows the fixed sequence: endoflife.date → eosl → microsoft-lifecycle → junos → suse → layer23-switch → router-switch.
- Toggle each source on or off.
- For keyword-gated sources, edit **family keywords** (add one via the **+ Add keyword** button, remove with the × on a keyword chip).
- **eosl** and **Microsoft Lifecycle** have no keyword gate (run whenever enabled).
- **Layer23-Switch** and **Router-Switch** are **off by default** (large hardware catalogs).

### Configure AI

- **AI match** toggle — off by default; no AI calls happen at all until this is on.
- **Provider** chips — OpenAI, Gemini, OpenRouter; each shows its currently selected model. Providers without a configured API key appear unavailable.
- **Model** dropdown — pick from a curated list for the active provider, or **Add custom model…** to type any model id (useful for OpenRouter, whose catalog is far larger than any curated list). **Reset to default** restores the provider's built-in default.
- **Confidence cutoff** — a slider (50–100%, default 85%) for how sure an AI match has to be before it's accepted. Drag to preview, release to save.
- **AI match system prompt** — edit the plain-language matching rules, or **Reset to default**. Use `{threshold}` where the confidence cutoff should appear; you never need to write the JSON output format yourself, the app appends that automatically.

### Appearance

- **Theme** — Light or Dark.
- **Row density** — click the value chip to toggle between Compact and Comfortable.

Both Appearance settings are saved in your browser only (not shared with other users of the same server).

---

## Deploy (Azure / AWS)

Available on **Data** only — it publishes the validated lookup, not a Draft.

1. Click **Deploy**.
2. Choose a provider card: **Azure Blob** or **AWS S3**.
3. Pick or create a profile:
   - **Azure**: Storage account, Container, Blob path
   - **AWS**: Bucket, Region, Key
4. **+ New profile** opens a small dialog for the profile name (no browser popups). **Save profile** to keep your edits.
5. Click **Upload to Azure Blob** / **Upload to AWS S3** — uses the CLI (`az login` / `aws configure`) authenticated on the machine running the app. This runs as a cancellable background task.
6. **Delete profile** removes a profile after confirmation.

---

## Suggested end-to-end path

For a typical update session:

1. Open the app → confirm you're on **Data**.
2. **Vendor lookups** → **Update** the sources you rely on (optional but useful; runs in the background).
3. Click **Edit data**.
4. **Add OS** for new inventory strings — single, pasted list, or CSV/Excel.
5. Turn on **AI match** in **Settings → Configure AI** if fuzzy misses are common and you have a key configured.
6. Click **Refresh EOL/EOAS**.
7. Use the **Changed** quick chip and each row's **Evidence** to review what happened.
8. Fix anything wrong by hand, or with **Same as OS** / **Re-run lookup**.
9. Click **Validate & publish** → resolve any conflicts shown → confirm.
10. (Optional) **Deploy** → upload to Azure or AWS.

---

## Quick reference: buttons

| Button | Mode | Meaning |
|--------|------|---------|
| Edit data / Resume draft | Data | Open or create a Draft |
| Exit draft | Draft | Return to Data; keep the Draft |
| Save draft | Draft | Save now |
| Validate & publish | Draft | Check for conflicts, resolve if needed, publish Draft → Data, backup old Data, delete Draft |
| Revert all changes | Draft | Reset every row in the Draft to Data |
| Delete draft | Draft | Discard the Draft permanently |
| Refresh EOL/EOAS | Both | Fill dates (uses/creates a Draft) |
| Add OS | Draft | Insert one or many rows |
| Export | Both | Download the current filtered view as CSV |
| Column filters | Both | Open the filters panel |
| Deploy | Data | Upload Data to Azure or AWS |
| Settings | Both | Vendor lookups / Configure AI / Appearance |
| Background tasks | Both | Track and cancel long-running work |

---

## Common questions

**Why can't I edit the table?**
You're on **Data**. Click **Edit data**.

**I clicked Exit draft — where did my work go?**
Still in the **Draft**. Switch the mode bar to **Draft**, or click **Resume draft**.

**Validate & publish deleted my Draft — is that normal?**
Yes. Publishing promotes the Draft into Data and removes the Draft. The previous Data was backed up first.

**I hit a conflict I didn't expect during publish.**
Someone else published a change to a row you also changed. Pick **Keep mine** or **Keep theirs** per row (or bulk-apply one choice), then confirm. If the row's lifecycle dates are the disagreement, "Keep theirs" (the default) is usually right — it means the already-published version was refreshed more recently.

**Refresh did not fill some dates.**
Dates may be missing from endoflife.date and vendor caches, or the row may be Ambiguous / blank. Update Vendor Lookups, check Settings toggles/keywords, then Refresh again.

**AI match does nothing.**
Confirm the toggle is on in **Settings → Configure AI**, a provider with a configured key is selected, and the row didn't already fuzzy-match. Without a key, only fuzzy matching runs.

**Where do I change which AI model is used?**
**Settings → Configure AI → Model** dropdown, per provider. `.env`'s `*_MODEL` variables are just the fallback default.

**Deploy failed.**
Azure upload needs the Azure CLI (`az login`); AWS upload needs the AWS CLI (`aws configure`) — both on the machine running the app. Profiles must be complete (all fields filled).

**A background task is stuck "running" and I don't see progress.**
Open **Background tasks** to check its live log and stage. Most tasks can be cancelled from there; **Validate & publish** cannot be cancelled once started (by design — see [Background tasks](#background-tasks)).

**What is Ambiguous OS?**
A string like `AIX 5.x / AIX 6.x` that lists multiple products. The app will not invent a single normalization; fill those carefully by hand.

**I saw a "Data has been updated" banner.**
Someone else published while you had the page open. On Data, click **Reload**. In a Draft, you can keep working — the note is just letting you know; the merge at publish time handles it safely.

---

## Need setup help?

Installation, Docker, PostgreSQL, and AI API keys are documented in [README.md](../README.md).
