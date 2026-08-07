// Lookup editor: mode bar, toolbar, bulk bar, table, footer.

import { state, setState, rows as currentRows, isDraft, isData } from "./state.js";
import { api, streams } from "./api.js";
import { iconMarkup } from "./icons.js";
import { parseRowDate, classifyDateChip, formatRelative, toISODate } from "./date_utils.js";
import { initFiltersPanel, toggleFiltersPanel, matchesColumnFilters, activeFilterCount, clearAllColumnFilters, syncChangedFilterVisibility } from "./filters_panel.js";
import { initDrawer, openDrawer, closeDrawer, isDrawerOpenFor, refreshDrawerFields, refreshDrawerReviewedState, refreshDrawerEvidence, markDrawerFieldManual } from "./drawer.js";
import { openModal, closeModal, showToast, runProgress } from "./modals.js";
import { getTasks, hasActive } from "./tasks.js";

const CSV_HEADERS = ["os_string", "normalized_os_detailed_name", "normalized_os", "eol_date", "eol_status", "eoas_date", "eoas_status"];
const DATE_FIELDS = new Set(["eol_date", "eoas_date"]);
const FIELD_LABELS = {
  normalized_os_detailed_name: "Normalized OS detailed name",
  normalized_os: "Normalized OS",
  eol_date: "EOL date",
  eol_status: "EOL status",
  eoas_date: "EOAS date",
  eoas_status: "EOAS status",
};
const PAGE_SIZE_OPTIONS = [50, 100, 250, 500, 1000];
const EXPORT_FORMATS = [
  ["csv", "CSV"],
  ["excel", "Excel"],
  ["parquet", "Parquet"],
];

let diff = { added: [], edited: [], deleted: [], unresolved: 0 };
let addedSet = new Set();
let editedSet = new Set();
let dataByOs = new Map();
let refreshEolEnabled = true;
let page = 1;
let pageSizeIndex = 0;
let saveTimer = null;

const el = {
  modeBar: document.getElementById("mode-bar"),
  modeChip: document.getElementById("mode-chip"),
  modeHint: document.getElementById("mode-hint"),
  modeBarRight: document.getElementById("mode-bar-right"),
  segData: document.querySelector('.segment[data-mode="data"]'),
  segDraft: document.getElementById("segment-draft"),
  search: document.getElementById("search-input"),
  quickChips: document.getElementById("quick-chips"),
  addOsBtn: document.getElementById("add-os-btn"),
  refreshBtn: document.getElementById("refresh-eol-btn"),
  bulkRefreshBtn: document.getElementById("bulk-refresh-btn"),
  exportBtn: document.getElementById("export-btn"),
  columnFiltersBtn: document.getElementById("column-filters-btn"),
  filterBadge: document.getElementById("filter-count-badge"),
  bulkBar: document.getElementById("bulk-bar"),
  bulkCount: document.getElementById("bulk-count"),
  bulkSelectAllFilteredBtn: document.getElementById("bulk-select-all-filtered-btn"),
  tableHeaderRow: document.getElementById("table-header-row"),
  tableBody: document.getElementById("table-body"),
  tableTrack: document.getElementById("table-track"),
  tableEmpty: document.getElementById("table-empty"),
  footerShown: document.getElementById("footer-shown"),
  footerBreakdown: document.getElementById("footer-breakdown"),
  footerPage: document.getElementById("footer-page"),
  rowsPerPageChip: document.getElementById("rows-per-page-chip"),
  railRowCount: document.getElementById("rail-row-count"),
  publishedAtMeta: document.getElementById("published-at-meta"),
};

export async function initEditor() {
  el.columnFiltersBtn.querySelector("#sliders-icon").innerHTML = iconMarkup("sliders", { size: 14 });
  el.search.parentElement.querySelector("#toolbar-search-icon").innerHTML = iconMarkup("search", { size: 14 });
  document.getElementById("export-chevron-icon").innerHTML = iconMarkup("chevron-down", { size: 12 });
  document.getElementById("bulk-export-chevron-icon").innerHTML = iconMarkup("chevron-down", { size: 12 });

  initFiltersPanel({
    onFilterChange: () => { clearSelection(); renderAll(); },
    getChangedFields: (row) => changedFields(row),
  });
  initDrawer({
    onFieldChange: (row, field) => {
      const label = markFieldManual(row, field);
      if (label) markDrawerFieldManual(row, label);
      scheduleAutosave();
    },
    onSameAsOs: (row) => { applySameAsOs(row); scheduleAutosave(); refreshView(); },
    onMarkAmbiguous: (row) => { applyAmbiguous(row); scheduleAutosave(); refreshView(); refreshDrawerFields(row); },
    onRerun: (row) => rerunRow(row),
    onRevert: (row) => { revertRow(row); scheduleAutosave(); refreshView(); refreshDrawerFields(row); },
    onClearFields: (row) => { clearRowFields(row); scheduleAutosave(); refreshView(); refreshDrawerFields(row); },
    onToggleReviewed: (row) => { toggleRowReviewed(row); scheduleAutosave(); refreshDrawerReviewedState(row); refreshView(); },
    isReviewed: (row) => isRowReviewed(row),
    isChanged: (row) => isChangedRow(row),
    getChangedFields: (row) => changedFields(row),
  });

  el.segData.addEventListener("click", () => switchSource("data"));
  el.segDraft.addEventListener("click", () => { if (state.draftExists || isDraft()) switchSource("draft"); });

  el.search.addEventListener("input", () => { state.search = el.search.value; clearSelection(); renderAll(); });
  el.columnFiltersBtn.addEventListener("click", () => toggleFiltersPanel());
  el.refreshBtn.addEventListener("click", () => openRefreshModal());
  el.addOsBtn.addEventListener("click", () => openAddOsModal());
  el.exportBtn.addEventListener("click", () => openExportMenu(el.exportBtn, visibleRowsUnpaged));
  document.getElementById("clear-filters-btn").addEventListener("click", () => {
    state.search = ""; el.search.value = ""; state.chip = "all";
    clearAllColumnFilters(); clearSelection(); renderAll();
  });

  // openRefreshModal already branches on state.selected -- reusing it here
  // (instead of the old bulkRefresh, which awaited one non-streamed request
  // and only showed a toast before/after) gives the bulk-selection refresh
  // the same progress bar as the toolbar's Refresh EOL/EOAS, so a large
  // selection shows live per-chunk progress instead of going quiet for
  // however long the whole batch takes.
  el.bulkRefreshBtn.addEventListener("click", openRefreshModal);
  document.getElementById("bulk-same-as-os-btn").addEventListener("click", bulkSameAsOs);
  document.getElementById("bulk-set-fields-btn").addEventListener("click", openBulkSetFieldsModal);
  document.getElementById("bulk-ambiguous-btn").addEventListener("click", bulkMarkAmbiguous);
  document.getElementById("bulk-revert-btn").addEventListener("click", bulkRevert);
  document.getElementById("bulk-clear-fields-btn").addEventListener("click", bulkClearFields);
  document.getElementById("bulk-mark-reviewed-btn").addEventListener("click", bulkMarkReviewed);
  document.getElementById("bulk-mark-unreviewed-btn").addEventListener("click", bulkMarkUnreviewed);
  document.getElementById("bulk-export-btn").addEventListener("click", (event) => openExportMenu(event.currentTarget, selectedRows));
  document.getElementById("bulk-delete-btn").addEventListener("click", bulkDelete);
  document.getElementById("bulk-clear-btn").addEventListener("click", clearSelection);

  document.getElementById("footer-prev-btn").addEventListener("click", () => { if (page > 1) { page -= 1; renderTable(); } });
  document.getElementById("footer-next-btn").addEventListener("click", () => { page += 1; renderTable(); });
  el.rowsPerPageChip.addEventListener("click", () => {
    pageSizeIndex = (pageSizeIndex + 1) % PAGE_SIZE_OPTIONS.length;
    el.rowsPerPageChip.textContent = String(PAGE_SIZE_OPTIONS[pageSizeIndex]);
    page = 1; renderTable();
  });

  await Promise.all([loadData(), loadRefreshEolSetting()]);
  renderAll();
}

async function loadRefreshEolSetting() {
  const settings = await api.getSettings().catch(() => ({ refresh_eol_enabled: true }));
  refreshEolEnabled = settings.refresh_eol_enabled !== false;
}

/** initEditor() only runs once at app startup, so a Settings change made
 * later in the same session never touched the editor's cached flag until a
 * full page reload -- the toolbar button kept looking clickable even after
 * being disabled. Called from main.js whenever the Lookup editor screen is
 * navigated to, so the button reflects the current setting every time. */
export async function syncRefreshEolSetting() {
  await loadRefreshEolSetting();
  updateRefreshButtonsState();
}

async function loadData() {
  const data = await api.getLookup("data");
  state.dataRows = data.rows;
  state.evidence = data.evidence;
  state.draftExists = data.draft_exists;
  state.publishedAt = data.published_at;
  state.dataRevision = data.data_revision ?? 0;
  dataByOs = new Map(state.dataRows.map((row) => [dedupeKey(row.os_string), row]));
  el.railRowCount.textContent = String(state.dataRows.length);
  el.publishedAtMeta.textContent = state.publishedAt
    ? `Lookup published ${formatPublishedAt(state.publishedAt)}`
    : "Lookup published —";
  renderStorageChip(data.storage_mode, data.storage_target);
}

