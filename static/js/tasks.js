// Background task registry. A task keeps running (consuming its SSE/async
// generator) independent of whatever UI is currently attached to it -- a
// modal can render its live progress, or nobody can be watching at all, and
// completion/cancellation still happens exactly the same way. This is what
// makes "Run in background" real: closing the modal only detaches the view,
// it never touches the underlying task.
//
// Finished tasks (complete/error/cancelled) also persist to localStorage as
// history, capped at MAX_HISTORY entries, so they survive a page reload --
// running tasks never persist, since a reload drops their live connection
// anyway. History is client-only (matches the app's existing theme/density
// persistence), pruned by count rather than age since usage here is light
// (a few sessions a week) and a count cap avoids date-math edge cases.
const HISTORY_KEY = "oshc.taskHistory";
const MAX_HISTORY = 40;

const tasks = [];
const listeners = new Set();

function notify() {
  for (const fn of listeners) fn(tasks);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function getTasks() {
  return tasks;
}

export function getTask(id) {
  return tasks.find((t) => t.id === id);
}

export function hasActive(kind) {
  return tasks.some((t) => t.kind === kind && t.status === "running");
}

export function activeCount() {
  return tasks.filter((t) => t.status === "running").length;
}

function newTaskId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed;
  } catch (_err) {
    return [];
  }
}

function saveHistory() {
  const finished = tasks
    .filter((t) => t.status !== "running")
    .sort((a, b) => (b.finishedAt || 0) - (a.finishedAt || 0))
    .slice(0, MAX_HISTORY)
    .map((t) => ({
      id: t.id, kind: t.kind, label: t.label, status: t.status, stage: t.stage,
      log: t.log.slice(-20), startedAt: t.startedAt, finishedAt: t.finishedAt, error: t.error,
    }));
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(finished));
  } catch (_err) { /* storage full/unavailable -- history just won't persist */ }
}

// Seed history from a previous session so the History tab isn't empty after
// a reload. These are plain finished records -- no onCancel, nothing to run.
for (const record of loadHistory()) {
  tasks.push({ ...record, pct: 100, processed: 0, total: 0, unread: false, jobId: null, onCancel: () => {} });
}

/**
 * Starts a task backed by an async generator of {type, stage, message,
 * processed, total, percent, job_id} events (the same shape the SSE
 * endpoints and the client-orchestrated Add-OS pipeline both emit).
 * Returns the task object immediately; it updates in place as events arrive.
 */
export function startTask({ kind, label, eventGenerator, onCancel, onComplete, onError, cancellable = true }) {
  const task = {
    id: newTaskId(),
    kind,
    label,
    status: "running",
    stage: "Starting…",
    pct: null,
    processed: 0,
    total: 0,
    log: [],
    jobId: null,
    startedAt: Date.now(),
    finishedAt: null,
    error: null,
    unread: false,
    cancellable,
    onCancel: () => onCancel?.(task.jobId),
  };
  tasks.unshift(task);
  notify();

  (async () => {
    try {
      for await (const event of eventGenerator) {
        if (event.type === "started") {
          task.jobId = event.job_id || task.jobId;
          task.log.push("Started.");
        } else if (event.type === "progress") {
          const total = event.total || 0;
          const processed = event.processed || 0;
          task.total = total;
          task.processed = processed;
          task.pct = event.percent != null ? event.percent : total ? Math.round((processed / total) * 100) : null;
          task.stage = event.stage || event.message || task.stage;
          if (event.message) task.log.push(event.message);
          else if (event.stage) task.log.push(`${event.stage} (${processed}/${total})`);
        } else if (event.type === "complete") {
          task.status = "complete";
          task.pct = 100;
          task.stage = "Done.";
          task.finishedAt = Date.now();
          task.unread = true;
          task.log.push(event.message || "Completed.");
          saveHistory();
          notify();
          onComplete?.(event);
          notify();
          return;
        } else if (event.type === "cancelled") {
          task.status = "cancelled";
          task.stage = "Cancelled.";
          task.finishedAt = Date.now();
          task.log.push("Cancelled.");
          saveHistory();
          notify();
          return;
        } else if (event.type === "conflict") {
          // Publish found rows changed both here and in Data since the
          // pre-check ran (or the pre-check was skipped/failed) -- rare
          // enough that surfacing it as a plain error is an acceptable
          // fallback; the common path resolves conflicts before this
          // ever starts a task (see openValidateModal).
          task.status = "error";
          task.stage = "Failed.";
          task.finishedAt = Date.now();
          task.unread = true;
          task.error = `${(event.conflicts || []).length} row(s) changed both here and in Data since you last checked. Re-open Validate & Publish to resolve them.`;
          task.log.push(task.error);
          saveHistory();
          notify();
          onError?.({ message: task.error, conflicts: event.conflicts });
          notify();
          return;
        } else if (event.type === "error") {
          task.status = "error";
          task.stage = "Failed.";
          task.finishedAt = Date.now();
          task.unread = true;
          task.error = event.message || "Something went wrong.";
          task.log.push(task.error);
          if (Array.isArray(event.output)) task.log.push(...event.output);
          saveHistory();
          notify();
          onError?.(event);
          notify();
          return;
        }
        notify();
      }
    } catch (error) {
      // A cancelled AbortController-backed stream (e.g. deploy upload)
      // surfaces here as a rejected fetch, not a "cancelled" SSE event --
      // treat it the same way so the UI doesn't report a cancel as a failure.
      if (error?.name === "AbortError") {
        task.status = "cancelled";
        task.stage = "Cancelled.";
        task.finishedAt = Date.now();
        task.log.push("Cancelled.");
        saveHistory();
        notify();
        return;
      }
      task.status = "error";
      task.stage = "Failed.";
      task.finishedAt = Date.now();
      task.unread = true;
      task.error = String(error?.message || error);
      task.log.push(task.error);
      saveHistory();
      notify();
      onError?.({ message: task.error });
      notify();
    }
  })();

  return task;
}

export function cancelTask(id) {
  const task = getTask(id);
  if (!task || task.status !== "running" || task.cancellable === false) return;
  task.onCancel?.();
}

export function dismissTask(id) {
  const index = tasks.findIndex((t) => t.id === id);
  if (index === -1) return;
  tasks.splice(index, 1);
  saveHistory();
  notify();
}

/** Removes every finished task from history (running tasks are untouched). */
export function clearHistory() {
  for (let i = tasks.length - 1; i >= 0; i -= 1) {
    if (tasks[i].status !== "running") tasks.splice(i, 1);
  }
  saveHistory();
  notify();
}

/** Resolves once the given task leaves "running" (complete/error/cancelled).
 * Used to sequence multiple tasks one at a time (e.g. "Update all" vendor
 * sources) without blocking on the old owns-the-loop runProgress model. */
export function waitForTask(id) {
  return new Promise((resolve) => {
    const current = getTask(id);
    if (!current || current.status !== "running") {
      resolve(current);
      return;
    }
    const unsub = subscribe(() => {
      const task = getTask(id);
      if (!task || task.status !== "running") {
        unsub();
        resolve(task);
      }
    });
  });
}

export function markTaskRead(id) {
  const task = getTask(id);
  if (task) task.unread = false;
}
