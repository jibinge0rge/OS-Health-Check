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

/**
 * Replaces window.prompt() with an in-app modal matching the rest of the
 * UI's chrome. Resolves with the trimmed input value, or null if the user
 * cancels via the Cancel button, X, Escape, or a backdrop click.
 */
export function promptModal({ eyebrow = "", title = "", label = "", placeholder = "", confirmLabel = "OK", initialValue = "" } = {}) {
  return new Promise((resolve) => {
    document.getElementById("prompt-eyebrow").textContent = eyebrow;
    document.getElementById("prompt-title").textContent = title;
    document.getElementById("prompt-label").textContent = label;
    const input = document.getElementById("prompt-input");
    input.placeholder = placeholder;
    input.value = initialValue;
    const confirmBtn = document.getElementById("prompt-confirm-btn");
    confirmBtn.textContent = confirmLabel;

    let settled = false;
    const cleanup = () => {
      confirmBtn.removeEventListener("click", onConfirm);
      input.removeEventListener("keydown", onKeydown);
    };
    const finish = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const onConfirm = () => {
      const value = input.value.trim();
      if (!value) {
        input.focus();
        return;
      }
      finish(value);
      closeModal();
    };
    const onKeydown = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        onConfirm();
      }
    };
    confirmBtn.addEventListener("click", onConfirm);
    input.addEventListener("keydown", onKeydown);

    openModal("modal-prompt");
    // Cancel / X / Escape / backdrop all route through closeModal(), which
    // runs activeDetach -- reuse that hook so every dismissal path resolves.
    activeDetach = () => finish(null);
    setTimeout(() => input.focus(), 50);
  });
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
  cancellable,
}) {
  const task = startTask({ kind, label, eventGenerator, onCancel, onComplete, onError, cancellable });
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
  let autoCloseTimer = null;

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
      const cancelBtnHtml = task.cancellable === false ? "" : `<button class="btn" id="progress-cancel-btn" type="button">Cancel</button>`;
      footerEl.innerHTML = `
        ${cancelBtnHtml}
        <button class="btn" id="progress-bg-btn" type="button">${backgroundLabel}</button>`;
      if (task.cancellable !== false) {
        footerEl.querySelector("#progress-cancel-btn").onclick = () => {
          cancelTask(task.id);
          closeModal();
        };
      }
      footerEl.querySelector("#progress-bg-btn").onclick = () => closeModal();
    } else {
      footerEl.innerHTML = `<button class="btn primary" id="progress-close-btn" type="button">Close</button>`;
      footerEl.querySelector("#progress-close-btn").onclick = () => closeModal();
      // Successful runs close themselves once the final state is visible for
      // a moment -- error/cancelled stay open since those need reading.
      if (task.status === "complete" && autoCloseTimer === null) {
        autoCloseTimer = setTimeout(() => closeModal(), 900);
      }
    }
  }

  function renderAll() {
    renderBody();
    renderFooter();
  }

  const unsub = subscribeTasks(renderAll);
  activeDetach = () => {
    unsub();
    clearTimeout(autoCloseTimer);
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