function renderStorageChip(mode, target) {
  const chip = document.getElementById("storage-mode-chip");
  chip.hidden = false;
  chip.classList.toggle("postgres", mode === "postgres");
  chip.classList.toggle("file", mode !== "postgres");
  if (mode === "postgres") {
    chip.textContent = "Shared Database";
    chip.title = `The published lookup and draft are stored in a shared database (${target || "unknown host"}), not on this machine.`;
  } else {
    chip.textContent = "Local files";
    chip.title = "The published lookup and draft are stored as local files on this machine, not shared with other instances.";
  }
}

const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Only this one topbar timestamp uses this richer "2 Aug 2026, 12:24 pm"
// style -- everywhere else in the app (vendor last-updated, background task
// times, drawer/table dates) uses the plain YYYY-MM-DD[ HH:MM] format.
function formatPublishedAt(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hour24 = d.getHours();
  const hour12 = hour24 % 12 || 12;
  const minutes = String(d.getMinutes()).padStart(2, "0");
  const ampm = hour24 < 12 ? "am" : "pm";
  return `${d.getDate()} ${SHORT_MONTHS[d.getMonth()]} ${d.getFullYear()}, ${hour12}:${minutes} ${ampm}`;
}

function dedupeKey(v) { return String(v || "").trim().toLowerCase(); }

// Boolean-ish status cells ("true"/"false") compare case-insensitively --
// mirrors lookup_extras.py's _comparable_cell, which the server's own
// Draft-vs-Data diff already uses, so "edited" here means the same thing
// it means there.
function comparableCell(value) {
  const text = String(value ?? "").trim();
  const lowered = text.toLowerCase();
  return lowered === "true" || lowered === "false" ? lowered : text;
}

function displayFieldValue(field, value) {
  const text = String(value ?? "").trim();
  if (!text) return "(empty)";
  if (DATE_FIELDS.has(field)) {
    const parsed = parseRowDate(text);
    return parsed ? toISODate(parsed) : text;
  }
  return text;
}

/** Which fields differ between a Draft row and its Data baseline, formatted
 * for display -- the same 7-column comparison compute_lookup_diff does
 * server-side (lookup_extras.py), just per-field instead of a single
 * whole-row bool, since that's all the server tells the client today. */
function changedFields(row) {
  const baseline = dataByOs.get(dedupeKey(row.os_string));
  if (!baseline) return [];
  return CSV_HEADERS.filter(
    (header) => header !== "os_string" && comparableCell(row[header]) !== comparableCell(baseline[header])
  ).map((header) => ({
    field: header,
    label: FIELD_LABELS[header] || header,
    from: displayFieldValue(header, baseline[header]),
    to: displayFieldValue(header, row[header]),
  }));
}

/** Return to Data after Exit draft / Delete draft / Publish — resets the
 * quick chip so the table doesn't look empty (Data has no "changed" chip). */
function backToData() {
  if (state.chip === "changed") state.chip = "all";
  setState({ source: "data" });
}

// True while a vendor lookup update or an Add OS enrichment pipeline is
// running -- both mutate rows/evidence the same Draft would be built from,
// so entering Draft mid-run risks forking from a half-updated state.
function isEnrichmentBusy() {
  return hasActive("add-os") || getTasks().some((t) => t.kind.startsWith("vendor-sync:") && t.status === "running");
}

async function switchSource(target) {
  if (target === state.source) return;
  if (target === "draft") {
    if (isEnrichmentBusy()) {
      showToast("Vendor lookup update or Add OS enrichment is in progress — try again once it finishes.");
      return;
    }
    if (state.draftExists) {
      const draft = await api.getLookup("draft");
      state.draftRows = draft.rows;
      state.evidence = draft.evidence;
      state.draftBasedOnRevision = draft.based_on_revision ?? 0;
    } else {
      // Fork from a fresh fetch of Data, not whatever the browser's
      // in-memory copy happens to be -- that stale copy could already be
      // older than Data's current on-disk state. This same fetch also
      // becomes the publish-time merge base, so it must be exactly what
      // Data was at the instant the draft was created, not re-derived
      // from disk later (which would race against concurrent publishes).
      const fresh = await api.getLookup("data");
      state.draftRows = fresh.rows.map((row) => ({ ...row }));
      // Same "fetch fresh" principle applies to evidence -- state.evidence
      // is a single shared field, not per-source, so without this it keeps
      // whatever was already in memory (e.g. a just-deleted draft's
      // "reviewed" marks) instead of the fresh Data evidence being used as
      // this new draft's actual persisted baseline below.
      state.evidence = fresh.evidence;
      await persistDraft({ baseRows: fresh.rows, baseEvidence: fresh.evidence });
      state.draftExists = true;
      state.draftBasedOnRevision = fresh.data_revision ?? 0;
    }
    if (state.chip === "all") state.chip = "changed";
  } else {
    if (state.chip === "changed") state.chip = "all";
  }
  setState({ source: target });
  clearSelection();
  closeDrawer();
  await recomputeDiff();
  renderAll();
}

let diffRequestId = 0;

async function recomputeDiff() {
  if (!isDraft()) { diff = { added: [], edited: [], deleted: [], unresolved: 0 }; addedSet = new Set(); editedSet = new Set(); return; }
  const requestId = ++diffRequestId;
  const result = await api.getDiff("draft").catch(() => ({ added: [], edited: [], deleted: [], unresolved: 0 }));
  // Autosave can fire several overlapping diff requests in quick succession
  // (e.g. a row edit followed almost immediately by another); only apply the
  // response if nothing newer has been requested since, so a slow, now-stale
  // response can't clobber a fresher one that already landed.
  if (requestId !== diffRequestId) return;
  diff = result;
  addedSet = new Set((diff.added || []).map(dedupeKey));
  editedSet = new Set((diff.edited || []).map(dedupeKey));
}

function scheduleAutosave() {
  setState({ dirty: true });
  updateModeBar();
  if (!state.autoSave) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(persistDraft, 800);
}

async function persistDraft(baseOptions = {}) {
  await api.saveLookup(state.draftRows, state.evidence, "draft", baseOptions);
  setState({ dirty: false });
  // The diff (Changed chip, NEW/EDITED flags) is computed server-side from
  // the saved draft file, so it can only be refreshed once the save above
  // has actually landed -- recomputing it right after a mutation, before
  // the debounced save runs, would just re-read the stale on-disk draft.
  await recomputeDiff();
  refreshView();
}

// ---------- Row transforms (client-side; no external calls needed) ----------

/** "Linux Linux 2.6.32" -> "Linux 2.6.32", "VMware VMware ESXi" -> "VMware
 * ESXi" -- collapses a word repeated immediately next to itself (case
 * insensitive), which shows up a lot in raw inventory strings. Only
 * adjacent repeats collapse; "Windows Server 2012 Windows" is untouched
 * since the two "Windows" aren't next to each other. */
function collapseConsecutiveDuplicateWords(text) {
  // Collapses a word OR multi-word phrase repeated immediately next to
  // itself (case-insensitive), preferring the longest repeated run at each
  // position. Mirrors collapse_consecutive_duplicate_words in
  // normalization_service.py -- the two must agree on what counts as a
  // duplicate since both feed the same fields.
  const words = String(text || "").trim().split(/\s+/).filter(Boolean);
  const n = words.length;
  const out = [];
  let i = 0;
  while (i < n) {
    let runLen = 0;
    for (let length = Math.floor((n - i) / 2); length > 0; length -= 1) {
      const first = words.slice(i, i + length).map((w) => w.toLowerCase());
      const second = words.slice(i + length, i + 2 * length).map((w) => w.toLowerCase());
      if (first.length === second.length && first.every((w, idx) => w === second[idx])) {
        runLen = length;
        break;
      }
    }
    if (runLen) {
      out.push(...words.slice(i, i + runLen));
      i += 2 * runLen;
    } else {
      out.push(words[i]);
      i += 1;
    }
  }
  return out.join(" ");
}

function applySameAsOs(row) {
  const collapsed = collapseConsecutiveDuplicateWords(row.os_string);
  row.normalized_os_detailed_name = collapsed;
  row.normalized_os = collapsed;
}

// Same shape as an ambiguous row created by the Add OS pipeline -- clears
// EOL/EOAS too, since keeping lifecycle dates tied to a normalization the
// user just disavowed as unreliable would be actively misleading (they'd
// otherwise still show a stale "Past EOL" chip etc.). Matches what
// isAmbiguousRow/dateCellHtml already expect ("skipped" for a blank date on
// an ambiguous row, not "none").
function applyAmbiguous(row) {
  row.normalized_os_detailed_name = "Ambiguous OS";
  row.normalized_os = "Ambiguous OS";
  row.eol_date = "";
  row.eol_status = "";
  row.eoas_date = "";
  row.eoas_status = "";
  row.matched_by = "Ambiguous";
}

