// Central client state + a tiny pub/sub so feature modules can react to
// changes without a framework. Mirrors the shape in the design README's
// "State" section (prototype-only keys dropped).

const STORAGE_KEYS = {
  theme: "oshc.theme",
  density: "oshc.density",
  railCollapsed: "oshc.railCollapsed",
};

function loadPersisted() {
  return {
    theme: localStorage.getItem(STORAGE_KEYS.theme) || "light",
    density: localStorage.getItem(STORAGE_KEYS.density) || "Compact",
    railCollapsed: localStorage.getItem(STORAGE_KEYS.railCollapsed) === "1",
  };
}

const persisted = loadPersisted();

export const state = {
  theme: persisted.theme,
  density: persisted.density,
  screen: "editor",
  railCollapsed: persisted.railCollapsed,

  source: "data",
  draftExists: false,
  dirty: false,
  autoSave: true,
  publishedAt: "",
  // Data's revision as of this page's last load, and (in Draft) the
  // revision the open draft's merge base was captured from -- compared
  // against a fresh check in staleness.js so "Data changed" is surfaced
  // proactively instead of only being discovered at publish time.
  dataRevision: 0,
  draftBasedOnRevision: 0,

  headers: [],
  dataRows: [],
  draftRows: [],
  evidence: { by_os: {} },

  search: "",
  chip: "all",
  sort: { key: null, dir: "asc" },
  filterPanel: false,
  f: {
    os: { mode: "all", text: "" },
    detailed: { mode: "all", text: "" },
    norm: { mode: "all", text: "" },
    eol: { from: "", to: "" },
    eoas: { from: "", to: "" },
    // Multi-select: which "Matched by" categories to show -- [] means no
    // restriction (every category passes).
    src: [],
    // Draft-only, multi-select: which field(s) a row must have changed in,
    // compared to Data -- [] means no restriction by change type.
    changed: [],
    // How multiple `changed` selections combine: "or" (changed in any of
    // them), "and" (changed in all of them, possibly others too), or
    // "exact" (changed in exactly those fields, nothing else).
    changedMatch: "or",
  },

  selected: new Set(),
  drawer: null,

  modal: null,
  addTab: "single",
  progress: null,
  toast: null,

  aiOn: false,
  aiProvider: "openai",
  vendorSource: "eosl",
  settingsTab: "vendor",
  cloud: "azure",
  profile: {},
};

const listeners = new Set();

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function setState(patch) {
  Object.assign(state, patch);
  notify();
}

function notify() {
  for (const fn of listeners) fn(state);
}

export function rows() {
  return state.source === "draft" ? state.draftRows : state.dataRows;
}

export function isDraft() {
  return state.source === "draft";
}

export function isData() {
  return state.source === "data";
}

export function setTheme(theme) {
  localStorage.setItem(STORAGE_KEYS.theme, theme);
  document.documentElement.setAttribute("data-theme", theme);
  setState({ theme });
}

export function setDensity(density) {
  localStorage.setItem(STORAGE_KEYS.density, density);
  document.documentElement.setAttribute("data-density", density === "Comfortable" ? "comfortable" : "compact");
  setState({ density });
}

export function setRailCollapsed(collapsed) {
  localStorage.setItem(STORAGE_KEYS.railCollapsed, collapsed ? "1" : "0");
  setState({ railCollapsed: collapsed });
}

export function applyPersistedAppearance() {
  document.documentElement.setAttribute("data-theme", state.theme);
  document.documentElement.setAttribute("data-density", state.density === "Comfortable" ? "comfortable" : "compact");
}
