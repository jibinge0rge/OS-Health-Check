// Background tasks screen: Active tab (live, running tasks) and History tab
// (finished tasks, most recent first, persisted across reloads by tasks.js).

import { subscribe, getTasks, cancelTask, dismissTask, clearHistory } from "./tasks.js";
import { formatDateTime } from "./date_utils.js";

export function initBackgroundTasks() {
  document.querySelectorAll("[data-bg-tab]").forEach((btn) => {
    btn.addEventListener("click", () => selectTab(btn.dataset.bgTab));
  });
  document.getElementById("bg-clear-history-btn").addEventListener("click", () => {
    clearHistory();
  });

  // Delegated on the stable list containers (never themselves replaced),
  // not bound to each Cancel/Dismiss button directly -- renderActive/
  // renderHistory replace every row's innerHTML on every progress tick (a
  // fast-moving sync can tick several times a second), which would
  // otherwise tear down a listener bound straight to a button mid-click and
  // silently swallow that click, requiring another one to register.
  document.getElementById("bg-active-list").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-cancel]");
    if (btn) cancelTask(btn.dataset.cancel);
  });
  document.getElementById("bg-history-list").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-dismiss]");
    if (btn) dismissTask(btn.dataset.dismiss);
  });

  subscribe(render);
  render(getTasks());
}

function selectTab(tab) {
  document.querySelectorAll("[data-bg-tab]").forEach((btn) => btn.classList.toggle("active", btn.dataset.bgTab === tab));
  document.getElementById("bg-tab-active").hidden = tab !== "active";
  document.getElementById("bg-tab-history").hidden = tab !== "history";
}

function render(tasks) {
  const all = tasks || [];
  renderActive(all.filter((t) => t.status === "running"));
  renderHistory(
    all
      .filter((t) => t.status !== "running")
      .sort((a, b) => (b.finishedAt || 0) - (a.finishedAt || 0))
  );
}

function renderActive(running) {
  const list = document.getElementById("bg-active-list");
  const empty = document.getElementById("bg-active-empty");
  empty.hidden = running.length !== 0;

  list.innerHTML = running
    .map((task) => {
      const pct = task.pct != null ? Math.min(100, Math.max(0, task.pct)) : 0;
      const elapsed = Math.round((Date.now() - task.startedAt) / 1000);
      return `
      <div class="bg-task-row">
        <div class="bg-task-main">
          <span class="bg-task-status-dot running"></span>
          <div class="bg-task-info">
            <div class="bg-task-label">${escapeHtml(task.label)}</div>
            <div class="bg-task-stage">${escapeHtml(task.stage)}</div>
            <div class="bg-task-meta">${elapsed}s · running</div>
          </div>
        </div>
        <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
        <div class="bg-task-log">${task.log.slice(-6).map((l) => escapeHtml(l)).join("<br>")}</div>
        ${task.cancellable === false ? "" : `
        <div class="bg-task-actions">
          <button class="btn tertiary" data-cancel="${task.id}" type="button" ${task.cancelling ? "disabled" : ""}>${task.cancelling ? "Cancelling…" : "Cancel"}</button>
        </div>`}
      </div>`;
    })
    .join("");
}

const STATUS_LABEL = { complete: "Succeeded", error: "Failed", cancelled: "Cancelled" };

function renderHistory(finished) {
  const list = document.getElementById("bg-history-list");
  const empty = document.getElementById("bg-history-empty");
  empty.hidden = finished.length !== 0;

  list.innerHTML = finished
    .map((task) => {
      const reason = task.status === "error" ? (task.error || "Unknown error") : STATUS_LABEL[task.status] || task.status;
      return `
      <div class="bg-task-row bg-history-row">
        <div class="bg-task-main">
          <span class="bg-task-status-dot ${task.status}"></span>
          <div class="bg-task-info">
            <div class="bg-task-label">${escapeHtml(task.label)}</div>
            <div class="bg-task-stage">${escapeHtml(reason)}</div>
            <div class="bg-task-meta">${formatWhen(task.finishedAt)}</div>
          </div>
          <button class="btn tertiary" data-dismiss="${task.id}" type="button">Dismiss</button>
        </div>
      </div>`;
    })
    .join("");
}

function formatWhen(at) {
  if (!at) return "";
  const d = new Date(at);
  return formatDateTime(d);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
