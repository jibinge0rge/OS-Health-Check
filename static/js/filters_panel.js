// Column filters panel: OS/detailed/norm text modes, EOL/EOAS date range,
// Matched-by single-select. Everything here ANDs together, and ANDs with
// the toolbar search box + active quick chip (applied in editor.js).

import { state, setState } from "./state.js";
import { parseRowDate, toISODate } from "./date_utils.js";
import { initDateRangePicker } from "./date_range_picker.js";

const TEXT_MODES = [
  ["all", "All"], ["contains", "Contains"], ["excludes", "Excludes"],
  ["equals", "Equals"], ["empty", "Empty"], ["not_empty", "Not empty"],
];
const DATE_FIELDS = new Set(["eol", "eoas"]);
const MATCHED_BY_CHIPS = [
  "All", "endoflife.date", "Fuzzy", "AI", "eosl.date", "Microsoft Lifecycle",
  "Juniper Junos", "SUSE Lifecycle", "Manual", "Ambiguous", "No match",
];

const FIELD_TO_ROW_KEY = { os: "os_string", detailed: "normalized_os_detailed_name", norm: "normalized_os" };

let onChange = () => {};
const rangePickers = {};

export function initFiltersPanel({ onFilterChange }) {
  onChange = onFilterChange || (() => {});

  document.querySelectorAll(".filter-section[data-filter-field]").forEach((section) => {
    const field = section.dataset.filterField;
    if (field === "src") return;

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
      state.f.src = btn.dataset.src;
      syncMatchedByUI();
      onChange();
    });
  });
  syncMatchedByUI();

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

function syncMatchedByUI() {
  document.querySelectorAll("#matched-by-chips .filter-mode-chip").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.src === state.f.src);
  });
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
    src: "All",
  };
  document.querySelectorAll(".filter-section[data-filter-field]").forEach((section) => {
    const field = section.dataset.filterField;
    if (field === "src") return;
    if (DATE_FIELDS.has(field)) {
      rangePickers[field]?.setValue("", "");
      return;
    }
    syncSectionUI(section, field);
    const textInput = section.querySelector("[data-filter-text]");
    if (textInput) textInput.value = "";
  });
  syncMatchedByUI();
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

export function matchesColumnFilters(row) {
  if (!textFieldMatches(row, "os")) return false;
  if (!textFieldMatches(row, "detailed")) return false;
  if (!textFieldMatches(row, "norm")) return false;
  if (!dateFieldMatches(row, "eol")) return false;
  if (!dateFieldMatches(row, "eoas")) return false;
  if (state.f.src && state.f.src !== "All" && row.matched_by !== state.f.src) return false;
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
  if (state.f.src && state.f.src !== "All") count += 1;
  return count;
}
