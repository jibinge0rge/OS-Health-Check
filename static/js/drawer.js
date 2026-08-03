// Row detail + evidence drawer.

import { state, isDraft } from "./state.js";
import { api } from "./api.js";
import { iconMarkup } from "./icons.js";
import { parseRowDate, toISODate } from "./date_utils.js";
import { initSingleDatePicker } from "./date_range_picker.js";

const DATE_FIELDS = new Set(["eol_date", "eoas_date"]);

// The CSV stores eol_date/eoas_date as unix epoch-second strings; show/edit
// them as calendar dates here and convert back on save.
function toDisplayValue(field, raw) {
  if (!DATE_FIELDS.has(field)) return raw ?? "";
  const d = parseRowDate(raw);
  return d ? toISODate(d) : "";
}

function toStoredValue(field, displayValue) {
  if (!DATE_FIELDS.has(field)) return displayValue;
  const text = String(displayValue ?? "").trim();
  if (!text) return "";
  const d = new Date(text);
  if (Number.isNaN(d.getTime())) return text;
  return String(Math.round(d.getTime() / 1000));
}

const panel = document.getElementById("drawer-panel");
const titleEl = document.getElementById("drawer-title");
const actionsEl = document.getElementById("drawer-actions");
const evidenceListEl = document.getElementById("drawer-evidence-list");

let currentRow = null;
let handlers = {};
// eol_date/eoas_date are custom calendar triggers (date_range_picker.js),
// not <input> elements -- they're wired up once here and updated via
// .setValue()/.setDisabled() instead of the generic text-input handling
// below, so they get the same theme-matched calendar as the column filters
// (a native <input type="date">'s popup can't be restyled to match dark
// mode, and can't offer an explicit "clear" affordance consistently).
const datePickers = {};

function setFieldValue(field, rawValue) {
  const display = toDisplayValue(field, rawValue);
  if (DATE_FIELDS.has(field)) {
    datePickers[field]?.setValue(display);
    return;
  }
  const input = panel.querySelector(`[data-drawer-field="${field}"]`);
  if (input) input.value = display;
}

export function initDrawer(cb) {
  handlers = cb || {};
  document.getElementById("drawer-close-btn").innerHTML = iconMarkup("x", { size: 14 });
  document.getElementById("drawer-close-btn").addEventListener("click", closeDrawer);

  panel.querySelectorAll("input[data-drawer-field]").forEach((input) => {
    const field = input.dataset.drawerField;
    input.addEventListener("change", () => {
      if (!currentRow || input.readOnly) return;
      const stored = toStoredValue(field, input.value);
      currentRow[field] = stored;
      setFieldValue(field, stored);
      handlers.onFieldChange?.(currentRow, field, stored);
    });
  });

  DATE_FIELDS.forEach((field) => {
    const trigger = panel.querySelector(`[data-drawer-field="${field}"]`);
    datePickers[field] = initSingleDatePicker(trigger, "", (iso) => {
      if (!currentRow) return;
      const stored = toStoredValue(field, iso);
      currentRow[field] = stored;
      handlers.onFieldChange?.(currentRow, field, stored);
    });
  });

  document.getElementById("drawer-same-as-os-btn").addEventListener("click", () => currentRow && handlers.onSameAsOs?.(currentRow));
  document.getElementById("drawer-ambiguous-btn").addEventListener("click", () => currentRow && handlers.onMarkAmbiguous?.(currentRow));
  document.getElementById("drawer-rerun-btn").addEventListener("click", () => currentRow && handlers.onRerun?.(currentRow));
  document.getElementById("drawer-revert-btn").addEventListener("click", () => currentRow && handlers.onRevert?.(currentRow));
  document.getElementById("drawer-toggle-reviewed-btn").addEventListener("click", () => currentRow && handlers.onToggleReviewed?.(currentRow));
}

function updateReviewedButton(row) {
  const btn = document.getElementById("drawer-toggle-reviewed-btn");
  // Only a changed (new/edited) row needs reviewing -- a row identical to
  // published Data has nothing to validate.
  const changed = handlers.isChanged?.(row) ?? true;
  btn.hidden = !changed;
  if (!changed) return;
  const reviewed = handlers.isReviewed?.(row) ?? false;
  btn.textContent = reviewed ? "Mark as not reviewed" : "Mark as reviewed";
  btn.classList.toggle("reviewed", reviewed);
}

/** Called after the reviewed flag changes elsewhere (table toggle, bulk
 * action) so the drawer's own button reflects it without a full re-open
 * (which would needlessly re-fetch evidence over the network). */
