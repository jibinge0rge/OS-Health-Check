// Settings screen: Vendor lookups / Configure AI / Appearance tabs.

import { state, setTheme, setDensity } from "./state.js";
import { api } from "./api.js";
import { promptModal, showToast } from "./modals.js";

// Bound once at module load (not per-render) so repeated renderAiTab() calls
// never stack duplicate document listeners -- these look up the dropdown by
// id each time they fire, so they stay correct across re-renders.
document.addEventListener("click", (event) => {
  const menu = document.getElementById("ai-model-menu");
  if (menu && !menu.hidden && !event.target.closest("#ai-model-dropdown")) menu.hidden = true;
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const menu = document.getElementById("ai-model-menu");
  if (menu && !menu.hidden) menu.hidden = true;
});

export async function initSettings() {
  document.querySelectorAll("[data-settings-tab]").forEach((btn) => {
    btn.addEventListener("click", () => selectTab(btn.dataset.settingsTab));
  });

  await Promise.all([renderVendorTab(), renderAiTab()]);
  renderAppearanceTab();
}

function selectTab(tab) {
  document.querySelectorAll("[data-settings-tab]").forEach((btn) => btn.classList.toggle("active", btn.dataset.settingsTab === tab));
  document.getElementById("settings-tab-vendor").hidden = tab !== "vendor";
  document.getElementById("settings-tab-prompt").hidden = tab !== "prompt";
  document.getElementById("settings-tab-appearance").hidden = tab !== "appearance";
}

function renderRefreshOrderChips(sources) {
  const strip = document.getElementById("settings-refresh-order-chips");
  if (!strip) return;
  strip.innerHTML = sources
    .filter((s) => s.enabled)
    .map((s) => `<span class="chip-arrow">&#8594; ${escapeHtml(s.id)}</span>`)
    .join("");
}

async function renderVendorTab() {
  const [{ sources }, settingsPayload, appSettings] = await Promise.all([
    api.vendorSources(),
    api.vendorSettingsGet().catch(() => ({ sources: {} })),
    api.getSettings(),
  ]);
  renderRefreshOrderChips(sources);

  const refreshToggle = document.getElementById("refresh-eol-enabled-toggle");
  refreshToggle.checked = appSettings.refresh_eol_enabled;
  refreshToggle.onchange = async () => {
    // ai_enabled/ai_provider aren't Optional in the update request (a bool
    // field can't mean "omit to leave unchanged"), so they must be echoed
    // here too -- otherwise saving this toggle would silently reset AI
    // match back to its defaults, the same reason the AI tab's own saves
    // echo refresh_eol_enabled back (see renderAiTab).
    await api.putSettings({
      refresh_eol_enabled: refreshToggle.checked,
      ai_enabled: appSettings.ai_enabled,
      ai_provider: appSettings.ai_provider,
    });
    showToast(`Refresh EOL/EOAS ${refreshToggle.checked ? "enabled" : "disabled"}.`);
  };

  const wrap = document.getElementById("settings-vendor-cards");
  wrap.innerHTML = sources
    .map(
      (s) => `
      <div class="settings-card" data-source-card="${s.id}">
        <div class="settings-card-row">
          <div>
            <div class="settings-card-title">${escapeHtml(s.label)}</div>
            <p class="settings-card-note">${s.uses_keywords ? "Runs only when a family keyword matches" : "No keyword gate — runs whenever enabled"}</p>
          </div>
          <label class="toggle">
            <input type="checkbox" data-vendor-toggle ${s.enabled ? "checked" : ""} />
            <span class="toggle-track"><span class="toggle-knob"></span></span>
          </label>
        </div>
        ${s.uses_keywords ? `
        <div class="keyword-row" data-keyword-row>
          <span class="settings-card-note" style="width:100%">FAMILY KEYWORDS</span>
          ${(s.keywords || []).map((kw) => `<span class="keyword-chip" data-keyword="${escapeHtml(kw)}">${escapeHtml(kw)} <button type="button" data-remove-keyword>&times;</button></span>`).join("")}
          <button type="button" class="add-keyword-btn" data-add-keyword>+ Add keyword</button>
        </div>` : ""}
      </div>`
    )
    .join("");

  wrap.querySelectorAll("[data-source-card]").forEach((card) => {
    const sourceId = card.dataset.sourceCard;
    card.querySelector("[data-vendor-toggle]").addEventListener("change", async (event) => {
      await api.vendorPreferences(sourceId, { enabled: event.target.checked });
      showToast(`${sourceId} ${event.target.checked ? "enabled" : "disabled"}.`);
      const { sources: updated } = await api.vendorSources();
      renderRefreshOrderChips(updated);
    });
    card.querySelectorAll("[data-remove-keyword]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const chip = btn.closest("[data-keyword]");
        const keywords = [...card.querySelectorAll("[data-keyword]")].map((c) => c.dataset.keyword).filter((k) => k !== chip.dataset.keyword);
        await api.vendorPreferences(sourceId, { keywords });
        chip.remove();
      });
    });
    const addBtn = card.querySelector("[data-add-keyword]");
    addBtn?.addEventListener("click", async () => {
      const value = await promptModal({
        eyebrow: "VENDOR LOOKUPS",
        title: "Add family keyword",
        label: "Keyword",
        placeholder: "e.g. cisco",
        confirmLabel: "Add",
      });
      if (!value) return;
      const keywords = [...card.querySelectorAll("[data-keyword]")].map((c) => c.dataset.keyword);
      keywords.push(value);
      await api.vendorPreferences(sourceId, { keywords });
      renderVendorTab();
    });
  });
}

