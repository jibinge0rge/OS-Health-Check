// Row detail + evidence drawer.

import { state, isDraft } from "./state.js";
import { api } from "./api.js";
import { iconMarkup } from "./icons.js";
import { parseRowDate, toISODate } from "./date_utils.js";

const DATE_FIELDS = new Set(["eol_date", "eoas_date"]);

// The CSV stores eol_date/eoas_date as unix epoch-second strings; show/edit
// them as calendar dates here and convert back on save.
function toDisplayValue(field, raw) {
  if (!DATE_FIELDS.has(field)) return raw ?? "";
  const d = parseRowDate(raw);
  return d ? toISODate(d) : (raw ?? "");
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

export function initDrawer(cb) {
  handlers = cb || {};
  document.getElementById("drawer-close-btn").innerHTML = iconMarkup("x", { size: 14 });
  document.getElementById("drawer-close-btn").addEventListener("click", closeDrawer);

  panel.querySelectorAll("[data-drawer-field]").forEach((input) => {
    input.addEventListener("change", () => {
      if (!currentRow || input.readOnly) return;
      const field = input.dataset.drawerField;
      const stored = toStoredValue(field, input.value);
      currentRow[field] = stored;
      input.value = toDisplayValue(field, stored);
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
  panel.querySelectorAll("[data-drawer-field]").forEach((input) => {
    const field = input.dataset.drawerField;
    input.value = toDisplayValue(field, row[field]);
    if (field === "matched_by") {
      input.readOnly = true;
    } else {
      input.readOnly = !editable;
    }
  });

  evidenceListEl.innerHTML = `<p class="modal-note">Loading evidence…</p>`;
  try {
    const result = await api.getRowEvidence(row.os_string, state.source);
    if (!isDrawerOpenFor(row)) return;
    const matchedInput = panel.querySelector('[data-drawer-field="matched_by"]');
    if (matchedInput) matchedInput.value = result.matched_by || row.matched_by || "No match";
    if (!result.entries || !result.entries.length) {
      evidenceListEl.innerHTML = `<p class="modal-note">No evidence recorded for this row.</p>`;
      return;
    }
    evidenceListEl.innerHTML = result.entries
      .map(
        (entry) => `
        <div class="evidence-entry">
          <span class="evidence-method-chip ${entry.method === "none" ? "none" : ""}">${escapeHtml(entry.method)}</span>
          <div class="evidence-field-name">${escapeHtml(entry.field)}</div>
          <div class="evidence-detail">${escapeHtml(entry.detail)}</div>
        </div>`
      )
      .join("");
  } catch (error) {
    if (!isDrawerOpenFor(row)) return;
    evidenceListEl.innerHTML = `<p class="modal-note">Couldn't load evidence: ${escapeHtml(String(error.message || error))}</p>`;
  }
}

export function refreshDrawerFields(row) {
  if (!isDrawerOpenFor(row)) return;
  panel.querySelectorAll("[data-drawer-field]").forEach((input) => {
    const field = input.dataset.drawerField;
    if (field !== "matched_by") input.value = toDisplayValue(field, row[field]);
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
