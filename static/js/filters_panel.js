// Column filters panel: OS/detailed/norm text modes, EOL/EOAS date modes +
// range + status, Matched-by single-select. Everything here ANDs together,
// and ANDs with the toolbar search box + active quick chip (applied in
// editor.js).

import { state, setState } from "./state.js";
import { parseRowDate, toISODate } from "./date_utils.js";

const TEXT_MODES = [
  ["all", "All"], ["contains", "Contains"], ["excludes", "Excludes"],
  ["equals", "Equals"], ["empty", "Empty"], ["not_empty", "Not empty"],
];
const DATE_MODES = [
  ["all", "All"], ["passed", "Passed"], ["upcoming", "Upcoming"],
  ["empty", "Empty"], ["not_empty", "Not empty"],
];
const MATCHED_BY_CHIPS = [
  "All", "endoflife.date", "Fuzzy", "AI", "eosl.date", "Microsoft Lifecycle",
  "Juniper Junos", "SUSE Lifecycle", "Manual", "Ambiguous", "No match",
];

const FIELD_TO_ROW_KEY = { os: "os_string", detailed: "normalized_os_detailed_name", norm: "normalized_os" };

let onChange = () => {};

export function initFiltersPanel({ onFilterChange }) {
  onChange = onFilterChange || (() => {});

  document.querySelectorAll(".filter-section[data-filter-field]").forEach((section) => {
    const field = section.dataset.filterField;
    if (field === "src") return;
    const chipsWrap = section.querySelector(".filter-mode-chips");
    const modes = chipsWrap.dataset.modes.split(",");
    const isDate = field === "eol" || field === "eoas";
    const table = isDate ? DATE_MODES : TEXT_MODES;
    chipsWrap.innerHTML = modes
      .map((mode) => {
        const label = (table.find(([key]) => key === mode) || [mode, mode])[1];
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

    if (!isDate) {
      const textInput = section.querySelector("[data-filter-text]");
      textInput.addEventListener("input", () => {
        state.f[field].text = textInput.value;
        textInput.placeholder = state.f[field].mode === "equals" ? "Exact value" : "Contains…";
        onChange();
      });
    } else {
      const fromInput = section.querySelector("[data-filter-from]");
      const toInput = section.querySelector("[data-filter-to]");
      const statusSelect = section.querySelector("[data-filter-status]");
      fromInput.addEventListener("change", () => { state.f[field].from = fromInput.value; onChange(); });
      toInput.addEventListener("change", () => { state.f[field].to = toInput.value; onChange(); });
      statusSelect.addEventListener("change", () => { state.f[field].status = statusSelect.value; onChange(); });
    }
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
    eol: { mode: "all", from: "", to: "", status: "any" },
    eoas: { mode: "all", from: "", to: "", status: "any" },
    src: "All",
  };
  document.querySelectorAll(".filter-section[data-filter-field]").forEach((section) => {
    const field = section.dataset.filterField;
    if (field === "src") return;
    syncSectionUI(section, field);
    const textInput = section.querySelector("[data-filter-text]");
    if (textInput) textInput.value = "";
    const fromInput = section.querySelector("[data-filter-from]");
    const toInput = section.querySelector("[data-filter-to]");
    const statusSelect = section.querySelector("[data-filter-status]");
    if (fromInput) fromInput.value = "";
    if (toInput) toInput.value = "";
    if (statusSelect) statusSelect.value = "any";
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
  const dateKey = field === "eol" ? "eol_date" : "eoas_date";
  const statusKey = field === "eol" ? "eol_status" : "eoas_status";
  const d = parseRowDate(row[dateKey]);

  switch (filter.mode) {
    case "passed": if (!d || d.getTime() >= Date.now()) return false; break;
    case "upcoming": if (!d || d.getTime() < Date.now()) return false; break;
    case "empty": if (d) return false; break;
    case "not_empty": if (!d) return false; break;
    default: break;
  }

  if (d && (filter.from || filter.to)) {
    const iso = toISODate(d);
    if (filter.from && iso < filter.from) return false;
    if (filter.to && iso > filter.to) return false;
  } else if (!d && (filter.from || filter.to)) {
    return false;
  }

  if (filter.status && filter.status !== "any") {
    const statusValue = String(row[statusKey] ?? "").trim().toLowerCase();
    if (statusValue !== filter.status) return false;
  }
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
    if (f.mode !== "all" || f.from || f.to || (f.status && f.status !== "any")) count += 1;
  }
  if (state.f.src && state.f.src !== "All") count += 1;
  return count;
}