function revertRow(row) {
  const baseline = dataByOs.get(dedupeKey(row.os_string));
  if (!baseline) return;
  CSV_HEADERS.forEach((header) => { if (header !== "os_string") row[header] = baseline[header]; });
}

// Blanks every field except os_string -- for when a match is simply wrong
// and the user wants to start over from scratch. Distinct from "Mark
// Ambiguous" (a different signal: "this string names more than one
// product, do not guess") and "Revert to Data" (recovers Data's last
// published values, not empty).
function clearRowFields(row) {
  CSV_HEADERS.forEach((header) => { if (header !== "os_string") row[header] = ""; });
  row.matched_by = "No match";
}

// ---------- Reviewed flag ----------
// Stored as a sibling key inside the evidence sidecar's by_os[os_string]
// entry (alongside the detailed/normalized/eol lookup slots) rather than as
// a CSV column -- it rides along with the rest of the evidence through
// autosave, pruning and the 3-way publish merge for free, with no schema
// migration needed (see row_matched_by for the same pattern already in use).
function evidenceKey(row) { return String(row.os_string || "").trim(); }

function isRowReviewed(row) {
  return Boolean(state.evidence?.by_os?.[evidenceKey(row)]?.reviewed);
}

function setRowReviewed(row, value) {
  const key = evidenceKey(row);
  if (!key) return;
  state.evidence.by_os = state.evidence.by_os || {};
  state.evidence.by_os[key] = { ...(state.evidence.by_os[key] || {}), reviewed: value };
}

function toggleRowReviewed(row) {
  setRowReviewed(row, !isRowReviewed(row));
}

// Mirrors lookup_extras._FIELD_LABELS / the evidence sidecar's 3 slots.
const FIELD_TO_EVIDENCE_SLOT = {
  normalized_os_detailed_name: ["detailed", "Normalized OS detailed name"],
  normalized_os: ["normalized", "Normalized OS"],
  eol_date: ["eol", "EOL / EOAS lifecycle"],
  eol_status: ["eol", "EOL / EOAS lifecycle"],
  eoas_date: ["eol", "EOL / EOAS lifecycle"],
  eoas_status: ["eol", "EOL / EOAS lifecycle"],
};

/** A field hand-edited in the drawer no longer reflects whatever a vendor
 * lookup (or fuzzy/AI match) originally filled it with -- without this, the
 * evidence panel kept showing that stale "Filled from X" note forever,
 * describing a value the user had since overridden. Returns the evidence
 * slot's display label (for updating the drawer immediately) or null if
 * this field has no evidence slot. */
function markFieldManual(row, field) {
  const mapping = FIELD_TO_EVIDENCE_SLOT[field];
  if (!mapping) return null;
  const [slot, label] = mapping;
  const key = evidenceKey(row);
  if (!key) return null;
  state.evidence.by_os = state.evidence.by_os || {};
  const entry = state.evidence.by_os[key] || {};
  state.evidence.by_os[key] = { ...entry, [slot]: { method: "manual" } };
  return label;
}

async function rerunRow(row) {
  // A manual, single-row re-run is always allowed -- the Settings toggle
  // only blocks refreshing the whole draft/data in one shot.
  showToast("Re-running lookup…");
  try {
    // Only when both normalized fields are blank -- same precondition as
    // Add-OS's brand-new rows. A row that already has normalized names set
    // keeps them exactly as-is; re-run only refreshes EOL/EOAS for it,
    // unchanged from before. This is NOT run for bulk Refresh EOL/EOAS --
    // that operation is about refreshing dates for already-normalized
    // rows, not reconsidering which existing pair a row's name matches.
    if (!row.normalized_os_detailed_name && !row.normalized_os) {
      const allowedPairs = buildAllowedPairsFromDraft();
      const settings = await api.getSettings().catch(() => ({ ai_enabled: false }));
      const match = await matchAgainstExistingPairs(row.os_string, allowedPairs, settings);
      if (match) {
        row.normalized_os_detailed_name = match.normalized_os_detailed_name;
        row.normalized_os = match.normalized_os;
      }
    }
    const result = await api.refreshRow(row);
    Object.assign(row, result.row);
    // The row's fields update above, but the evidence sidecar entry (what
    // the drawer's "Filled from.../Retired method..." text is built from)
    // was never applied anywhere -- it stayed whatever was last saved, so a
    // re-run could find nothing new yet keep showing an old, now-stale
    // evidence note (e.g. a retired "copied from row X" record) forever,
    // even after the row's own fields plainly changed.
    const key = evidenceKey(row);
    if (key) {
      state.evidence.by_os = state.evidence.by_os || {};
      state.evidence.by_os[key] = result.evidence_entry || {};
    }
    scheduleAutosave();
    refreshView();
    refreshDrawerFields(row);
    refreshDrawerEvidence(row, result.evidence_detail);
    showToast("Lookup refreshed.");
  } catch (error) {
    showToast(`Re-run failed: ${error.message}`);
  }
}

// Themed replacement for a native <select> -- only one export menu open at
// a time, closes on outside click/Escape/scroll-reflow, matching the
// pattern date_range_picker.js already established for popovers.
let openExportMenuState = null;

function openExportMenu(triggerEl, getRows) {
  if (openExportMenuState) {
    const wasThis = openExportMenuState.triggerEl === triggerEl;
    openExportMenuState.close();
    if (wasThis) return;
  }
  triggerEl.classList.add("is-open");

  const menu = document.createElement("div");
  menu.className = "action-menu";
  menu.innerHTML = EXPORT_FORMATS.map(
    ([fmt, label]) => `<button type="button" class="action-menu-item" data-format="${fmt}">${label}</button>`
  ).join("");
  document.body.appendChild(menu);
  menu.querySelectorAll("[data-format]").forEach((btn) => {
    btn.addEventListener("click", () => {
      close();
      exportRows(getRows(), btn.dataset.format);
    });
  });

  const onDocClick = (event) => {
    if (!menu.contains(event.target) && event.target !== triggerEl) close();
  };
  const onKeydown = (event) => { if (event.key === "Escape") close(); };
  const onReflow = () => position();
  document.addEventListener("click", onDocClick, true);
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("resize", onReflow);
  window.addEventListener("scroll", onReflow, true);

  function close() {
    triggerEl.classList.remove("is-open");
    menu.remove();
    document.removeEventListener("click", onDocClick, true);
    document.removeEventListener("keydown", onKeydown);
    window.removeEventListener("resize", onReflow);
    window.removeEventListener("scroll", onReflow, true);
    if (openExportMenuState && openExportMenuState.triggerEl === triggerEl) openExportMenuState = null;
  }

  function position() {
    const rect = triggerEl.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    let left = rect.left;
    if (left + menuRect.width > window.innerWidth - 8) left = window.innerWidth - menuRect.width - 8;
    let top = rect.bottom + 4;
    if (top + menuRect.height > window.innerHeight - 8) top = rect.top - menuRect.height - 4;
    menu.style.left = `${Math.max(8, left)}px`;
    menu.style.top = `${Math.max(8, top)}px`;
  }

  openExportMenuState = { triggerEl, close };
  position();
}

async function exportRows(list, format = "csv") {
  if (!list.length) { showToast("Nothing to export."); return; }
  if (format === "csv") {
    downloadCsv(list);
    return;
  }
  try {
    const { blob, filename } = await api.exportRowsAsFile(format, list);
    downloadBlob(blob, filename || `eol_lookup_export.${format === "excel" ? "xlsx" : "parquet"}`);
  } catch (error) {
    showToast(`Export failed: ${error.message}`);
  }
}

