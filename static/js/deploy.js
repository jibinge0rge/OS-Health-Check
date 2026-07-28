// Deploy screen: Azure Blob / AWS S3 / Google Cloud Storage, multi-profile.

import { api, streams } from "./api.js";
import { openModal, closeModal, runProgress, showToast } from "./modals.js";

const PROVIDERS = {
  azure: {
    label: "Azure Blob",
    fields: [
      { key: "account_name", label: "Storage account" },
      { key: "container_name", label: "Container" },
      { key: "blob_name", label: "Blob path" },
    ],
    authLabel: "Azure CLI (az login)",
    get: api.getAzureSettings, put: api.putAzureSettings, uploadStream: streams.azureUpload,
  },
  aws: {
    label: "AWS S3",
    fields: [
      { key: "bucket", label: "Bucket" },
      { key: "region", label: "Region" },
      { key: "key", label: "Key" },
    ],
    authLabel: "AWS CLI (aws configure)",
    get: api.getAwsSettings, put: api.putAwsSettings, uploadStream: streams.awsUpload,
  },
  gcp: {
    label: "Google Cloud Storage",
    fields: [
      { key: "bucket", label: "Bucket" },
      { key: "blob_name", label: "Blob path" },
    ],
    authLabel: "gcloud CLI (gcloud auth login)",
    get: api.getGcpSettings, put: api.putGcpSettings, uploadStream: streams.gcpUpload,
  },
};

let activeProvider = "azure";
let stores = { azure: { active_profile_id: "", profiles: [] }, aws: { active_profile_id: "", profiles: [] }, gcp: { active_profile_id: "", profiles: [] } };
let activeProfileId = "";

export async function initDeploy() {
  await Promise.all(
    Object.keys(PROVIDERS).map(async (id) => {
      stores[id] = await PROVIDERS[id].get().catch(() => ({ active_profile_id: "", profiles: [] }));
    })
  );
  renderProviderCards();
  selectProvider("azure");

  document.querySelectorAll(".provider-card").forEach((card) => {
    card.addEventListener("click", () => selectProvider(card.dataset.provider));
  });
  document.getElementById("new-profile-btn").addEventListener("click", createProfile);
  document.getElementById("profile-save-btn").addEventListener("click", saveActiveProfile);
  document.getElementById("profile-delete-btn").addEventListener("click", deleteActiveProfile);
  document.getElementById("profile-upload-btn").addEventListener("click", uploadActiveProfile);
}

function renderProviderCards() {
  document.getElementById("provider-count-azure").textContent = countLabel(stores.azure);
  document.getElementById("provider-count-aws").textContent = countLabel(stores.aws);
  document.getElementById("provider-count-gcp").textContent = countLabel(stores.gcp);
  document.querySelectorAll(".provider-card").forEach((card) => card.classList.toggle("active", card.dataset.provider === activeProvider));
}

function countLabel(store) {
  const n = store.profiles.length;
  return n === 0 ? "No profiles yet" : `${n} profile${n === 1 ? "" : "s"}`;
}

function selectProvider(providerId) {
  activeProvider = providerId;
  const store = stores[providerId];
  activeProfileId = store.active_profile_id || store.profiles[0]?.id || "";
  renderProviderCards();
  renderProfileUI();
}

function renderProfileUI() {
  const provider = PROVIDERS[activeProvider];
  const store = stores[activeProvider];
  document.getElementById("profile-pills").innerHTML = store.profiles
    .map((p) => `<button type="button" class="profile-pill ${p.id === activeProfileId ? "active" : ""}" data-profile="${p.id}">${escapeHtml(p.name)}</button>`)
    .join("");
  document.getElementById("profile-pills").querySelectorAll("[data-profile]").forEach((btn) => {
    btn.addEventListener("click", () => { activeProfileId = btn.dataset.profile; renderProfileUI(); });
  });

  const profile = store.profiles.find((p) => p.id === activeProfileId);
  document.getElementById("profile-active-name").textContent = profile ? profile.name : "No profile selected";
  document.getElementById("profile-upload-btn").textContent = `Upload to ${provider.label}`;

  const fieldsWrap = document.getElementById("profile-fields");
  fieldsWrap.innerHTML = provider.fields
    .map((f) => `<div class="profile-field"><label>${f.label}</label><input type="text" data-field="${f.key}" value="${escapeHtml(profile?.[f.key] || "")}" /></div>`)
    .join("") + `<div class="profile-field"><label>Auth</label><input type="text" value="${provider.authLabel}" disabled /></div>`;

  const hasProfile = Boolean(profile);
  document.getElementById("profile-upload-btn").disabled = !hasProfile;
  const deleteBtn = document.getElementById("profile-delete-btn");
  deleteBtn.disabled = !hasProfile || store.profiles.length <= 1;
  deleteBtn.title = hasProfile && store.profiles.length <= 1 ? "At least one profile must remain — edit or overwrite it instead." : "";
}

function createProfile() {
  const store = stores[activeProvider];
  const name = prompt("Profile name (e.g. prod-eu)");
  if (!name || !name.trim()) return;
  const id = `new-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const blank = { id, name: name.trim() };
  PROVIDERS[activeProvider].fields.forEach((f) => { blank[f.key] = ""; });
  store.profiles.push(blank);
  activeProfileId = id;
  renderProviderCards();
  renderProfileUI();
}

async function saveActiveProfile() {
  const store = stores[activeProvider];
  const profile = store.profiles.find((p) => p.id === activeProfileId);
  if (!profile) return;
  document.querySelectorAll("#profile-fields [data-field]").forEach((input) => {
    profile[input.dataset.field] = input.value.trim();
  });
  store.active_profile_id = activeProfileId;
  try {
    stores[activeProvider] = await PROVIDERS[activeProvider].put(store);
    showToast("Profile saved.");
  } catch (error) {
    showToast(`Save failed: ${error.message}`);
    return;
  }
  renderProviderCards();
  renderProfileUI();
}

async function deleteActiveProfile() {
  const store = stores[activeProvider];
  store.profiles = store.profiles.filter((p) => p.id !== activeProfileId);
  if (store.active_profile_id === activeProfileId) store.active_profile_id = store.profiles[0]?.id || "";
  try {
    stores[activeProvider] = store.profiles.length ? await PROVIDERS[activeProvider].put(store) : store;
    if (!store.profiles.length) await PROVIDERS[activeProvider].put({ active_profile_id: "", profiles: [] }).catch(() => {});
  } catch (error) {
    showToast(`Delete failed: ${error.message}`);
  }
  activeProfileId = stores[activeProvider].active_profile_id || "";
  renderProviderCards();
  renderProfileUI();
}

function uploadActiveProfile() {
  const provider = PROVIDERS[activeProvider];
  document.getElementById("deploy-upload-eyebrow").textContent = provider.label.toUpperCase();
  document.getElementById("modal-deploy-upload-body").innerHTML = `<p class="modal-note">Uploading the validated Data file to ${provider.label}…</p>`;
  document.getElementById("modal-deploy-upload-footer").innerHTML = "";
  openModal("modal-deploy-upload");

  const bodyEl = document.getElementById("modal-deploy-upload-body");
  const footerEl = document.getElementById("modal-deploy-upload-footer");
  runProgress({
    bodyEl, footerEl,
    eventGenerator: provider.uploadStream(activeProfileId),
    onCancel: () => {},
    onComplete: () => showToast(`Uploaded to ${provider.label}.`),
    onError: (event) => showToast(`Upload failed: ${event.message || "unknown error"}`),
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
