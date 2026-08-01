import { state, setRailCollapsed, applyPersistedAppearance } from "./state.js";
import { iconMarkup } from "./icons.js";
import { initModals } from "./modals.js";
import { initEditor } from "./editor.js";
import { initVendor } from "./vendor.js";
import { initSettings } from "./settings.js";
import { initDeploy } from "./deploy.js";
import { initBackgroundTasks } from "./background.js";
import { subscribe as subscribeTasks, activeCount } from "./tasks.js";
import { subscribe as subscribeNotifications, hasUnread, markAllRead, getNotifications } from "./notifications.js";
import { initStaleness } from "./staleness.js";

applyPersistedAppearance();

const rail = document.getElementById("rail");
rail.classList.toggle("collapsed", state.railCollapsed);

const collapseBtn = document.getElementById("rail-collapse-btn");
collapseBtn.textContent = state.railCollapsed ? "›" : "‹";
collapseBtn.addEventListener("click", () => {
  const next = !rail.classList.contains("collapsed");
  rail.classList.toggle("collapsed", next);
  collapseBtn.textContent = next ? "›" : "‹";
  setRailCollapsed(next);
});

document.getElementById("rail-settings-icon").innerHTML = iconMarkup("settings", { size: 16 });
document.getElementById("rail-background-icon").innerHTML = iconMarkup("activity", { size: 16 });

const screens = {
  editor: document.getElementById("screen-editor"),
  vendor: document.getElementById("screen-vendor"),
  settings: document.getElementById("screen-settings"),
  deploy: document.getElementById("screen-deploy"),
  background: document.getElementById("screen-background"),
};

function showScreen(name) {
  Object.entries(screens).forEach(([key, el]) => { el.hidden = key !== name; });
  document.querySelectorAll(".rail-item[data-screen]").forEach((btn) => btn.classList.toggle("active", btn.dataset.screen === name));
  state.screen = name;
  if (name === "background") {
    markAllRead();
    document.querySelector("#notif-bell .dot").hidden = true;
  }
}

document.querySelectorAll(".rail-item[data-screen]").forEach((btn) => {
  btn.addEventListener("click", () => showScreen(btn.dataset.screen));
});

// Bell: opens a dropdown listing background-task completions/errors --
// stays on whatever screen you're on, no navigation.
const bell = document.getElementById("notif-bell");
bell.innerHTML = `${iconMarkup("bell", { size: 16 })}<span class="dot" hidden></span>`;
const notifDropdown = document.getElementById("notif-dropdown");
const notifList = document.getElementById("notif-dropdown-list");
const notifEmpty = document.getElementById("notif-dropdown-empty");

function renderNotifDropdown(notifications) {
  const items = notifications || getNotifications();
  notifEmpty.hidden = items.length !== 0;
  notifList.innerHTML = items
    .map((n) => `
      <div class="notif-item">
        <span class="notif-item-dot ${n.status === "error" ? "error" : ""}"></span>
        <div class="notif-item-body">
          <div class="notif-item-message">${escapeHtml(n.message)}</div>
          <div class="notif-item-time">${timeAgo(n.at)}</div>
        </div>
      </div>`)
    .join("");
}

function timeAgo(at) {
  const seconds = Math.round((Date.now() - at) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

bell.addEventListener("click", (event) => {
  event.stopPropagation();
  const opening = notifDropdown.hidden;
  notifDropdown.hidden = !opening;
  if (opening) {
    renderNotifDropdown();
    markAllRead();
  }
});
document.addEventListener("click", (event) => {
  if (!notifDropdown.hidden && !event.target.closest(".notif-bell-wrap")) notifDropdown.hidden = true;
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") notifDropdown.hidden = true;
});
subscribeNotifications((notifications) => {
  bell.querySelector(".dot").hidden = !hasUnread();
  if (!notifDropdown.hidden) renderNotifDropdown(notifications);
});

// Rail badge: how many background tasks are currently running.
const bgBadge = document.getElementById("rail-background-count");
subscribeTasks(() => {
  const n = activeCount();
  bgBadge.hidden = n === 0;
  bgBadge.textContent = String(n);
});

initModals();
initBackgroundTasks();
showScreen("editor");

const avatar = document.getElementById("topbar-avatar");
avatar.title = "Signed in";

await initEditor();
initVendor();
initSettings();
initDeploy();
initStaleness();
