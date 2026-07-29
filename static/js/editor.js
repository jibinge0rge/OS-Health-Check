// Lookup editor: mode bar, toolbar, bulk bar, table, footer.

import { state, setState, rows as currentRows, isDraft, isData } from "./state.js";
import { api, streams } from "./api.js";
import { iconMarkup } from "./icons.js";
import { parseRowDate, classifyDateChip, formatRelative } from "./date_utils.js";
import { initFiltersPanel, toggleFiltersPanel, matchesColumnFilters, activeFilterCount, clearAllColumnFilters } from "./filters_panel.js";
import { initDrawer, openDrawer, closeDrawer, isDrawerOpenFor, refreshDrawerFields } from "./drawer.js";
import { openModal, closeModal, showToast, runProgress } from "./modals.js";
import { hasActive } from "./tasks.js";

const CSV_HEADERS = ["os_string", "normalized_os_detailed_name", "normalized_os", "eol_date", "eol_status", "eoas_date", "eoas_status"];
const PAGE_SIZE_OPTIONS = [50, 100, 250, 500, 1000];

let diff = { added: [], edited: [], deleted: [], unresolved: 0 };
let addedSet = new Set();
let editedSet = new Set();
let dataByOs = new Map();
let page = 1;
let pageSizeIndex = 1;
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
  exportBtn: document.getElementById("export-btn"),
  columnFiltersBtn: document.getElementById("column-filters-btn"),
  filterBadge: document.getElementById("filter-count-badge"),
  bulkBar: document.getElementById("bulk-bar"),
  bulkCount: document.getElementById("bulk-count"),
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

  initFiltersPanel({ onFilterChange: () => { clearSelection(); renderAll(); } });
  initDrawer({
    onFieldChange: () => scheduleAutosave(),
    onSameAsOs: (row) => { applySameAsOs(row); scheduleAutosave(); refreshView(); },
    onRerun: (row) => rerunRow(row),
    onRevert: (row) => { revertRow(row); scheduleAutosave(); refreshView(); },
  });

  el.segData.addEventListener("click", () => switchSource("data"));
  el.segDraft.addEventListener("click", () => { if (state.draftExists || isDraft()) switchSource("draft"); });

  el.search.addEventListener("input", () => { state.search = el.search.value; clearSelection(); renderAll(); });
  el.columnFiltersBtn.addEventListener("click", () => toggleFiltersPanel());
  el.refreshBtn.addEventListener("click", () => openRefreshModal());
  el.addOsBtn.addEventListener("click", () => openAddOsModal());
  el.exportBtn.addEventListener("click", () => exportRows(visibleRowsUnpaged()));
  document.getElementById("clear-filters-btn").addEventListener("click", () => {
    state.search = ""; el.search.value = ""; state.chip = "all";
    clearAllColumnFilters(); clearSelection(); renderAll();
  });

  document.getElementById("bulk-refresh-btn").addEventListener("click", bulkRefresh);
  document.getElementById("bulk-same-as-os-btn").addEventListener("click", bulkSameAsOs);
  document.getElementById("bulk-revert-btn").addEventListener("click", bulkRevert);
  document.getElementById("bulk-export-btn").addEventListener("click", () => exportRows(selectedRows()));
  document.getElementById("bulk-delete-btn").addEventListener("click", bulkDelete);
  document.getElementById("bulk-clear-btn").addEventListener("click", clearSelection);

  document.getElementById("footer-prev-btn").addEventListener("click", () => { if (page > 1) { page -= 1; renderTable(); } });
  document.getElementById("footer-next-btn").addEventListener("click", () => { page += 1; renderTable(); });
  el.rowsPerPageChip.addEventListener("click", () => {
    pageSizeIndex = (pageSizeIndex + 1) % PAGE_SIZE_OPTIONS.length;
    el.rowsPerPageChip.textContent = String(PAGE_SIZE_OPTIONS[pageSizeIndex]);
    page = 1; renderTable();
  });

  await loadData();
  renderAll();
}

