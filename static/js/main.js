import { state, setRailCollapsed, applyPersistedAppearance } from "./state.js";
import { iconMarkup } from "./icons.js";
import { initModals } from "./modals.js";
import { initEditor, syncRefreshEolSetting } from "./editor.js";
import { initVendor } from "./vendor.js";
import { initSettings } from "./settings.js";
import { initDeploy } from "./deploy.js";
import { initBackgroundTasks } from "./background.js";
import { subscribe as subscribeTasks, activeCount } from "./tasks.js";
import { subscribe as subscribeNotifications, hasUnread, markAllRead, getNotifications } from "./notifications.js";
import { initStaleness } from "./staleness.js";
import { ensureAuthenticated, logout } from "./auth.js";
import { api } from "./api.js";

applyPersistedAppearance();

// A failure here (Keycloak unreachable, misconfigured KEYCLOAK_ISSUER_URL,
// PKCE/state mismatch, ...) used to throw an unhandled rejection that
// silently stopped this whole module -- the page was left showing its
// static, server-rendered shell (stale "SA" avatar, "0" row count, no
// data) with no visible sign anything was wrong, only a console error.
// Show it on the page instead, since most people won't have devtools open.
function showFatalAuthError(err) {
  console.error("Authentication setup failed:", err);

  const box = document.createElement("div");
  box.style.cssText =
    "max-width:640px;margin:15vh auto;padding:24px 28px;font-family:system-ui,sans-serif;" +
    "border:1px solid #d33;border-radius:8px;background:#2a1414;color:#f5c2c2;";

  const heading = document.createElement("h2");
  heading.style.cssText = "margin-top:0;color:#ff6b6b;";
  heading.textContent = "Couldn't sign in";

  const message = document.createElement("p");
  message.textContent = (err && err.message) || "Unknown error"; // textContent, not innerHTML -- err.message is untrusted

  const hint = document.createElement("p");
  hint.style.color = "#e0a0a0";
  hint.textContent =
    "This usually means KEYCLOAK_ISSUER_URL (in .env) doesn't match a Keycloak that's actually " +
    "reachable from your browser, or the client/redirect URI isn't configured for this URL. " +
    "See KEYCLOAK_SETUP.md §6 for the specific gotchas this matches.";

  const retryBtn = document.createElement("button");
  retryBtn.textContent = "Retry";
  retryBtn.style.cssText =
    "padding:8px 16px;border-radius:6px;border:1px solid #f5c2c2;background:transparent;" +
    "color:#f5c2c2;cursor:pointer;";
  retryBtn.addEventListener("click", () => window.location.reload());

  box.append(heading, message, hint, retryBtn);
  document.body.replaceChildren(box);
}

try {
  // Gates the whole app behind a Keycloak login (AUTH_MULTITENANCY_PLAN.md
  // §8) -- redirects away and never resolves if there's no valid session
  // yet, so nothing below ever runs unauthenticated.
  await ensureAuthenticated();
} catch (err) {
  showFatalAuthError(err);
  throw err; // stop the rest of this module from running against no session
}

try {
  state.currentUser = await api.getMe();
} catch (_err) {
  // /api/auth/me itself requires the token ensureAuthenticated() just
  // guaranteed -- a failure here means something else is wrong (backend
  // unreachable, misconfigured KEYCLOAK_AUDIENCE, ...). Leave
  // state.currentUser null; editor.js's publisher-gate check already treats
  // that as "assume not a publisher" rather than crashing the whole app.
  console.error("Failed to load current user info:", _err);
}

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
  // A Refresh EOL/EOAS setting change made elsewhere in this session never
  // reaches the editor's cached flag on its own (initEditor() only runs
  // once) -- re-check every time this screen is actually opened.
  if (name === "editor") syncRefreshEolSetting();
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
const displayName = state.currentUser?.username || state.currentUser?.email || "";
avatar.textContent = displayName ? displayName.slice(0, 2).toUpperCase() : "?";
avatar.title = displayName ? `Signed in as ${displayName}` : "Account";
avatar.style.cursor = "pointer";

// Themed dropdown (same .action-menu pattern as editor.js's export-format
// picker) instead of a bare confirm() -- shows who's signed in and offers
// Log out as an explicit menu item.
let openUserMenuState = null;

function openUserMenu(triggerEl) {
  if (openUserMenuState) {
    openUserMenuState.close();
    return;
  }
  triggerEl.classList.add("is-open");

  const menu = document.createElement("div");
  menu.className = "action-menu";

  const info = document.createElement("div");
  info.style.cssText = "padding:6px 10px 8px;border-bottom:1px solid var(--c-border-subtle);";
  const nameEl = document.createElement("div");
  nameEl.style.cssText = "font:600 12px/16px var(--font-sans);color:var(--fg1);";
  nameEl.textContent = displayName || "Signed in";
  info.appendChild(nameEl);
  if (state.currentUser?.email && state.currentUser.email !== displayName) {
    const emailEl = document.createElement("div");
    emailEl.style.cssText = "font-size:11px;color:var(--fg3);margin-top:2px;";
    emailEl.textContent = state.currentUser.email;
    info.appendChild(emailEl);
  }
  menu.appendChild(info);

  const logoutBtn = document.createElement("button");
  logoutBtn.type = "button";
  logoutBtn.className = "action-menu-item";
  logoutBtn.textContent = "Log out";
  logoutBtn.addEventListener("click", () => {
    close();
    logout();
  });
  menu.appendChild(logoutBtn);

  document.body.appendChild(menu);

  const onDocClick = (event) => {
    if (!menu.contains(event.target) && event.target !== triggerEl) close();
  };
  const onKeydown = (event) => { if (event.key === "Escape") close(); };
  const onReflow = () => position();
  document.addEventListener("click", onDocClick, true);
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("resize", onReflow);
  window.addEventListener("scroll", onReflow, true);

  function close() {
    triggerEl.classList.remove("is-open");
    menu.remove();
    document.removeEventListener("click", onDocClick, true);
    document.removeEventListener("keydown", onKeydown);
    window.removeEventListener("resize", onReflow);
    window.removeEventListener("scroll", onReflow, true);
    if (openUserMenuState && openUserMenuState.triggerEl === triggerEl) openUserMenuState = null;
  }

  function position() {
    const rect = triggerEl.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    let left = rect.right - menuRect.width;
    if (left < 8) left = 8;
    let top = rect.bottom + 4;
    if (top + menuRect.height > window.innerHeight - 8) top = rect.top - menuRect.height - 4;
    menu.style.left = `${left}px`;
    menu.style.top = `${Math.max(8, top)}px`;
  }

  openUserMenuState = { triggerEl, close };
  position();
}

avatar.addEventListener("click", (event) => {
  event.stopPropagation();
  openUserMenu(avatar);
});

await initEditor();
initVendor();
initSettings();
initDeploy();
initStaleness();