function downloadCsv(list) {
  const csvHeaderLine = CSV_HEADERS.join(",");
  const lines = list.map((row) => CSV_HEADERS.map((h) => csvEscape(row[h])).join(","));
  const blob = new Blob([csvHeaderLine + "\n" + lines.join("\n") + "\n"], { type: "text/csv" });
  downloadBlob(blob, "eol_lookup_export.csv");
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function csvEscape(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

// ---------- Selection ----------

// Always re-renders the table too -- every row/header checkbox's "checked"
// state is baked into its HTML at render time, not live-bound to
// state.selected, so skipping this leaves them showing stale (still
// checked) even though the selection underneath is now empty. The bulk
// bar's own "Clear" button used to call this alone, with no render after --
// clicking it emptied state.selected but the header checkbox kept showing
// checked, and clicking that stale-checked box then silently RE-selected
// the page (toggleSelectAllPage reads state.selected fresh) instead of
// visibly doing nothing, which is what made it look like there was no way
// to select just the current page again.
function clearSelection() { state.selected.clear(); updateBulkBar(); renderTable(); }
function selectedRows() { return currentRows().filter((row) => state.selected.has(row.os_string)); }

function updateBulkBar() {
  const n = state.selected.size;
  el.bulkBar.hidden = !(isDraft() && n > 0);
  el.bulkCount.textContent = `${n} selected`;

  // Only offer "select all N matching rows" once the current page is
  // entirely selected AND there's more beyond this page left to add --
  // otherwise the prompt would show for any partial selection, or linger
  // uselessly once everything filtered is already selected.
  const filtered = visibleRowsUnpaged();
  const size = PAGE_SIZE_OPTIONS[pageSizeIndex];
  const pageRows = filtered.slice((page - 1) * size, page * size);
  const pageAllSelected = pageRows.length > 0 && pageRows.every((r) => state.selected.has(r.os_string));
  const allFilteredSelected = filtered.length > 0 && filtered.every((r) => state.selected.has(r.os_string));
  const showSelectAllPrompt = pageAllSelected && filtered.length > pageRows.length && !allFilteredSelected;
  el.bulkSelectAllFilteredBtn.hidden = !showSelectAllPrompt;
  if (showSelectAllPrompt) {
    el.bulkSelectAllFilteredBtn.textContent = `Select all ${filtered.length} matching rows`;
    el.bulkSelectAllFilteredBtn.onclick = () => selectAllFiltered(filtered);
  }

  updateRefreshButtonsState();
}

/** Refresh EOL/EOAS being disabled in Settings only blocks the "refresh
 * everything" action -- a manual re-run on one row (the drawer) or a
 * deliberate bulk-selected refresh must always still work, so only the
 * toolbar button gets disabled, and only while nothing is selected (with a
 * selection active, that same button refreshes just the selection, same as
 * the bulk bar's own button, which is never disabled by this setting). */
function updateRefreshButtonsState() {
  const blanketDisabled = !refreshEolEnabled && state.selected.size === 0;
  el.refreshBtn.disabled = blanketDisabled;
  el.refreshBtn.title = blanketDisabled
    ? "Refresh EOL/EOAS is disabled in Settings for the whole draft — select rows to refresh just those."
    : "";
}

/** Select-all applies to only the current page, not the whole filtered set
 * -- selecting every matching row regardless of pagination is a deliberate,
 * separate action (the "Select all N matching rows" prompt in the bulk bar,
 * see updateBulkBar) so a click meant to select "what's on screen" can't
 * silently sweep in thousands of off-screen rows. */
function toggleSelectAllPage(pageRows) {
  const allSelected = pageRows.length > 0 && pageRows.every((r) => state.selected.has(r.os_string));
  if (allSelected) {
    pageRows.forEach((r) => state.selected.delete(r.os_string));
  } else {
    pageRows.forEach((r) => state.selected.add(r.os_string));
  }
  updateBulkBar();
  renderTable();
}

/** Explicit opt-in to select every row matching the current filter, across
 * every page -- offered only once the whole current page is already
 * selected via the header checkbox (see updateBulkBar's showSelectAllPrompt). */
function selectAllFiltered(filtered) {
  filtered.forEach((r) => state.selected.add(r.os_string));
  updateBulkBar();
  renderTable();
}

function bulkSameAsOs() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach(applySameAsOs);
  scheduleAutosave();
  refreshView();
  showToast(`Set ${targets.length} row(s) to match their OS string.`);
}

/** Bulk-set normalized_os_detailed_name/normalized_os to a user-typed value
 * across every selected row -- for when several rows are all genuinely the
 * same OS but never got a clean matching pair (e.g. a batch of near-
 * identical inventory strings). "Use the same value for Normalized OS"
 * covers the common case of wanting both fields identical; unchecking it
 * reveals a second, independent input. A blank field is left untouched on
 * every row, so this can also set just one of the two fields in bulk.
 * Marks each field actually written as "manual" evidence, same as typing
 * directly into that cell would (unlike the derived "Same as OS" action,
 * this is a genuine manually-dictated value, not derived from the row). */
function openBulkSetFieldsModal() {
  const targets = selectedRows();
  if (!targets.length) return;

  const detailedInput = document.getElementById("bulk-set-detailed-input");
  const normalizedInput = document.getElementById("bulk-set-normalized-input");
  const normalizedField = document.getElementById("bulk-set-normalized-field");
  const sameCheckbox = document.getElementById("bulk-set-same-checkbox");
  const sameChip = document.getElementById("bulk-set-same-chip");
  const syncSameChipVisual = () => {
    sameChip.classList.toggle("active", sameCheckbox.checked);
    sameChip.querySelector(".box").innerHTML = sameCheckbox.checked ? iconMarkup("check", { size: 9 }) : "";
  };

  document.getElementById("bulk-set-fields-count").textContent =
    `Applies to ${targets.length} selected row(s).`;
  detailedInput.value = "";
  normalizedInput.value = "";
  sameCheckbox.checked = true;
  normalizedField.hidden = true;
  syncSameChipVisual();
  sameCheckbox.onchange = () => {
    normalizedField.hidden = sameCheckbox.checked;
    syncSameChipVisual();
  };

  openModal("modal-bulk-set-fields");
  document.getElementById("bulk-set-fields-confirm-btn").onclick = () => {
    const detailedValue = detailedInput.value.trim();
    const normalizedValue = sameCheckbox.checked ? detailedValue : normalizedInput.value.trim();
    if (!detailedValue && !normalizedValue) {
      showToast("Enter a value for at least one field.");
      return;
    }
    targets.forEach((row) => {
      if (detailedValue) {
        row.normalized_os_detailed_name = detailedValue;
        markFieldManual(row, "normalized_os_detailed_name");
      }
      if (normalizedValue) {
        row.normalized_os = normalizedValue;
        markFieldManual(row, "normalized_os");
      }
    });
    scheduleAutosave();
    refreshView();
    closeModal();
    showToast(`Set fields for ${targets.length} row(s).`);
  };
}

function bulkRevert() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach(revertRow);
  scheduleAutosave();
  refreshView();
  showToast(`Reverted ${targets.length} row(s) to Data.`);
}

function bulkMarkAmbiguous() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach(applyAmbiguous);
  scheduleAutosave();
  refreshView();
  showToast(`Marked ${targets.length} row(s) as Ambiguous OS.`);
}

function bulkClearFields() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach(clearRowFields);
  scheduleAutosave();
  refreshView();
  showToast(`Cleared fields for ${targets.length} row(s).`);
}

function bulkMarkReviewed() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach((row) => setRowReviewed(row, true));
  scheduleAutosave();
  refreshView();
  showToast(`Marked ${targets.length} row(s) as reviewed.`);
}

function bulkMarkUnreviewed() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach((row) => setRowReviewed(row, false));
  scheduleAutosave();
  refreshView();
  showToast(`Marked ${targets.length} row(s) as not reviewed.`);
}

function bulkDelete() {
  const targets = selectedRows();
  if (!targets.length) return;
  const ids = new Set(targets.map((r) => r.os_string));
  state.draftRows = state.draftRows.filter((r) => !ids.has(r.os_string));
  clearSelection();
  scheduleAutosave();
  refreshView();
  showToast(`Deleted ${targets.length} row(s).`);
}

// ---------- Quick chips ----------

const QUICK_CHIPS = [
  ["all", "All"],
  ["missing", "Missing normalization"],
  ["eol", "Past EOL"],
  ["eoas", "Past EOAS"],
  ["nodates", "No dates"],
  ["ambiguous", "Ambiguous"],
  ["changed", "Changed"],
  ["unreviewed", "Not reviewed"],
];

function isAmbiguousRow(row) {
  const d = String(row.normalized_os_detailed_name || "").trim().toLowerCase();
  const n = String(row.normalized_os || "").trim().toLowerCase();
  return d === "ambiguous os" || n === "ambiguous os";
}

// Only rows that are actually new/edited need reviewing -- a row that's
// identical to published Data doesn't need re-validating just because it's
// sitting in the draft.
function isChangedRow(row) {
  const key = dedupeKey(row.os_string);
  return addedSet.has(key) || editedSet.has(key);
}

function rowMatchesChip(row, chip) {
  switch (chip) {
    case "missing": return !String(row.normalized_os_detailed_name || "").trim() || !String(row.normalized_os || "").trim();
    case "eol": { const d = parseRowDate(row.eol_date); return d && d.getTime() < Date.now(); }
    case "eoas": { const d = parseRowDate(row.eoas_date); return d && d.getTime() < Date.now(); }
    case "nodates": return !String(row.eol_date || "").trim() && !String(row.eoas_date || "").trim();
    case "ambiguous": return isAmbiguousRow(row);
    case "changed": return isDraft() && isChangedRow(row);
    case "unreviewed": return isDraft() && isChangedRow(row) && !isRowReviewed(row);
    default: return true;
  }
}

function quickChipCounts() {
  const base = currentRows();
  const counts = {};
  for (const [key] of QUICK_CHIPS) counts[key] = base.filter((row) => rowMatchesChip(row, key)).length;
  return counts;
}

function renderQuickChips() {
  const counts = quickChipCounts();
  const chips = isDraft() ? QUICK_CHIPS : QUICK_CHIPS.filter(([key]) => key !== "changed" && key !== "unreviewed");
  el.quickChips.innerHTML = chips
    .map(([key, label]) => `
      <button type="button" class="quick-chip ${state.chip === key ? "active" : ""}" data-chip="${key}">
        ${label} <span class="count tabular">${counts[key]}</span>
      </button>`)
    .join("");
  el.quickChips.querySelectorAll(".quick-chip").forEach((btn) => {
    btn.addEventListener("click", () => { state.chip = btn.dataset.chip; clearSelection(); renderAll(); });
  });
}