async function renderAiTab() {
  const settings = await api.getSettings();
  const toggle = document.getElementById("ai-enabled-toggle");
  toggle.checked = settings.ai_enabled;
  toggle.onchange = async () => {
    await api.putSettings({ refresh_eol_enabled: settings.refresh_eol_enabled, ai_enabled: toggle.checked, ai_provider: settings.ai_provider, ai_match_prompt: null });
    showToast(`AI match ${toggle.checked ? "enabled" : "disabled"}.`);
  };

  const providerMeta = {
    openai: { label: "OpenAI", available: settings.openai_available },
    gemini: { label: "Gemini", available: settings.gemini_available },
    openrouter: { label: "OpenRouter", available: settings.openrouter_available },
  };
  const providerIds = Object.keys(providerMeta);
  document.getElementById("ai-provider-chips").innerHTML = providerIds
    .map((id) => {
      const p = providerMeta[id];
      const model = settings.ai_models?.[id] || settings.default_ai_models?.[id] || "";
      return `<span class="provider-chip ${p.available ? "" : "unavailable"} ${id === settings.ai_provider ? "active" : ""}" data-provider="${id}"><strong>${p.label}</strong><span>${escapeHtml(model)}</span></span>`;
    })
    .join("");
  document.getElementById("ai-provider-chips").querySelectorAll("[data-provider]").forEach((chip) => {
    chip.addEventListener("click", async () => {
      await api.putSettings({ refresh_eol_enabled: settings.refresh_eol_enabled, ai_enabled: toggle.checked, ai_provider: chip.dataset.provider, ai_match_prompt: null });
      renderAiTab();
    });
  });

  const activeProvider = settings.ai_provider;
  const activeModel = settings.ai_models?.[activeProvider] || settings.default_ai_models?.[activeProvider] || "";
  const catalog = settings.ai_model_choices?.[activeProvider] || [];

  const saveModel = async (value) => {
    const model = value.trim() || settings.default_ai_models?.[activeProvider] || "";
    await api.putSettings({
      refresh_eol_enabled: settings.refresh_eol_enabled,
      ai_enabled: toggle.checked,
      ai_provider: activeProvider,
      ai_match_prompt: null,
      ai_models: { [activeProvider]: model },
    });
    showToast(`${providerMeta[activeProvider].label} model set to ${model}.`);
    renderAiTab();
  };

  const menuEl = document.getElementById("ai-model-menu");
  document.getElementById("ai-model-trigger-label").textContent = activeModel;
  menuEl.innerHTML = catalog
    .map((m) => `<button type="button" class="model-option ${m === activeModel ? "active" : ""}" data-model="${escapeHtml(m)}">${escapeHtml(m)}</button>`)
    .join("") + `<button type="button" class="model-option add-custom" id="ai-model-add-custom">+ Add custom model…</button>`;
  menuEl.querySelectorAll("[data-model]").forEach((btn) => {
    btn.addEventListener("click", () => {
      menuEl.hidden = true;
      if (btn.dataset.model !== activeModel) saveModel(btn.dataset.model);
    });
  });
  document.getElementById("ai-model-add-custom").addEventListener("click", async () => {
    menuEl.hidden = true;
    const value = await promptModal({
      eyebrow: "CONFIGURE AI",
      title: "Add custom model",
      label: `${providerMeta[activeProvider].label} model id`,
      placeholder: "e.g. gpt-4.1",
      confirmLabel: "Add",
    });
    if (!value) return;
    saveModel(value);
  });
  document.getElementById("ai-model-trigger").onclick = () => { menuEl.hidden = !menuEl.hidden; };
  document.getElementById("ai-model-reset-btn").onclick = () => saveModel(settings.default_ai_models?.[activeProvider] || "");

  const confidenceSlider = document.getElementById("ai-confidence-slider");
  const confidenceValue = document.getElementById("ai-confidence-value");
  const initialConfidence = settings.ai_confidence_threshold ?? 85;
  confidenceSlider.value = initialConfidence;
  confidenceValue.textContent = `${initialConfidence}%`;
  // Live-update the label while dragging; only save once the user releases
  // the thumb so scrubbing across the range doesn't fire a PUT per pixel.
  confidenceSlider.oninput = () => {
    confidenceValue.textContent = `${confidenceSlider.value}%`;
  };
  confidenceSlider.onchange = async () => {
    const clamped = Math.max(50, Math.min(100, Number(confidenceSlider.value) || 85));
    confidenceSlider.value = clamped;
    confidenceValue.textContent = `${clamped}%`;
    await api.putSettings({
      refresh_eol_enabled: settings.refresh_eol_enabled,
      ai_enabled: toggle.checked,
      ai_provider: settings.ai_provider,
      ai_match_prompt: null,
      ai_confidence_threshold: clamped,
    });
    showToast(`Confidence cutoff set to ${clamped}%.`);
  };

  const promptArea = document.getElementById("ai-prompt-textarea");
  promptArea.value = settings.ai_match_prompt || settings.default_ai_match_prompt;
  document.getElementById("ai-prompt-save-btn").onclick = async () => {
    await api.putSettings({ refresh_eol_enabled: settings.refresh_eol_enabled, ai_enabled: toggle.checked, ai_provider: settings.ai_provider, ai_match_prompt: promptArea.value });
    showToast("AI match prompt saved.");
  };
  document.getElementById("ai-prompt-reset-btn").onclick = async () => {
    promptArea.value = settings.default_ai_match_prompt;
    await api.putSettings({ refresh_eol_enabled: settings.refresh_eol_enabled, ai_enabled: toggle.checked, ai_provider: settings.ai_provider, ai_match_prompt: "" });
    showToast("Reset to default prompt.");
  };
}

function renderAppearanceTab() {
  document.querySelectorAll("[data-theme-option]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.themeOption === state.theme);
    btn.addEventListener("click", () => {
      setTheme(btn.dataset.themeOption);
      document.querySelectorAll("[data-theme-option]").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });
  document.getElementById("density-value-chip").textContent = state.density;
  document.getElementById("density-value-chip").addEventListener("click", () => {
    const next = state.density === "Compact" ? "Comfortable" : "Compact";
    setDensity(next);
    document.getElementById("density-value-chip").textContent = next;
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
