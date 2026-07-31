// Vendor lookups screen: read-only browser over the local vendor lifecycle
// databases. Never say "cache" in any UI copy here (design rule).

import { api, streams } from "./api.js";
import { iconMarkup } from "./icons.js";
import { openModal, runProgress, showToast } from "./modals.js";
import { getTasks, waitForTask } from "./tasks.js";

const PAGE_SIZE_OPTIONS = [50, 100, 250, 500, 1000];

let sources = [];
let activeSourceId = "eosl";
let allRows = [];
let page = 1;
let pageSizeIndex = 1;
let searchQuery = "";

export async function initVendor() {
  sources = await api.vendorSources().then((r) => r.sources).catch(() => []);
  renderSourcePicker();
  await selectSource(activeSourceId);

  document.getElementById("vendor-search-icon").innerHTML = iconMarkup("search", { size: 14 });
  document.getElementById("vendor-search-input").addEventListener("input", (event) => {
    searchQuery = event.target.value;
    page = 1;
    renderPage();
  });

  document.getElementById("vendor-source-select").addEventListener("change", (event) => selectSource(event.target.value));
  document.getElementById("vendor-update-all-btn").addEventListener("click", () => openUpdateModal(null));
  document.getElementById("vendor-update-source-btn").addEventListener("click", () => openUpdateModal(activeSourceId));

  document.getElementById("vendor-footer-prev-btn").addEventListener("click", () => { if (page > 1) { page -= 1; renderPage(); } });
  document.getElementById("vendor-footer-next-btn").addEventListener("click", () => { page += 1; renderPage(); });
  document.getElementById("vendor-rows-per-page-chip").addEventListener("click", () => {
    pageSizeIndex = (pageSizeIndex + 1) % PAGE_SIZE_OPTIONS.length;
    document.getElementById("vendor-rows-per-page-chip").textContent = String(PAGE_SIZE_OPTIONS[pageSizeIndex]);
    page = 1;
    renderPage();
  });
}

function renderSourcePicker() {
  const select = document.getElementById("vendor-source-select");
  select.innerHTML = sources
    .map((s) => `<option value="${s.id}">${escapeHtml(s.label)}${s.enabled ? "" : " (disabled)"}</option>`)
    .join("");
  updateSourceStatus();
}

function updateSourceStatus() {
  const select = document.getElementById("vendor-source-select");
  select.value = activeSourceId;
  const index = sources.findIndex((s) => s.id === activeSourceId);
  const source = sources[index];
  const statusEl = document.getElementById("vendor-source-status");
  statusEl.querySelector(".status-dot").classList.toggle("enabled", Boolean(source?.enabled));
  document.getElementById("vendor-source-rank").textContent = index >= 0 ? `#${index + 1} in refresh order` : "";
}

async function selectSource(sourceId) {
  activeSourceId = sourceId;
  updateSourceStatus();
  document.getElementById("vendor-update-source-label").textContent =
    sources.find((s) => s.id === sourceId)?.label || sourceId;

  searchQuery = "";
  const searchInput = document.getElementById("vendor-search-input");
  if (searchInput) searchInput.value = "";

  const body = document.getElementById("vendor-table-body");
  let rows = [];
  let status = {};
  try {
    ({ rows, status } = await api.vendorRows(sourceId));
  } catch (error) {
    document.getElementById("vendor-meta").textContent = "Local vendor lifecycle database unavailable.";
    body.innerHTML = `<div class="vendor-row"><span style="grid-column:1/-1;color:var(--fg3)">${escapeHtml(error.message)}</span></div>`;
    allRows = [];
    document.getElementById("vendor-footer").hidden = true;
    return;
  }
  document.getElementById("vendor-footer").hidden = false;
  const productCount = status.product_count ?? status.total_products ?? 0;
  const releaseCount = status.release_count ?? rows.length;
  const lastUpdated = status.last_updated ? new Date(status.last_updated).toLocaleString() : "never";
  document.getElementById("vendor-meta").textContent = `Last updated ${lastUpdated} · ${productCount} products · ${releaseCount} releases`;

  allRows = rows;
  page = 1;
  renderPage();
}

function matchesSearch(row) {
  const q = searchQuery.trim().toLowerCase();
  if (!q) return true;
  return [row.product, row.release].some((v) => String(v || "").toLowerCase().includes(q));
}