// ---------- Search + full filter pipeline ----------

function matchesSearch(row) {
  const q = state.search.trim().toLowerCase();
  if (!q) return true;
  return [row.os_string, row.normalized_os_detailed_name, row.normalized_os].some((v) => String(v || "").toLowerCase().includes(q));
}

function visibleRowsUnpaged() {
  const filtered = currentRows().filter((row) => matchesSearch(row) && rowMatchesChip(row, state.chip) && matchesColumnFilters(row));
  if (state.sort.key) filtered.sort((a, b) => compareRows(a, b, state.sort.key, state.sort.dir));
  return filtered;
}

// ---------- Column sort ----------

const SORTABLE_COLUMNS = [
  { label: "OS string", key: "os_string" },
  { label: "Normalized detailed name", key: "normalized_os_detailed_name" },
  { label: "Normalized OS", key: "normalized_os" },
  { label: "EOL date", key: "eol_date" },
  { label: "EOL status", key: "eol_status" },
  { label: "EOAS date", key: "eoas_date" },
  { label: "EOAS status", key: "eoas_status" },
];
const DATE_SORT_KEYS = new Set(["eol_date", "eoas_date"]);

function sortValue(row, key) {
  const raw = row[key];
  if (DATE_SORT_KEYS.has(key)) {
    const d = parseRowDate(raw);
    return d ? d.getTime() : null;
  }
  const s = String(raw || "").trim();
  return s ? s.toLowerCase() : null;
}

/** Blank/unparseable values always sort last, regardless of direction --
 * matches how the table already renders them ("none" chips at a glance
 * read as "nothing to compare", not as the lowest possible value). */
function compareRows(a, b, key, dir) {
  const va = sortValue(a, key);
  const vb = sortValue(b, key);
  if (va === null && vb === null) return 0;
  if (va === null) return 1;
  if (vb === null) return -1;
  const cmp = typeof va === "number" ? va - vb : (va < vb ? -1 : va > vb ? 1 : 0);
  return dir === "asc" ? cmp : -cmp;
}

function sortIndicator(key) {
  if (state.sort.key !== key) return "";
  return state.sort.dir === "asc" ? " &#9650;" : " &#9660;";
}

function columnHeaderHtml(col) {
  return `<span class="col-head sortable ${state.sort.key === col.key ? "active" : ""}" data-sort-key="${col.key}">${col.label}${sortIndicator(col.key)}</span>`;
}

function bindSortHeaders() {
  el.tableHeaderRow.querySelectorAll("[data-sort-key]").forEach((headEl) => {
    headEl.addEventListener("click", () => {
      const key = headEl.dataset.sortKey;
      if (state.sort.key !== key) state.sort = { key, dir: "asc" };
      else if (state.sort.dir === "asc") state.sort = { key, dir: "desc" };
      else state.sort = { key: null, dir: "asc" };
      renderAll();
    });
  });
}

// ---------- Rendering ----------

/** Full re-render for when the *context* changed (search/chip/filter/source)
 * -- resets to page 1 since the filtered set is effectively a new list. */
function renderAll() {
  page = 1;
  refreshView();
}

/** Re-render for in-place data changes (row edits, bulk actions, autosave)
 * that don't change what's being filtered/searched for -- keeps whatever
 * page the user is currently looking at instead of jumping back to page 1,
 * which is what made bulk actions look like they'd cleared the selection. */
function refreshView() {
  updateModeBar();
  renderQuickChips();
  updateBulkBar();
  syncChangedFilterVisibility();
  el.filterBadge.hidden = activeFilterCount() === 0;
  el.filterBadge.textContent = String(activeFilterCount());
  el.columnFiltersBtn.classList.toggle("has-filters", activeFilterCount() > 0);
  el.addOsBtn.hidden = !isDraft();
  renderTable();
}

function updateModeBar() {
  el.modeBar.classList.toggle("is-draft", isDraft());
  el.segData.classList.toggle("active", isData());
  el.segDraft.classList.toggle("active", isDraft());
  el.segDraft.disabled = !state.draftExists && !isDraft();
  el.segDraft.title = el.segDraft.disabled ? "No draft yet. Click Edit data to create one." : "";

  if (isDraft()) {
    el.modeChip.textContent = "EDITING DRAFT";
    el.modeHint.textContent = "Changes stay in the draft until you publish";
    el.modeBarRight.innerHTML = `
      <label class="checkbox-chip ${state.chip === "changed" ? "active" : ""}" id="only-changed-chip">
        <input type="checkbox" ${state.chip === "changed" ? "checked" : ""} />
        <span class="box">${state.chip === "changed" ? iconMarkup("check", { size: 9 }) : ""}</span>
        Only changed rows
      </label>
      <label class="checkbox-chip ${state.autoSave ? "active" : ""}" id="autosave-chip">
        <input type="checkbox" ${state.autoSave ? "checked" : ""} />
        <span class="box">${state.autoSave ? iconMarkup("check", { size: 9 }) : ""}</span>
        Auto-save
      </label>
      <span class="save-state-pill ${state.dirty ? "unsaved" : "saved"}">${state.dirty ? "Unsaved" : "Saved"}</span>
      <button class="btn tertiary" id="save-draft-btn" type="button">Save draft</button>
      <button class="btn" id="exit-draft-btn" type="button">Exit draft</button>
      <button class="btn" id="revert-draft-btn" type="button">Revert all changes</button>
      <button class="btn danger" id="delete-draft-btn" type="button">Delete draft</button>
      <button class="btn primary" id="validate-publish-btn" type="button">
        <span class="icon" style="width:14px;height:14px;background-image:url('/static/icons/publish.svg');background-size:contain;filter:var(--icon-filter)"></span>
        Validate &amp; publish
      </button>`;
    document.getElementById("only-changed-chip").addEventListener("click", (e) => {
      e.preventDefault();
      state.chip = state.chip === "changed" ? "all" : "changed";
      clearSelection(); renderAll();
    });
    document.getElementById("autosave-chip").addEventListener("click", (e) => {
      e.preventDefault();
      state.autoSave = !state.autoSave;
      if (state.autoSave && state.dirty) persistDraft();
      renderAll();
    });
    document.getElementById("save-draft-btn").addEventListener("click", persistDraft);
    document.getElementById("exit-draft-btn").addEventListener("click", () => switchSource("data"));
    document.getElementById("revert-draft-btn").addEventListener("click", openRevertDraftModal);
    document.getElementById("delete-draft-btn").addEventListener("click", openDeleteDraftModal);
    document.getElementById("validate-publish-btn").addEventListener("click", openValidateModal);
  } else {
    el.modeChip.textContent = "PUBLISHED · READ-ONLY";
    el.modeHint.textContent = state.draftExists ? "A saved draft exists — open it to keep editing" : "Click Edit data to create a draft";
    el.modeBarRight.innerHTML = `
      <button class="btn primary" id="edit-data-btn" type="button">
        <span class="icon" style="width:14px;height:14px;background-image:url('/static/icons/edit.svg');background-size:contain;filter:var(--icon-filter)"></span>
        ${state.draftExists ? "Resume draft" : "Edit data"}
      </button>`;
    document.getElementById("edit-data-btn").addEventListener("click", () => switchSource("draft"));
  }
}

function renderTable() {
  el.tableTrack.classList.toggle("is-draft", isDraft());
  el.tableTrack.classList.toggle("is-data", isData());

  const filtered = visibleRowsUnpaged();

  const size = PAGE_SIZE_OPTIONS[pageSizeIndex];
  const totalPages = Math.max(1, Math.ceil(filtered.length / size));
  if (page > totalPages) page = totalPages;
  const pageRows = filtered.slice((page - 1) * size, page * size);

  const columnHeadersHtml = SORTABLE_COLUMNS.map(columnHeaderHtml);
  if (isDraft()) {
    const pageAllSelected = pageRows.length > 0 && pageRows.every((r) => state.selected.has(r.os_string));
    const headerCheckboxHtml = `<span class="row-checkbox ${pageAllSelected ? "checked" : ""}" id="header-select-all" title="${pageAllSelected ? "Deselect all on this page" : "Select all on this page"}">${iconMarkup("check", { size: 10 })}</span>`;
    el.tableHeaderRow.innerHTML = [headerCheckboxHtml, ...columnHeadersHtml].join("");
    document.getElementById("header-select-all").addEventListener("click", () => toggleSelectAllPage(pageRows));
  } else {
    el.tableHeaderRow.innerHTML = columnHeadersHtml.join("");
  }
  bindSortHeaders();

  el.tableBody.innerHTML = "";
  el.tableEmpty.hidden = filtered.length !== 0;
  pageRows.forEach((row) => el.tableBody.appendChild(renderRow(row)));

  renderFooter(filtered.length, currentRows().length);
  el.footerPage.textContent = `Page ${page} of ${totalPages}`;
  document.getElementById("footer-prev-btn").disabled = page <= 1;
  document.getElementById("footer-next-btn").disabled = page >= totalPages;

  updateBulkBar();
}