export function refreshDrawerReviewedState(row) {
  if (!isDrawerOpenFor(row)) return;
  updateReviewedButton(row);
}

export function isDrawerOpenFor(row) {
  return currentRow && currentRow.os_string === row.os_string;
}

export function closeDrawer() {
  currentRow = null;
  panel.hidden = true;
  handlers.onClose?.();
}

export async function openDrawer(row) {
  currentRow = row;
  panel.hidden = false;
  panel.dataset.editable = isDraft() ? "true" : "false";
  titleEl.textContent = row.os_string || "(blank)";
  actionsEl.hidden = !isDraft();
  if (isDraft()) updateReviewedButton(row);

  const editable = isDraft();
  panel.querySelectorAll("input[data-drawer-field]").forEach((input) => {
    const field = input.dataset.drawerField;
    setFieldValue(field, row[field]);
    input.readOnly = field === "matched_by" ? true : !editable;
  });
  DATE_FIELDS.forEach((field) => {
    setFieldValue(field, row[field]);
    datePickers[field]?.setDisabled(!editable);
  });

  evidenceListEl.innerHTML = `<p class="modal-note">Loading evidence…</p>`;
  try {
    const result = await api.getRowEvidence(row.os_string, state.source);
    if (!isDrawerOpenFor(row)) return;
    renderEvidenceDetail(row, result);
  } catch (error) {
    if (!isDrawerOpenFor(row)) return;
    evidenceListEl.innerHTML = `<p class="modal-note">Couldn't load evidence: ${escapeHtml(String(error.message || error))}</p>`;
  }
}

/** Shared by openDrawer's fetch and refreshDrawerEvidence's already-in-hand
 * result -- `detail` is {matched_by, entries} (build_evidence_entries's
 * shape), same as GET /api/lookup/evidence returns. */
function renderEvidenceDetail(row, detail) {
  const matchedInput = panel.querySelector('[data-drawer-field="matched_by"]');
  if (matchedInput) matchedInput.value = detail?.matched_by || row.matched_by || "No match";
  if (!detail?.entries || !detail.entries.length) {
    evidenceListEl.innerHTML = `<p class="modal-note">No evidence recorded for this row.</p>`;
    return;
  }
  evidenceListEl.innerHTML = detail.entries
    .map(
      (entry) => `
      <div class="evidence-entry">
        <span class="evidence-method-chip ${entry.method === "none" ? "none" : ""}">${escapeHtml(entry.method)}</span>
        <div class="evidence-field-name">${escapeHtml(entry.field)}</div>
        <div class="evidence-detail">${escapeHtml(entry.detail)}</div>
      </div>`
    )
    .join("");
}

/** Called after a fresh re-run/refresh already returned formatted evidence
 * detail, so the drawer's evidence list updates immediately instead of
 * showing whatever was loaded when it first opened -- no extra round trip,
 * and no dependency on the (possibly debounced/off) autosave having landed. */
export function refreshDrawerEvidence(row, detail) {
  if (!isDrawerOpenFor(row)) return;
  renderEvidenceDetail(row, detail);
}

/** Called right after a field is hand-edited (its evidence slot has already
 * been set to method "manual" -- see editor.js's onFieldChange) so the
 * drawer reflects that immediately instead of keep showing whatever the
 * field was last actually looked up as. Patches just that one entry rather
 * than re-fetching/re-rendering the whole list, since the summary text for
 * "manual" is a fixed string (matches lookup_extras._METHOD_SUMMARIES),
 * not something that needs the server's full evidence-formatting logic. */
export function markDrawerFieldManual(row, fieldLabel) {
  if (!isDrawerOpenFor(row)) return;
  const entryEl = [...evidenceListEl.querySelectorAll(".evidence-entry")].find(
    (el) => el.querySelector(".evidence-field-name")?.textContent === fieldLabel
  );
  if (!entryEl) return;
  const chip = entryEl.querySelector(".evidence-method-chip");
  const detail = entryEl.querySelector(".evidence-detail");
  if (chip) {
    chip.textContent = "manual";
    chip.classList.remove("none");
  }
  if (detail) detail.textContent = "Edited by hand in this session.";
}

export function refreshDrawerFields(row) {
  if (!isDrawerOpenFor(row)) return;
  panel.querySelectorAll("input[data-drawer-field]").forEach((input) => {
    const field = input.dataset.drawerField;
    if (field !== "matched_by") setFieldValue(field, row[field]);
  });
  DATE_FIELDS.forEach((field) => setFieldValue(field, row[field]));
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