async function loadData() {
  const data = await api.getLookup("data");
  state.dataRows = data.rows;
  state.evidence = data.evidence;
  state.draftExists = data.draft_exists;
  state.publishedAt = data.published_at;
  dataByOs = new Map(state.dataRows.map((row) => [dedupeKey(row.os_string), row]));
  el.railRowCount.textContent = String(state.dataRows.length);
  el.publishedAtMeta.textContent = state.publishedAt
    ? `Lookup published ${formatPublishedAt(state.publishedAt)}`
    : "Lookup published —";
}

function formatPublishedAt(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function dedupeKey(v) { return String(v || "").trim().toLowerCase(); }

/** Return to Data after Exit draft / Delete draft / Publish — resets the
 * quick chip so the table doesn't look empty (Data has no "changed" chip). */
function backToData() {
  if (state.chip === "changed") state.chip = "all";
  setState({ source: "data" });
}

async function switchSource(target) {
  if (target === state.source) return;
  if (target === "draft") {
    if (state.draftExists) {
      const draft = await api.getLookup("draft");
      state.draftRows = draft.rows;
      state.evidence = draft.evidence;
    } else {
      state.draftRows = state.dataRows.map((row) => ({ ...row }));
      await persistDraft();
      state.draftExists = true;
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

async function persistDraft() {
  await api.saveLookup(state.draftRows, state.evidence, "draft");
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
  const words = String(text || "").trim().split(/\s+/);
  const out = [];
  for (const word of words) {
    const prev = out[out.length - 1];
    if (prev && prev.toLowerCase() === word.toLowerCase()) continue;
    out.push(word);
  }
  return out.join(" ");
}

function applySameAsOs(row) {
  const collapsed = collapseConsecutiveDuplicateWords(row.os_string);
  row.normalized_os_detailed_name = collapsed;
  row.normalized_os = collapsed;
}

function revertRow(row) {
  const baseline = dataByOs.get(dedupeKey(row.os_string));
  if (!baseline) return;
  CSV_HEADERS.forEach((header) => { if (header !== "os_string") row[header] = baseline[header]; });
}

async function rerunRow(row) {
  showToast("Re-running lookup…");
  try {
    const result = await api.refreshRow(row);
    Object.assign(row, result.row);
    scheduleAutosave();
    refreshView();
    refreshDrawerFields(row);
    showToast("Lookup refreshed.");
  } catch (error) {
    showToast(`Re-run failed: ${error.message}`);
  }
}

function exportRows(list) {
  const csvHeaderLine = CSV_HEADERS.join(",");
  const lines = list.map((row) => CSV_HEADERS.map((h) => csvEscape(row[h])).join(","));
  const blob = new Blob([csvHeaderLine + "\n" + lines.join("\n") + "\n"], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "eol_lookup_export.csv";
  a.click();
  URL.revokeObjectURL(url);
}

function csvEscape(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

// ---------- Selection ----------

function clearSelection() { state.selected.clear(); updateBulkBar(); }
function selectedRows() { return currentRows().filter((row) => state.selected.has(row.os_string)); }

function updateBulkBar() {
  const n = state.selected.size;
  el.bulkBar.hidden = !(isDraft() && n > 0);
  el.bulkCount.textContent = `${n} selected`;
}

/** Select-all applies to the filtered set, not the whole file (matches the
 * quick-chip counts and the design spec). */
function toggleSelectAllFiltered(filtered) {
  const allSelected = filtered.length > 0 && filtered.every((r) => state.selected.has(r.os_string));
  if (allSelected) {
    filtered.forEach((r) => state.selected.delete(r.os_string));
  } else {
    filtered.forEach((r) => state.selected.add(r.os_string));
  }
  updateBulkBar();
  renderTable();
}

async function bulkRefresh() {
  const targets = selectedRows();
  if (!targets.length) return;
  showToast(`Refreshing ${targets.length} row(s)…`);
  const result = await api.refreshRows(targets);
  result.rows.forEach((updated, i) => Object.assign(targets[i], updated));
  scheduleAutosave();
  refreshView();
  showToast(`Refreshed ${targets.length} row(s).`);
}

function bulkSameAsOs() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach(applySameAsOs);
  scheduleAutosave();
  refreshView();
  showToast(`Set ${targets.length} row(s) to match their OS string.`);
}

function bulkRevert() {
  const targets = selectedRows();
  if (!targets.length) return;
  targets.forEach(revertRow);
  scheduleAutosave();
  refreshView();
  showToast(`Reverted ${targets.length} row(s) to Data.`);
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
];

function isAmbiguousRow(row) {
  const d = String(row.normalized_os_detailed_name || "").trim().toLowerCase();
  const n = String(row.normalized_os || "").trim().toLowerCase();
  return d === "ambiguous os" || n === "ambiguous os";
}

function rowMatchesChip(row, chip) {
  switch (chip) {
    case "missing": return !String(row.normalized_os_detailed_name || "").trim() || !String(row.normalized_os || "").trim();
    case "eol": { const d = parseRowDate(row.eol_date); return d && d.getTime() < Date.now(); }
    case "eoas": { const d = parseRowDate(row.eoas_date); return d && d.getTime() < Date.now(); }
    case "nodates": return !String(row.eol_date || "").trim() && !String(row.eoas_date || "").trim();
    case "ambiguous": return isAmbiguousRow(row);
    case "changed": return isDraft() && (addedSet.has(dedupeKey(row.os_string)) || editedSet.has(dedupeKey(row.os_string)));
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
  const chips = isDraft() ? QUICK_CHIPS : QUICK_CHIPS.filter(([key]) => key !== "changed");
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

  const columnHeadersHtml = SORTABLE_COLUMNS.map(columnHeaderHtml);
  if (isDraft()) {
    const allSelected = filtered.length > 0 && filtered.every((r) => state.selected.has(r.os_string));
    const headerCheckboxHtml = `<span class="row-checkbox ${allSelected ? "checked" : ""}" id="header-select-all" title="${allSelected ? "Deselect all" : "Select all filtered rows"}">${iconMarkup("check", { size: 10 })}</span>`;
    el.tableHeaderRow.innerHTML = [headerCheckboxHtml, ...columnHeadersHtml].join("");
    document.getElementById("header-select-all").addEventListener("click", () => toggleSelectAllFiltered(filtered));
  } else {
    el.tableHeaderRow.innerHTML = columnHeadersHtml.join("");
  }
  bindSortHeaders();

  const size = PAGE_SIZE_OPTIONS[pageSizeIndex];
  const totalPages = Math.max(1, Math.ceil(filtered.length / size));
  if (page > totalPages) page = totalPages;
  const pageRows = filtered.slice((page - 1) * size, page * size);

  el.tableBody.innerHTML = "";
  el.tableEmpty.hidden = filtered.length !== 0;
  pageRows.forEach((row) => el.tableBody.appendChild(renderRow(row)));

  renderFooter(filtered.length, currentRows().length);
  el.footerPage.textContent = `Page ${page} of ${totalPages}`;
  document.getElementById("footer-prev-btn").disabled = page <= 1;
  document.getElementById("footer-next-btn").disabled = page >= totalPages;
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
    else if (editedSet.has(key)) flag = `<span class="row-flag edited">EDITED</span>`;
  }

  const cells = [];
  if (isDraft()) {
    cells.push(`<span class="row-checkbox ${state.selected.has(row.os_string) ? "checked" : ""}" data-role="select">${iconMarkup("check", { size: 10 })}</span>`);
  }
  cells.push(`<div class="cell-os">${flag}<span class="os-value" title="${escapeHtml(row.os_string)}">${escapeHtml(row.os_string)}</span></div>`);
  cells.push(textCellHtml(row.normalized_os_detailed_name));
  cells.push(textCellHtml(row.normalized_os));
  cells.push(dateCellHtml(row, "eol_date", "eol"));
  cells.push(statusCellHtml(row, "eol_status"));
  cells.push(dateCellHtml(row, "eoas_date", "eoas"));
  cells.push(statusCellHtml(row, "eoas_status"));
  wrap.innerHTML = cells.join("");

  wrap.addEventListener("click", (event) => {
    if (event.target.closest("[data-role='select']")) return;
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

  return wrap;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------- Refresh EOL/EOAS modal ----------

async function openRefreshModal() {
  if (hasActive("refresh")) {
    showToast("A refresh is already running — see Background tasks.");
    return;
  }
  if (!isDraft() && !state.draftExists) {
    state.draftRows = state.dataRows.map((row) => ({ ...row }));
  } else if (!isDraft() && state.draftExists) {
    const draft = await api.getLookup("draft");
    state.draftRows = draft.rows;
  }
  const targets = state.selected.size ? selectedRows() : state.draftRows;
  document.getElementById("refresh-summary").textContent =
    `${targets.length} row(s) eligible for refresh. Existing EOL/EOAS values will be overwritten.`;
  openModal("modal-refresh");
  const bodyEl = document.getElementById("modal-refresh-body");
  const footerEl = document.getElementById("modal-refresh-footer");
  const confirmBtn = document.getElementById("refresh-confirm-btn");
  confirmBtn.onclick = () => {
    runProgress({
      kind: "refresh",
      label: "Refresh EOL/EOAS",
      bodyEl, footerEl,
      eventGenerator: streams.refreshLookup(targets, state.evidence, "draft"),
      onCancel: (jobId) => jobId && api.cancelRefreshJob(jobId).catch(() => {}),
      onComplete: async (event) => {
        // Merge by os_string -- `targets` may be a selection subset, so
        // replacing the whole array here would silently drop every other
        // draft row (and any edits made to them while this was running).
        const byOs = new Map(event.rows.map((r) => [r.os_string, r]));
        state.draftRows = state.draftRows.map((r) => byOs.get(r.os_string) || r);
        state.evidence.by_os = { ...(state.evidence.by_os || {}), ...(event.evidence?.by_os || {}) };
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
    } else {
      const exact = allowedPairs.find((p) => p.__key === dedupeKey(collapseConsecutiveDuplicateWords(osString)));
      if (exact) {
        row.normalized_os_detailed_name = exact.normalized_os_detailed_name;
        row.normalized_os = exact.normalized_os;
      } else if (settings.ai_enabled) {
        const threshold = settings.ai_confidence_threshold ?? 85;
        const [suggestion] = await api.normalizeSuggest([osString], allowedPairs, threshold, { signal }).then((r) => r.results).catch(() => [null]);
        if (suggestion) {
          row.normalized_os_detailed_name = suggestion.normalized_os_detailed_name;
          row.normalized_os = suggestion.normalized_os;
        }
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

async function openValidateModal() {
  if (hasActive("publish")) {
    showToast("A publish is already running — see Background tasks.");
    return;
  }
  if (hasActive("refresh")) {
    showToast("A refresh is still running — wait for it to finish before publishing.");
    return;
  }
  const d = await api.getDiff("draft").catch(() => ({ added_count: 0, edited_count: 0, unresolved: 0 }));
  document.getElementById("kpi-new").textContent = String(d.added_count ?? d.added?.length ?? 0);
  document.getElementById("kpi-edited").textContent = String(d.edited_count ?? d.edited?.length ?? 0);
  document.getElementById("kpi-unresolved").textContent = String(d.unresolved ?? 0);
  document.getElementById("backup-suffix-input").value = "";
  openModal("modal-validate");

  const bodyEl = document.getElementById("modal-validate-body");
  const footerEl = document.getElementById("modal-validate-footer");
  document.getElementById("validate-confirm-btn").onclick = () => {
    const suffix = document.getElementById("backup-suffix-input").value.trim();
    runProgress({
      kind: "publish",
      label: "Validate & publish",
      bodyEl, footerEl,
      eventGenerator: streams.validatePublish(state.draftRows, state.evidence, suffix),
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
    state.draftExists = false;
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
    clearSelection();
    await persistDraft();
    await recomputeDiff();
    renderAll();
    closeModal();
    showToast("Draft reverted to Data.");
  };
}