function renderFooter(shown, total) {
  const base = currentRows();
  const pastEol = base.filter((r) => { const d = parseRowDate(r.eol_date); return d && d.getTime() < Date.now(); }).length;
  const notNormalized = base.filter((r) => !String(r.normalized_os_detailed_name || "").trim() || !String(r.normalized_os || "").trim()).length;
  const noDates = base.filter((r) => !String(r.eol_date || "").trim() && !String(r.eoas_date || "").trim()).length;
  el.footerShown.textContent = `${shown} of ${total} shown · ${state.dataRows.length} total`;
  el.footerBreakdown.textContent = `${pastEol} past EOL · ${notNormalized} not normalized · ${noDates} without dates`;
}

function dateCellHtml(row, key, kind) {
  const d = parseRowDate(row[key]);
  const cls = classifyDateChip(d, kind);
  const label = d ? d.toISOString().slice(0, 10) : (isAmbiguousRow(row) ? "skipped" : "none");
  return `<div class="date-cell"><span class="date-chip ${cls}">${label}</span>${d ? `<span class="date-relative">${formatRelative(d)}</span>` : ""}</div>`;
}

function statusCellHtml(row, key) {
  const v = String(row[key] || "").trim().toLowerCase();
  if (v === "true") return `<span class="status-chip true">True</span>`;
  if (v === "false") return `<span class="status-chip false">False</span>`;
  return `<span class="status-chip empty">none</span>`;
}

function textCellHtml(value) {
  const text = String(value || "").trim();
  return text ? `<span class="cell-text">${escapeHtml(text)}</span>` : `<span class="cell-text empty">none</span>`;
}

function renderRow(row) {
  const wrap = document.createElement("div");
  wrap.className = "table-row";
  const key = dedupeKey(row.os_string);
  if (state.selected.has(row.os_string)) wrap.classList.add("selected");
  if (isDrawerOpenFor(row)) wrap.classList.add("drawer-open");

  let flag = "";
  if (isDraft()) {
    if (addedSet.has(key)) flag = `<span class="row-flag new">NEW</span>`;
    else if (editedSet.has(key)) {
      const changes = changedFields(row);
      const tooltip = changes.length
        ? changes.map((c) => `${c.label}: ${c.from} → ${c.to}`).join("\n")
        : "Edited";
      flag = `<span class="row-flag edited" title="${escapeHtml(tooltip)}">EDITED</span>`;
    }
  }

  // Only rows that actually changed need a review control -- an unchanged
  // row has nothing new to validate against published Data.
  const showReviewToggle = isDraft() && isChangedRow(row);
  const reviewed = showReviewToggle && isRowReviewed(row);
  const reviewToggleHtml = showReviewToggle
    ? `<button type="button" class="review-toggle ${reviewed ? "reviewed" : ""}" data-role="review" title="${reviewed ? "Mark as not reviewed" : "Mark as reviewed"}">${reviewed ? iconMarkup("check", { size: 9 }) + " Reviewed" : "Review"}</button>`
    : "";

  const cells = [];
  if (isDraft()) {
    cells.push(`<span class="row-checkbox ${state.selected.has(row.os_string) ? "checked" : ""}" data-role="select">${iconMarkup("check", { size: 10 })}</span>`);
  }
  cells.push(`<div class="cell-os">${flag}<span class="os-value" title="${escapeHtml(row.os_string)}">${escapeHtml(row.os_string)}</span>${reviewToggleHtml}</div>`);
  cells.push(textCellHtml(row.normalized_os_detailed_name));
  cells.push(textCellHtml(row.normalized_os));
  cells.push(dateCellHtml(row, "eol_date", "eol"));
  cells.push(statusCellHtml(row, "eol_status"));
  cells.push(dateCellHtml(row, "eoas_date", "eoas"));
  cells.push(statusCellHtml(row, "eoas_status"));
  wrap.innerHTML = cells.join("");

  wrap.addEventListener("click", (event) => {
    if (event.target.closest("[data-role='select']") || event.target.closest("[data-role='review']")) return;
    // While a selection is active, clicking a row is presumed to be part of
    // building/adjusting that selection (a near-miss on the checkbox) --
    // don't also pop the drawer open. Only open it when nothing is selected.
    if (state.selected.size > 0) return;
    openDrawer(row);
    renderTable();
  });

  const selectEl = wrap.querySelector("[data-role='select']");
  selectEl?.addEventListener("click", (event) => {
    event.stopPropagation();
    if (state.selected.has(row.os_string)) state.selected.delete(row.os_string);
    else state.selected.add(row.os_string);
    updateBulkBar();
    renderTable();
  });

  const reviewEl = wrap.querySelector("[data-role='review']");
  reviewEl?.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleRowReviewed(row);
    scheduleAutosave();
    refreshDrawerReviewedState(row);
    renderTable();
  });

  return wrap;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------- Refresh EOL/EOAS modal ----------

async function openRefreshModal() {
  // A disabled setting only blocks refreshing the whole draft/data in one
  // shot -- a deliberate bulk selection must still go through, whether it
  // was triggered from the bulk bar's own button or this same toolbar
  // button (both call this function; behavior already branches on
  // state.selected below).
  if (!refreshEolEnabled && state.selected.size === 0) {
    showToast("Refresh EOL/EOAS is disabled in Settings for the whole draft — select rows to refresh just those.");
    return;
  }
  if (hasActive("refresh")) {
    showToast("A refresh is already running — see Background tasks.");
    return;
  }
  if (!isDraft() && !state.draftExists) {
    // Fork from a fresh fetch of Data (rows AND evidence), not whatever's
    // already in memory -- state.evidence is a single shared field, not
    // per-source, so trusting it here risks carrying over a just-deleted
    // draft's stale evidence (e.g. "reviewed" marks) into this brand new
    // draft. Same principle switchSource's own fork branch already uses.
    const fresh = await api.getLookup("data");
    state.dataRows = fresh.rows;
    state.evidence = fresh.evidence;
    state.draftRows = fresh.rows.map((row) => ({ ...row }));
    dataByOs = new Map(state.dataRows.map((row) => [dedupeKey(row.os_string), row]));
  } else if (!isDraft() && state.draftExists) {
    const draft = await api.getLookup("draft");
    state.draftRows = draft.rows;
  }
  // `targets` deliberately still includes ambiguous rows -- this list is
  // sent to the server as-is and saved back verbatim at the end of the
  // refresh (see lookup_refresh_events), so shrinking it here would drop
  // every excluded row from the persisted draft, not just skip enriching
  // it. The server already skips ambiguous rows internally (never queries
  // a lifecycle source with them); this just makes the count honest about it.
  const isPartialRefresh = state.selected.size > 0;
  const targets = isPartialRefresh ? selectedRows() : state.draftRows;
  const ambiguousCount = targets.filter((row) => row.normalized_os_detailed_name === "Ambiguous OS").length;
  const enrichableCount = targets.length - ambiguousCount;
  document.getElementById("refresh-summary").textContent = ambiguousCount
    ? `${targets.length} row(s) selected, ${enrichableCount} eligible for refresh (${ambiguousCount} Ambiguous OS row(s) skipped). Existing EOL/EOAS values will be overwritten.`
    : `${targets.length} row(s) eligible for refresh. Existing EOL/EOAS values will be overwritten.`;
  openModal("modal-refresh");
  const bodyEl = document.getElementById("modal-refresh-body");
  const footerEl = document.getElementById("modal-refresh-footer");
  const confirmBtn = document.getElementById("refresh-confirm-btn");
  confirmBtn.onclick = () => {
    runProgress({
      kind: "refresh",
      label: "Refresh EOL/EOAS",
      bodyEl, footerEl,
      eventGenerator: streams.refreshLookup(targets, state.evidence, "draft", undefined, isPartialRefresh),
      // No inline .catch() -- tasks.js's cancelTask() awaits this to know
      // whether the cancel actually landed, re-enabling the Cancel button
      // if it rejects (job already gone, network error, etc.).
      onCancel: (jobId) => (jobId ? api.cancelRefreshJob(jobId) : undefined),
      onComplete: async (event) => {
        // Merge by os_string -- `targets` may be a selection subset, so
        // replacing the whole array here would silently drop every other
        // draft row (and any edits made to them while this was running).
        const byOs = new Map(event.rows.map((r) => [r.os_string, r]));
        state.draftRows = state.draftRows.map((r) => byOs.get(r.os_string) || r);
        state.evidence.by_os = { ...(state.evidence.by_os || {}), ...(event.evidence?.by_os || {}) };
        // If this refresh is what just created the draft (triggered with no
        // draft open yet, rather than via "Edit data"), the merge-base
        // revision was never captured on the client -- without this, the
        // staleness banner compares against a stale default and falsely
        // claims Data was published again by someone, even though nothing
        // changed at all.
        if (event.based_on_revision != null) state.draftBasedOnRevision = event.based_on_revision;
        state.draftExists = true;
        state.chip = "changed";
        setState({ source: "draft" });
        await recomputeDiff();
        renderAll();
        showToast("EOL/EOAS refresh complete.");
      },
      onError: (event) => showToast(`Refresh failed: ${event.message || "unknown error"}`),
    });
  };
}

