// Generic modal chrome + progress rendering, shared by every modal.
//
// Progress is backed by tasks.js: a task keeps running in the registry
// regardless of whether a modal is showing it. "Run in background" (and the
// X / Escape / backdrop close) just detaches the view -- it never cancels
// the task. Only the explicit Cancel button does that.

import { iconMarkup } from "./icons.js";
import { startTask, cancelTask, subscribe as subscribeTasks } from "./tasks.js";

const scrim = document.getElementById("modal-scrim");
let activeModalEl = null;
let activeDetach = null;

export function initModals() {
  // Delegated on the scrim (never itself replaced) rather than bound to each
  // [data-close-modal] button individually -- a finished task's progress
  // view overwrites the modal's body/footer HTML, which silently discards
  // any listener that was attached directly to a button inside it. A
  // listener on a stable ancestor keeps working no matter how many times
  // the buttons underneath get torn down and recreated.
  scrim.addEventListener("click", (event) => {
    if (event.target === scrim || event.target.closest("[data-close-modal]")) closeModal();
  });
  document.querySelectorAll(".modal-close").forEach((btn) => {
    btn.innerHTML = iconMarkup("x", { size: 14 });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && activeModalEl) closeModal();
  });
}

export function openModal(id) {
  // A finished task's progress view (bar + "Close" button) stays in the
  // modal's body/footer until something detaches it -- if the modal is
  // reopened directly (e.g. the toolbar button) instead of via its own
  // Close button, nothing had restored the original form, so the confirm/
  // cancel buttons the user expects were simply gone. Opening ANY modal
  // always clears whatever view was left attached first.
  activeDetach?.();
  activeDetach = null;
  document.querySelectorAll(".modal").forEach((el) => (el.hidden = el.id !== id));
  scrim.hidden = false;
  activeModalEl = document.getElementById(id);
}

export function closeModal() {
  activeDetach?.();
  activeDetach = null;
  scrim.hidden = true;
  if (activeModalEl) activeModalEl.hidden = true;
  activeModalEl = null;
}

let toastTimer = null;
export function showToast(message) {
  const toast = document.getElementById("toast");
  document.getElementById("toast-message").textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 2400);
}

/**
 * Starts a tracked background task (tasks.js) and attaches a live progress
 * view to it. Convenience wrapper around startTask + attachProgressView for
 * the common "open a modal, run one task, show its progress" case.
 */
export function runProgress({
  kind,
  label,
  bodyEl,
  footerEl,
  eventGenerator,
  onCancel,
  onComplete,
  onError,
  backgroundLabel,
}) {
  const task = startTask({ kind, label, eventGenerator, onCancel, onComplete, onError });
  attachProgressView(task, { bodyEl, footerEl, backgroundLabel });
  return task;
}

/**
 * Renders `task`'s live state (bar, stage, %, log) into bodyEl/footerEl and
 * keeps re-rendering as it changes. Restores bodyEl/footerEl's original
 * content on detach, so the modal's static confirm-form markup is intact
 * next time it's opened -- callers don't need to save/restore it themselves.
 */
export function attachProgressView(task, { bodyEl, footerEl, backgroundLabel = "Run in background" } = {}) {
  activeDetach?.();

  const originalBody = bodyEl.innerHTML;
  const originalFooter = footerEl.innerHTML;

  function renderBody() {
    const pct = task.pct;
    bodyEl.innerHTML = `
      <div class="progress-track"><div class="progress-fill" style="width:${pct != null ? Math.min(100, Math.max(0, pct)) : 0}%"></div></div>
      <div class="progress-meta"><span>${escapeHtml(task.stage)}</span><span class="pct tabular">${pct != null ? Math.round(pct) + "%" : ""}</span></div>
      <div class="progress-log">${task.log.map((line) => `<div>${escapeHtml(line)}</div>`).join("")}</div>
    `;
    const logEl = bodyEl.querySelector(".progress-log");
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  }

  function renderFooter() {
    if (task.status === "running") {
      footerEl.innerHTML = `
        <button class="btn" id="progress-cancel-btn" type="button">Cancel</button>
        <button class="btn" id="progress-bg-btn" type="button">${backgroundLabel}</button>`;
      footerEl.querySelector("#progress-cancel-btn").onclick = () => {
        cancelTask(task.id);
        closeModal();
      };
      footerEl.querySelector("#progress-bg-btn").onclick = () => closeModal();
    } else {
      footerEl.innerHTML = `<button class="btn primary" id="progress-close-btn" type="button">Close</button>`;
      footerEl.querySelector("#progress-close-btn").onclick = () => closeModal();
    }
  }

  function renderAll() {
    renderBody();
    renderFooter();
  }

  const unsub = subscribeTasks(renderAll);
  activeDetach = () => {
    unsub();
    bodyEl.innerHTML = originalBody;
    footerEl.innerHTML = originalFooter;
  };

  renderAll();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
