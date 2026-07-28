// Watches the background-task registry and turns completions/errors into
// notifications: a toast that shows and auto-dismisses, plus an entry the
// topbar bell's unread dot reflects until the user opens Background tasks.

import { subscribe as subscribeTasks, getTasks } from "./tasks.js";
import { showToast } from "./modals.js";

const notifications = [];
const listeners = new Set();
// Seed with whatever's already finished (rehydrated task history from a
// previous session) so reload doesn't re-toast/re-notify old completions.
const notifiedTaskIds = new Set(
  getTasks()
    .filter((t) => t.status === "complete" || t.status === "error")
    .map((t) => t.id)
);

function notify() {
  for (const fn of listeners) fn(notifications);
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function getNotifications() {
  return notifications;
}

export function hasUnread() {
  return notifications.some((n) => !n.read);
}

export function markAllRead() {
  notifications.forEach((n) => { n.read = true; });
  notify();
}

subscribeTasks((tasks) => {
  for (const task of tasks) {
    if ((task.status === "complete" || task.status === "error") && !notifiedTaskIds.has(task.id)) {
      notifiedTaskIds.add(task.id);
      const message =
        task.status === "complete"
          ? `${task.label} completed.`
          : `${task.label} failed: ${task.error || "unknown error"}`;
      notifications.unshift({
        id: task.id,
        taskId: task.id,
        label: task.label,
        status: task.status,
        message,
        at: Date.now(),
        read: false,
      });
      notify();
      showToast(message);
    }
  }
});