// ---------- Add OS modal (client-orchestrated pipeline) ----------

function openAddOsModal() {
  openModal("modal-add");
  document.getElementById("add-single-input").value = "";
  document.getElementById("add-list-input").value = "";
  // Rebind fresh every open, like the other modals' confirm buttons --
  // attachProgressView can replace this button with a new DOM node (no
  // listeners of its own) between opens, so a one-time addEventListener
  // bound at module load wouldn't survive a second use of this modal.
  document.getElementById("add-confirm-btn").onclick = handleAddConfirm;
}

async function* addOsPipeline(osStrings, { signal } = {}) {
  // This pipeline has no server-side job to cancel -- it's a sequence of
  // client-issued requests. Checked at every step so Cancel takes effect
  // within one row/request instead of running the whole batch to completion.
  if (signal?.aborted) { yield { type: "cancelled" }; return; }

  yield { type: "progress", stage: "Checking for ambiguous OS strings", processed: 0, total: 4 };
  const ambiguousFlags = await api.ambiguousOsDetect(osStrings, { signal }).then((r) => r.results).catch(() => osStrings.map(() => false));
  if (signal?.aborted) { yield { type: "cancelled" }; return; }

  const allowedPairs = buildAllowedPairsFromDraft();
  const newRows = [];
  const settings = await api.getSettings().catch(() => ({ ai_enabled: false }));
  if (signal?.aborted) { yield { type: "cancelled" }; return; }

  for (let i = 0; i < osStrings.length; i += 1) {
    if (signal?.aborted) { yield { type: "cancelled" }; return; }
    const osString = osStrings[i];
    yield { type: "progress", stage: `Normalizing ${osString}`, processed: i, total: osStrings.length };
    const row = { os_string: osString, normalized_os_detailed_name: "", normalized_os: "", eol_date: "", eol_status: "", eoas_date: "", eoas_status: "" };
    if (ambiguousFlags[i]) {
      row.normalized_os_detailed_name = "Ambiguous OS";
      row.normalized_os = "Ambiguous OS";
      // Ambiguous rows are filtered out of lookupRows below (never sent for
      // refresh), so they'd otherwise never get a matched_by at all until
      // the next full page load -- set it immediately, matching what the
      // server computes via lookup_extras.row_matched_by.
      row.matched_by = "Ambiguous";
    } else {
      const match = await matchAgainstExistingPairs(osString, allowedPairs, settings, { signal });
      if (match) {
        row.normalized_os_detailed_name = match.normalized_os_detailed_name;
        row.normalized_os = match.normalized_os;
      }
    }
    newRows.push(row);
  }
  if (signal?.aborted) { yield { type: "cancelled" }; return; }

  // Batched + streamed server-side (chunks of 25) instead of one HTTP
  // round trip per row -- a 1000-row add was previously ~1000 sequential
  // requests with a single progress tick logged before the whole thing,
  // so the bar sat at a misleading ~100% for however long that took.
  const evidenceByOs = {};
  const lookupRows = newRows.filter((row) => row.normalized_os_detailed_name !== "Ambiguous OS");
  if (lookupRows.length) {
    for await (const event of streams.refreshRowsBatch(lookupRows, { signal })) {
      if (event.type === "progress") {
        yield { type: "progress", stage: "Looking up EOL/EOAS", processed: event.processed, total: event.total };
      } else if (event.type === "complete") {
        const byOs = new Map((event.rows || []).map((r) => [r.os_string, r]));
        newRows.forEach((row) => {
          const updated = byOs.get(row.os_string);
          if (updated) Object.assign(row, updated);
        });
        Object.assign(evidenceByOs, event.evidence_by_os || {});
      }
    }
  }
  if (signal?.aborted) { yield { type: "cancelled" }; return; }

  yield { type: "complete", rows: newRows, evidence: evidenceByOs, message: `Added ${newRows.length} row(s).` };
}

// Shared by the Add-OS pipeline above and rerunRow's blank-normalized-field
// case below -- tries an exact match against existing Draft/Data pairs
// first, then defers to the server for a local fuzzy match (no AI needed)
// and, only if that doesn't find one and AI is enabled, an AI match.
// Returns { normalized_os_detailed_name, normalized_os } or null.
async function matchAgainstExistingPairs(osString, allowedPairs, settings, { signal } = {}) {
  const exact = allowedPairs.find((p) => p.__key === dedupeKey(collapseConsecutiveDuplicateWords(osString)));
  if (exact) {
    return {
      normalized_os_detailed_name: collapseConsecutiveDuplicateWords(exact.normalized_os_detailed_name),
      normalized_os: collapseConsecutiveDuplicateWords(exact.normalized_os),
    };
  }
  // Always calls the server now, regardless of settings.ai_enabled -- it
  // tries a local fuzzy match against existing pairs first (no AI needed
  // at all), and only falls through to AI internally when that doesn't
  // find one and AI is actually enabled/configured.
  const threshold = settings.ai_confidence_threshold ?? 85;
  const [suggestion] = await api.normalizeSuggest([osString], allowedPairs, threshold, { signal }).then((r) => r.results).catch(() => [null]);
  if (!suggestion) return null;
  return {
    normalized_os_detailed_name: collapseConsecutiveDuplicateWords(suggestion.normalized_os_detailed_name),
    normalized_os: collapseConsecutiveDuplicateWords(suggestion.normalized_os),
  };
}

function buildAllowedPairsFromDraft() {
  const seen = new Set();
  const pairs = [];
  for (const row of state.draftRows.length ? state.draftRows : state.dataRows) {
    const detailed = String(row.normalized_os_detailed_name || "").trim();
    const normalized = String(row.normalized_os || "").trim();
    if (!detailed || !normalized) continue;
    const key = dedupeKey(collapseConsecutiveDuplicateWords(row.os_string));
    if (seen.has(key)) continue;
    seen.add(key);
    pairs.push({ normalized_os_detailed_name: detailed, normalized_os: normalized, __key: key });
  }
  return pairs;
}

async function handleAddConfirm() {
  if (hasActive("add-os")) {
    showToast("An Add OS pipeline is already running — see Background tasks.");
    return;
  }
  const activeTab = document.querySelector(".add-tab-panel:not([hidden])").id;
  let osStrings = [];
  if (activeTab === "add-panel-single") {
    const v = document.getElementById("add-single-input").value.trim();
    if (v) osStrings = [v];
  } else if (activeTab === "add-panel-list") {
    osStrings = document.getElementById("add-list-input").value.split("\n").map((s) => s.trim()).filter(Boolean);
  } else {
    osStrings = window.__oshcImportedOsStrings || [];
  }
  const existing = new Set(state.draftRows.map((r) => dedupeKey(r.os_string)));
  osStrings = [...new Set(osStrings.map((s) => s.trim()).filter(Boolean))].filter((s) => !existing.has(dedupeKey(s)));
  if (!osStrings.length) { showToast("Nothing to add — duplicates are skipped."); closeModal(); return; }

  if (!isDraft() && !state.draftExists) state.draftRows = state.dataRows.map((row) => ({ ...row }));

  const bodyEl = document.getElementById("modal-add-body");
  const footerEl = document.getElementById("modal-add-footer");
  const controller = new AbortController();
  runProgress({
    kind: "add-os",
    label: "Add OS",
    bodyEl, footerEl,
    eventGenerator: addOsPipeline(osStrings, { signal: controller.signal }),
    onCancel: () => controller.abort(),
    onComplete: async (event) => {
      state.draftRows = [...state.draftRows, ...event.rows];
      state.evidence.by_os = { ...(state.evidence.by_os || {}), ...event.evidence };
      state.draftExists = true;
      state.chip = "changed";
      setState({ source: "draft" });
      await persistDraft();
      await recomputeDiff();
      renderAll();
      showToast(event.message);
    },
  });
}

// Delegated on the modal itself (never replaced -- only its body/footer
// are) so tab switching keeps working even after a finished task's
// progress view has overwritten and later restored modal-add-body.
document.getElementById("modal-add").addEventListener("click", (event) => {
  const btn = event.target.closest("[data-add-tab]");
  if (!btn) return;
  document.querySelectorAll("[data-add-tab]").forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".add-tab-panel").forEach((p) => { p.hidden = p.id !== `add-panel-${btn.dataset.addTab}`; });
});

// ---------- Validate & publish modal ----------

/** Short human-readable summary of one side of a conflict, for the
 * resolver list. `side` is {row, evidence} (or {rows, evidence} for
 * ambiguous_duplicate), or null when that side deleted the row. */
