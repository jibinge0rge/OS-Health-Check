// Column filters panel: OS/detailed/norm text modes, EOL/EOAS date range,
// Matched-by + Type-of-change multi-select. Everything here ANDs together
// (across sections), and ANDs with the toolbar search box + active quick
// chip (applied in editor.js). Within Matched-by/Type-of-change, multiple
// selected chips OR together -- e.g. selecting both "eosl.date" and
// "Manual" shows rows matched by either.

import { state, setState, isDraft } from "./state.js";
import { parseRowDate, toISODate } from "./date_utils.js";
import { initDateRangePicker } from "./date_range_picker.js";

const TEXT_MODES = [
  ["all", "All"], ["contains", "Contains"], ["excludes", "Excludes"],
  ["equals", "Equals"], ["empty", "Empty"], ["not_empty", "Not empty"],
];
const DATE_FIELDS = new Set(["eol", "eoas"]);
// "All" is a reset chip, not a real category -- clicking it clears the
// multi-select (state.f.src = []) the same way an empty selection already
// means "no restriction."
const MATCHED_BY_CHIPS = [
  "All", "endoflife.date", "Fuzzy", "AI", "eosl.date", "Microsoft Lifecycle",
  "Juniper Junos", "SUSE Lifecycle", "Manual", "Ambiguous", "No match",
];

const FIELD_TO_ROW_KEY = { os: "os_string", detailed: "normalized_os_detailed_name", norm: "normalized_os" };

// Draft-only. "any" is a reset chip (same role as Matched-by's "All"); every
// other option maps to exactly one CSV_HEADERS field (see editor.js's
// changedFields) that must appear in a row's diff against Data for it to
// match that option.
const CHANGED_FIELD_OPTIONS = [
  ["any", "Any"],
  ["eol_date", "EOL Date"],
  ["eoas_date", "EOAS Date"],
  ["eol_status", "EOL Status"],
  ["eoas_status", "EOAS Status"],
  ["detailed", "Normalized detailed name"],
  ["norm", "Normalized OS"],
];
const CHANGED_FIELD_TO_ROW_FIELD = {
  eol_date: "eol_date",
  eoas_date: "eoas_date",
  eol_status: "eol_status",
  eoas_status: "eoas_status",
  detailed: "normalized_os_detailed_name",
  norm: "normalized_os",
};

let onChange = () => {};
let getChangedFields = () => [];
const rangePickers = {};

export function initFiltersPanel({ onFilterChange, getChangedFields: getChangedFieldsFn }) {
  onChange = onFilterChange || (() => {});
  getChangedFields = getChangedFieldsFn || (() => []);

  document.querySelectorAll(".filter-section[data-filter-field]").forEach((section) => {
    const field = section.dataset.filterField;
    if (field === "src" || field === "changed") return;

    if (DATE_FIELDS.has(field)) {
      const trigger = section.querySelector("[data-range-trigger]");
      rangePickers[field] = initDateRangePicker(
        trigger,
        { from: state.f[field].from, to: state.f[field].to },
        (range) => {
          state.f[field].from = range.from;
          state.f[field].to = range.to;
          onChange();
        }
      );
      return;
    }

    const chipsWrap = section.querySelector(".filter-mode-chips");
    const modes = chipsWrap.dataset.modes.split(",");
    chipsWrap.innerHTML = modes
      .map((mode) => {
        const label = (TEXT_MODES.find(([key]) => key === mode) || [mode, mode])[1];
        return `<button type="button" class="filter-mode-chip" data-mode="${mode}">${label}</button>`;
      })
      .join("");
    chipsWrap.querySelectorAll(".filter-mode-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.f[field].mode = btn.dataset.mode;
        syncSectionUI(section, field);
        onChange();
      });
    });

    const textInput = section.querySelector("[data-filter-text]");
    textInput.addEventListener("input", () => {
      state.f[field].text = textInput.value;
      textInput.placeholder = state.f[field].mode === "equals" ? "Exact value" : "Contains…";
      onChange();
    });
    syncSectionUI(section, field);
  });

  const matchedByWrap = document.getElementById("matched-by-chips");
  matchedByWrap.innerHTML = MATCHED_BY_CHIPS.map(
    (label) => `<button type="button" class="filter-mode-chip" data-src="${label}">${label}</button>`
  ).join("");
  matchedByWrap.querySelectorAll(".filter-mode-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      toggleMultiSelect(state.f.src, "All", btn.dataset.src);
      syncMatchedByUI();
      onChange();
    });
  });
  syncMatchedByUI();

  const changedFieldWrap = document.getElementById("changed-field-chips");
  changedFieldWrap.innerHTML = CHANGED_FIELD_OPTIONS.map(
    ([mode, label]) => `<button type="button" class="filter-mode-chip" data-changed-mode="${mode}">${label}</button>`
  ).join("");
  changedFieldWrap.querySelectorAll(".filter-mode-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      // EXACT only ever tests one field at a time -- see toggleSingleSelect.
      if (state.f.changedMatch === "exact") {
        toggleSingleSelect(state.f.changed, "any", btn.dataset.changedMode);
      } else {
        toggleMultiSelect(state.f.changed, "any", btn.dataset.changedMode);
      }
      syncChangedFieldUI();
      onChange();
    });
  });
  syncChangedFieldUI();

  document.querySelectorAll("#changed-match-mode .segment").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.f.changedMatch = btn.dataset.changedMatch;
      // Switching into EXACT with 2+ chips already selected would be
      // ambiguous (EXACT is single-select) -- keep just the first.
      if (state.f.changedMatch === "exact" && state.f.changed.length > 1) {
        state.f.changed.length = 1;
      }
      syncChangedMatchUI();
      syncChangedFieldUI();
      onChange();
    });
  });
  syncChangedMatchUI();

  document.getElementById("filters-clear-btn").addEventListener("click", () => {
    clearAllColumnFilters();
    onChange();
  });
  document.getElementById("filters-collapse-btn").addEventListener("click", () => {
    setState({ filterPanel: false });
    document.getElementById("filters-panel").hidden = true;
    document.getElementById("column-filters-btn").classList.remove("is-open");
    onChange();
  });
}