function renderPage() {
  const body = document.getElementById("vendor-table-body");
  const filteredRows = allRows.filter(matchesSearch);
  const size = PAGE_SIZE_OPTIONS[pageSizeIndex];
  const totalPages = Math.max(1, Math.ceil(filteredRows.length / size));
  if (page > totalPages) page = totalPages;
  const start = (page - 1) * size;
  const pageRows = filteredRows.slice(start, start + size);

  body.innerHTML = pageRows
    .map(
      (row) => `
      <div class="vendor-row">
        <span>${escapeHtml(row.product)}</span>
        <span>${escapeHtml(row.release)}</span>
        <span class="tabular">${escapeHtml(row.released)}</span>
        <span class="tabular">${escapeHtml(row.eol_date)}</span>
        <span class="tabular">${escapeHtml(row.eoas_date)}</span>
        <span class="vendor-supported-chip ${row.supported ? "yes" : "no"}">${row.supported ? "Yes" : "No"}</span>
      </div>`
    )
    .join("");

  const shownCount = pageRows.length ? `${start + 1}–${start + pageRows.length}` : "0";
  document.getElementById("vendor-footer-shown").textContent = `${shownCount} of ${filteredRows.length} shown`;
  document.getElementById("vendor-footer-page").textContent = `Page ${page} of ${totalPages}`;
  document.getElementById("vendor-footer-prev-btn").disabled = page <= 1;
  document.getElementById("vendor-footer-next-btn").disabled = page >= totalPages;
}

function anyVendorSyncActive() {
  return getTasks().some((t) => t.kind.startsWith("vendor-sync:") && t.status === "running");
}

function openUpdateModal(sourceId) {
  const isAll = sourceId == null;
  if (isAll ? anyVendorSyncActive() : getTasks().some((t) => t.kind === `vendor-sync:${sourceId}` && t.status === "running")) {
    showToast("A vendor lookup update is already running — see Background tasks.");
    return;
  }
  document.getElementById("vendor-update-eyebrow").textContent = isAll ? "VENDOR LOOKUPS" : sources.find((s) => s.id === sourceId)?.label || sourceId;
  document.getElementById("vendor-update-title").textContent = isAll
    ? "Update all vendor lifecycle databases?"
    : `Update the ${sources.find((s) => s.id === sourceId)?.label || sourceId} local vendor lifecycle database?`;
  document.getElementById("modal-vendor-update-body").innerHTML =
    `<p class="modal-note">This refreshes the local vendor lifecycle database only. It does not write dates into the lookup — run Refresh EOL/EOAS afterward to pull new data into rows.</p>`;
  openModal("modal-vendor-update");

  const bodyEl = document.getElementById("modal-vendor-update-body");
  const footerEl = document.getElementById("modal-vendor-update-footer");
  document.getElementById("vendor-update-confirm-btn").onclick = async () => {
    const targets = isAll ? sources.map((s) => s.id) : [sourceId];
    let batchCancelled = false;
    for (const id of targets) {
      if (batchCancelled) break;
      const label = `Update ${sources.find((s) => s.id === id)?.label || id}`;
      const task = runProgress({
        kind: `vendor-sync:${id}`,
        label,
        bodyEl, footerEl,
        eventGenerator: streams.vendorSync(id, {}),
        // Cancel is a user action on the whole batch, not just this source --
        // stop the loop instead of silently starting the next update. (The
        // modal itself closes as soon as Cancel is clicked -- see
        // attachProgressView -- so without this, every remaining source
        // would run invisibly and need cancelling one by one from
        // Background tasks.) Deliberately no inline .catch() here -- tasks.js's
        // cancelTask() awaits this to know whether the cancel actually
        // landed, re-enabling the Cancel button if it rejects.
        onCancel: (jobId) => {
          batchCancelled = true;
          return jobId ? api.vendorSyncCancel(jobId) : undefined;
        },
        onComplete: () => showToast(`${sources.find((s) => s.id === id)?.label || id} updated.`),
        onError: (event) => showToast(`Update failed: ${event.message || "unknown error"}`),
      });
      const finished = await waitForTask(task.id);
      if (finished?.status === "cancelled") batchCancelled = true;
    }
    if (batchCancelled && isAll) showToast("Update all cancelled.");
    await selectSource(activeSourceId);
  };
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