function summarizeConflictSide(side, kind) {
  if (!side) return "(deleted)";
  if (kind === "ambiguous_duplicate") {
    const count = (side.rows || []).length;
    return `${count} row(s)`;
  }
  const row = side.row;
  if (!row) return "(deleted)";
  const name = row.normalized_os || row.normalized_os_detailed_name || "(no normalized name)";
  const eol = row.eol_date ? row.eol_date : "no EOL date";
  const eoas = row.eoas_date ? row.eoas_date : "no EOAS date";
  return `${name} — EOL ${eol}, EOAS ${eoas}`;
}

async function openValidateModal() {
  if (hasActive("publish")) {
    showToast("A publish is already running — see Background tasks.");
    return;
  }
  if (hasActive("refresh")) {
    showToast("A refresh is still running — wait for it to finish before publishing.");
    return;
  }

  openModal("modal-validate");
  const resolverEl = document.getElementById("conflict-resolver");
  const backupField = document.getElementById("backup-suffix-input");
  const confirmBtn = document.getElementById("validate-confirm-btn");
  resolverEl.hidden = true;
  confirmBtn.disabled = true;
  confirmBtn.textContent = "Checking…";
  backupField.value = "";

  const [d, conflictCheck] = await Promise.all([
    api.getDiff("draft").catch(() => ({ added_count: 0, edited_count: 0, unresolved: 0 })),
    api.checkPublishConflicts(state.draftRows, state.evidence).catch(() => ({ conflicts: [] })),
  ]);
  document.getElementById("kpi-new").textContent = String(d.added_count ?? d.added?.length ?? 0);
  document.getElementById("kpi-edited").textContent = String(d.edited_count ?? d.edited?.length ?? 0);
  document.getElementById("kpi-unresolved").textContent = String(d.unresolved ?? 0);

  const conflicts = conflictCheck.conflicts || [];
  // Default every conflict to "theirs" -- the already-published side is
  // usually the fresher one (e.g. two environments both ran Refresh
  // EOL/EOAS and published at different times; whichever landed first is
  // presumed more current).
  const resolutions = Object.fromEntries(conflicts.map((c) => [c.os_string, "theirs"]));

  // Only rows that actually changed (new/edited) need reviewing -- an
  // unchanged row has nothing new to validate against published Data. Uses
  // this fetch's own added/edited lists rather than the module-level
  // addedSet/editedSet so it can't be momentarily stale relative to `d`.
  const changedKeys = new Set([...(d.added || []), ...(d.edited || [])].map(dedupeKey));
  const changedRows = state.draftRows.filter((row) => changedKeys.has(dedupeKey(row.os_string)));
  const totalChanged = changedRows.length;
  const notReviewedCount = changedRows.filter((row) => !isRowReviewed(row)).length;
  document.getElementById("kpi-not-reviewed").textContent = String(notReviewedCount);

  const reviewAckBlock = document.getElementById("review-ack-block");
  const reviewAckChip = document.getElementById("review-ack-chip");
  const reviewAckCheckbox = document.getElementById("review-ack-checkbox");
  const reviewAckBox = reviewAckChip.querySelector(".box");
  let reviewAcknowledged = notReviewedCount === 0;
  reviewAckBlock.hidden = notReviewedCount === 0;
  reviewAckChip.classList.remove("active");
  reviewAckCheckbox.checked = false;
  reviewAckBox.innerHTML = "";
  document.getElementById("review-warning-note").textContent =
    `${notReviewedCount} of ${totalChanged} changed row(s) haven't been marked reviewed.`;
  reviewAckChip.onclick = (event) => {
    event.preventDefault();
    reviewAcknowledged = !reviewAcknowledged;
    reviewAckCheckbox.checked = reviewAcknowledged;
    reviewAckChip.classList.toggle("active", reviewAcknowledged);
    // Without this, "active" just fills the box with solid brand color and
    // no checkmark -- reads as a colored dot, not a checkbox.
    reviewAckBox.innerHTML = reviewAcknowledged ? iconMarkup("check", { size: 9 }) : "";
    updateConfirmState();
  };

  function updateConfirmState() {
    const allResolved = conflicts.every((c) => resolutions[c.os_string] === "mine" || resolutions[c.os_string] === "theirs");
    const reviewOk = notReviewedCount === 0 || reviewAcknowledged;
    confirmBtn.disabled = (conflicts.length > 0 && !allResolved) || !reviewOk;
    confirmBtn.textContent = conflicts.length > 0 ? "Resolve & publish" : "Validate and publish";
  }

  if (conflicts.length) {
    resolverEl.hidden = false;
    document.getElementById("conflict-count-note").textContent =
      `${conflicts.length} row(s) changed both here and in Data since you started this draft — pick which version to keep.`;

    const listEl = document.getElementById("conflict-list");
    listEl.innerHTML = conflicts
      .map(
        (c, i) => `
        <div class="conflict-row">
          <div class="conflict-os">${escapeHtml(c.os_string)}</div>
          <label class="conflict-option">
            <input type="radio" name="conflict-${i}" value="mine" data-os="${escapeHtml(c.os_string)}" />
            Keep mine: ${escapeHtml(summarizeConflictSide(c.mine, c.kind))}
          </label>
          <label class="conflict-option">
            <input type="radio" name="conflict-${i}" value="theirs" data-os="${escapeHtml(c.os_string)}" checked />
            Keep theirs: ${escapeHtml(summarizeConflictSide(c.theirs, c.kind))}
          </label>
        </div>`
      )
      .join("");
    listEl.querySelectorAll("input[type=radio]").forEach((input) => {
      input.addEventListener("change", () => {
        resolutions[input.dataset.os] = input.value;
        updateConfirmState();
      });
    });
    document.getElementById("conflict-apply-mine").onclick = () => {
      conflicts.forEach((c) => { resolutions[c.os_string] = "mine"; });
      listEl.querySelectorAll('input[value="mine"]').forEach((el) => { el.checked = true; });
      updateConfirmState();
    };
    document.getElementById("conflict-apply-theirs").onclick = () => {
      conflicts.forEach((c) => { resolutions[c.os_string] = "theirs"; });
      listEl.querySelectorAll('input[value="theirs"]').forEach((el) => { el.checked = true; });
      updateConfirmState();
    };
  }
  updateConfirmState();

  const bodyEl = document.getElementById("modal-validate-body");
  const footerEl = document.getElementById("modal-validate-footer");
  confirmBtn.onclick = () => {
    const suffix = backupField.value.trim();
    resolverEl.hidden = true;
    runProgress({
      kind: "publish",
      label: "Validate & publish",
      bodyEl, footerEl,
      eventGenerator: streams.validatePublish(state.draftRows, state.evidence, suffix, resolutions),
      // Backup + write + delete-draft each complete as one atomic file
      // operation on the server -- there's no safe midpoint to actually
      // stop at, so offering a Cancel button here would be a broken
      // promise rather than a real one. No AbortController wiring on purpose.
      cancellable: false,
      onComplete: async () => {
        await loadData();
        state.draftRows = [];
        state.draftExists = false;
        backToData();
        clearSelection();
        renderAll();
        showToast("Published. Draft deleted.");
      },
      onError: (event) => showToast(`Publish failed: ${event.message || "unknown error"}`),
    });
  };
}

function openDeleteDraftModal() {
  if (hasActive("refresh")) {
    showToast("A refresh is still running — wait for it to finish before deleting the draft.");
    return;
  }
  openModal("modal-delete-draft");
  document.getElementById("delete-draft-confirm-btn").onclick = async () => {
    await api.deleteDraft();
    state.draftRows = [];
    // Reuse loadData (the same fresh re-fetch Publish's own completion
    // handler already does) rather than only flipping draftExists/source
    // locally -- state.evidence is a single shared field, not per-source,
    // so left alone it keeps whatever the just-deleted draft's evidence
    // was (e.g. "reviewed" marks). Real incident: those marks then
    // silently resurfaced in the NEXT draft created from here, since
    // Refresh EOL/EOAS forks a new draft using whatever state.evidence
    // already holds in memory, not a fresh fetch.
    await loadData();
    backToData();
    clearSelection();
    renderAll();
    closeModal();
    showToast("Draft deleted.");
  };
}

// ---------- Revert all changes (whole draft back to Data) ----------

function openRevertDraftModal() {
  if (hasActive("refresh")) {
    showToast("A refresh is still running — wait for it to finish before reverting the draft.");
    return;
  }
  openModal("modal-revert-draft");
  document.getElementById("revert-draft-confirm-btn").onclick = async () => {
    // Re-fetch Data fresh rather than trusting state.evidence, which may
    // still hold draft evidence from before this session's last switch.
    const dataSnapshot = await api.getLookup("data");
    state.draftRows = dataSnapshot.rows.map((row) => ({ ...row }));
    state.evidence = dataSnapshot.evidence;
    state.draftBasedOnRevision = dataSnapshot.data_revision ?? 0;
    clearSelection();
    // Revert re-syncs the draft to current Data, so the merge base must
    // reset to this same snapshot too -- otherwise a later publish would
    // still diff against the draft's original (now stale) starting point.
    await persistDraft({ baseRows: dataSnapshot.rows, baseEvidence: dataSnapshot.evidence, resetBase: true });
    await recomputeDiff();
    renderAll();
    closeModal();
    showToast("Draft reverted to Data.");
  };
}