function syncSectionUI(section, field) {
  section.querySelectorAll(".filter-mode-chip[data-mode]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === state.f[field].mode);
  });
}

/** Shared multi-select toggle for Matched-by/Type-of-change: clicking the
 * reset chip (Matched-by's "All", Type-of-change's "any") clears the whole
 * selection; clicking any other chip adds it if it isn't selected yet, or
 * removes it (turning that filter back off) if it's already selected --
 * mutates `list` in place since it's the actual state.f.src/state.f.changed
 * array reference, not a copy. */
function toggleMultiSelect(list, resetValue, clicked) {
  if (clicked === resetValue) {
    list.length = 0;
    return;
  }
  const idx = list.indexOf(clicked);
  if (idx === -1) list.push(clicked);
  else list.splice(idx, 1);
}

/** Type-of-change's EXACT mode tests one field at a time (a row's diff must
 * equal exactly {clicked}, so testing 2+ at once would never match anything
 * with today's data model) -- clicking a chip replaces whatever was selected
 * instead of adding to it; clicking the already-selected chip again (or the
 * reset chip) clears the selection. Mutates `list` in place. */
function toggleSingleSelect(list, resetValue, clicked) {
  const wasOnlySelected = list.length === 1 && list[0] === clicked;
  list.length = 0;
  if (clicked !== resetValue && !wasOnlySelected) list.push(clicked);
}

function syncMatchedByUI() {
  document.querySelectorAll("#matched-by-chips .filter-mode-chip").forEach((btn) => {
    const value = btn.dataset.src;
    const active = value === "All" ? state.f.src.length === 0 : state.f.src.includes(value);
    btn.classList.toggle("active", active);
  });
}

function syncChangedFieldUI() {
  document.querySelectorAll("#changed-field-chips .filter-mode-chip").forEach((btn) => {
    const mode = btn.dataset.changedMode;
    const active = mode === "any" ? state.f.changed.length === 0 : state.f.changed.includes(mode);
    btn.classList.toggle("active", active);
  });
}

function syncChangedMatchUI() {
  document.querySelectorAll("#changed-match-mode .segment").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.changedMatch === state.f.changedMatch);
  });
}

/** The "Type of change" section is Draft-only -- there's nothing to have
 * changed in read-only Data -- so it's hidden entirely rather than left
 * visible-but-inert, matching how the quick chips row already hides its own
 * "Changed"/"Not reviewed" chips outside Draft. */
export function syncChangedFilterVisibility() {
  document.getElementById("changed-filter-section").hidden = !isDraft();
}

export function toggleFiltersPanel(force) {
  const panel = document.getElementById("filters-panel");
  const next = force != null ? force : panel.hidden;
  panel.hidden = !next;
  setState({ filterPanel: next });
  document.getElementById("column-filters-btn").classList.toggle("is-open", next);
}

export function clearAllColumnFilters() {
  state.f = {
    os: { mode: "all", text: "" },
    detailed: { mode: "all", text: "" },
    norm: { mode: "all", text: "" },
    eol: { from: "", to: "" },
    eoas: { from: "", to: "" },
    src: [],
    changed: [],
    changedMatch: "or",
  };
  document.querySelectorAll(".filter-section[data-filter-field]").forEach((section) => {
    const field = section.dataset.filterField;
    if (field === "src" || field === "changed") return;
    if (DATE_FIELDS.has(field)) {
      rangePickers[field]?.setValue("", "");
      return;
    }
    syncSectionUI(section, field);
    const textInput = section.querySelector("[data-filter-text]");
    if (textInput) textInput.value = "";
  });
  syncMatchedByUI();
  syncChangedFieldUI();
  syncChangedMatchUI();
}

function textFieldMatches(row, field) {
  const filter = state.f[field];
  const value = String(row[FIELD_TO_ROW_KEY[field]] ?? "").trim().toLowerCase();
  const needle = String(filter.text ?? "").trim().toLowerCase();
  switch (filter.mode) {
    case "contains": return !needle || value.includes(needle);
    case "excludes": return !needle || !value.includes(needle);
    case "equals": return !needle || value === needle;
    case "empty": return value === "";
    case "not_empty": return value !== "";
    default: return true;
  }
}

function dateFieldMatches(row, field) {
  const filter = state.f[field];
  if (!filter.from && !filter.to) return true;
  const dateKey = field === "eol" ? "eol_date" : "eoas_date";
  const d = parseRowDate(row[dateKey]);
  if (!d) return false;

  const iso = toISODate(d);
  if (filter.from && iso < filter.from) return false;
  if (filter.to && iso > filter.to) return false;
  return true;
}

/** Draft-only: does this row's diff against Data satisfy the selected
 * "Type of change" categories, combined per state.f.changedMatch? An
 * unchanged row (or one with no Data baseline at all, e.g. a brand-new row)
 * has nothing in getChangedFields, so it's correctly excluded by every mode
 * whenever at least one category is selected. An empty selection means no
 * restriction, regardless of match mode.
 *
 * - "or": changed in at least one selected field (others may differ too).
 * - "and": changed in every selected field (others may differ too).
 * - "exact": changed in exactly the selected fields, and nothing else. The
 *   UI restricts EXACT to a single chip (see toggleSingleSelect) since a
 *   row's diff can't equal 2+ fields' worth of "exactly this" at once.
 */
function changedFieldMatches(row) {
  if (!isDraft()) return true;
  const selected = state.f.changed;
  if (!selected || selected.length === 0) return true;
  const changedSet = new Set(getChangedFields(row).map((change) => change.field));
  const selectedFields = selected.map((mode) => CHANGED_FIELD_TO_ROW_FIELD[mode]);
  switch (state.f.changedMatch) {
    case "and":
      return selectedFields.every((field) => changedSet.has(field));
    case "exact":
      return changedSet.size === selectedFields.length && selectedFields.every((field) => changedSet.has(field));
    default:
      return selectedFields.some((field) => changedSet.has(field));
  }
}

export function matchesColumnFilters(row) {
  if (!textFieldMatches(row, "os")) return false;
  if (!textFieldMatches(row, "detailed")) return false;
  if (!textFieldMatches(row, "norm")) return false;
  if (!dateFieldMatches(row, "eol")) return false;
  if (!dateFieldMatches(row, "eoas")) return false;
  if (state.f.src && state.f.src.length > 0 && !state.f.src.includes(row.matched_by)) return false;
  if (!changedFieldMatches(row)) return false;
  return true;
}

export function activeFilterCount() {
  let count = 0;
  for (const field of ["os", "detailed", "norm"]) {
    if (state.f[field].mode !== "all") count += 1;
  }
  for (const field of ["eol", "eoas"]) {
    const f = state.f[field];
    if (f.from || f.to) count += 1;
  }
  if (state.f.src && state.f.src.length > 0) count += 1;
  if (state.f.changed && state.f.changed.length > 0 && isDraft()) count += 1;
  return count;
}
